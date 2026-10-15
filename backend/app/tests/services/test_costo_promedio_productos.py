"""M·P4-1: el costo promedio en la pantalla de productos.

Dos cosas se protegen acá:
- ``costos_a_lote`` lee lo MISMO que ``costo_a`` producto por producto, en una
  sola sentencia, sin mirar lo pendiente (nunca «recalculando»);
- ``GET /products`` sólo lo muestra con la compuerta, y no cambia nada más de la
  respuesta (el margen y el costo de reposición siguen iguales).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.config.avg_cost_display_rollout import avg_cost_display_enabled_for
from app.config.settings import get_settings
from app.domain.business_time import today_ar
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


def _dia(n: int) -> datetime:
    return datetime(2026, 3, n, 12, 0)


async def _producto(
    db: AsyncSession, tenant_id: uuid.UUID, nombre: str, costo: str = "200"
) -> Product:
    p = Product(
        tenant_id=tenant_id,
        name=nombre,
        sale_price_ars=Decimal("300"),
        unit_cost_ars=Decimal(costo),
        stock_units=0,
    )
    db.add(p)
    await db.flush()
    return p


async def _compra(db: AsyncSession, p: Product, qty: int, costo: str | None, dia: int) -> None:
    await stock_service.increment_stock(
        p.id, p.tenant_id, qty, None if costo is None else Decimal(costo), None, db,
        occurred_at=_dia(dia),
    )


async def _venta(db: AsyncSession, p: Product, qty: int, dia: int) -> None:
    venta = SaleEntry(
        tenant_id=p.tenant_id,
        product_id=p.id,
        amount=Decimal("300") * qty,
        quantity=qty,
        transaction_date=_dia(dia),
        payment_method="cash",
    )
    db.add(venta)
    await db.flush()
    await stock_service.decrement_stock(
        p.id, p.tenant_id, qty, stock_service.sale_source_event_id(venta.id), db,
        occurred_at=_dia(dia),
    )


@pytest.fixture
def habilitar(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _set(valores: list[str]) -> None:
        monkeypatch.setattr(get_settings(), "AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS", valores)

    return _set


# ── La compuerta ─────────────────────────────────────────────────────────────


def test_el_default_no_habilita_a_nadie() -> None:
    assert get_settings().AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS == []
    assert avg_cost_display_enabled_for(uuid.uuid4()) is False


def test_habilita_solo_a_los_configurados(habilitar: Any) -> None:
    adentro, afuera = uuid.uuid4(), uuid.uuid4()
    habilitar([str(adentro).upper()])
    assert avg_cost_display_enabled_for(adentro) is True
    assert avg_cost_display_enabled_for(str(adentro)) is True
    assert avg_cost_display_enabled_for(afuera) is False
    assert avg_cost_display_enabled_for("no-es-uuid") is False


# ── La lectura por lote ──────────────────────────────────────────────────────


async def _escenario(db: AsyncSession, tid: uuid.UUID) -> tuple[Product, Product, Product]:
    a = await _producto(db, tid, "A")
    await _compra(db, a, 10, "100", 1)
    await _venta(db, a, 5, 2)
    await _compra(db, a, 10, "200", 3)
    # El mismo día: compra y venta (empate de día, desempata la clase y la hora).
    await _compra(db, a, 5, "400", 4)
    await _venta(db, a, 3, 4)
    b = await _producto(db, tid, "B")
    await _compra(db, b, 4, None, 2)  # sin costo → incompleto/sin_medicion
    await _compra(db, b, 4, "50", 3)
    sin_historial = await _producto(db, tid, "C")
    await svc.reconstruir(db, tid)
    return a, b, sin_historial


@pytest.mark.parametrize("dia", [1, 2, 3, 4, 20])
async def test_el_lote_lee_lo_mismo_que_costo_a(
    db_session: AsyncSession, sample_tenant: Tenant, dia: int
) -> None:
    tid = sample_tenant.tenant_id
    productos = await _escenario(db_session, tid)
    fecha = date(2026, 3, dia)

    lote = await svc.costos_a_lote(db_session, tid, [p.id for p in productos], fecha)

    for p in productos:
        uno = await svc.costo_a(db_session, tid, p.id, fecha)
        assert (lote.get(p.id) and lote[p.id].id) == (uno and uno.id), p.name
    assert productos[2].id not in lote


async def test_el_lote_no_cruza_tenants_ni_lee_antes_de_la_primera_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    a, b, _ = await _escenario(db_session, tid)

    assert await svc.costos_a_lote(db_session, uuid.uuid4(), [a.id, b.id], date(2026, 3, 20)) == {}
    assert await svc.costos_a_lote(db_session, tid, [a.id, b.id], date(2026, 2, 28)) == {}
    assert await svc.costos_a_lote(db_session, tid, [], date(2026, 3, 20)) == {}


async def test_el_lote_es_una_sola_sentencia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    productos = await _escenario(db_session, tid)
    sentencias: list[str] = []

    def _contar(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        sentencias.append(statement)

    motor = db_session.bind.sync_engine
    event.listen(motor, "before_cursor_execute", _contar)
    try:
        await svc.costos_a_lote(db_session, tid, [p.id for p in productos] * 3, date(2026, 3, 20))
    finally:
        event.remove(motor, "before_cursor_execute", _contar)

    assert len(sentencias) == 1


async def test_con_un_recalculo_pendiente_lee_el_ultimo_publicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Decisión del dueño (2026-10-14): nunca «recalculando»."""
    tid = sample_tenant.tenant_id
    a, _, _ = await _escenario(db_session, tid)
    publicado = await svc.costo_a(db_session, tid, a.id, date(2026, 3, 20))
    await db_session.execute(
        update(ProductCostState)
        .where(ProductCostState.product_id == a.id)
        .values(pendiente_desde=date.min)
    )

    lote = await svc.costos_a_lote(db_session, tid, [a.id], date(2026, 3, 20))

    assert publicado is not None and lote[a.id].id == publicado.id


# ── GET /products ────────────────────────────────────────────────────────────


def _por_id(resp: Any) -> dict[str, dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    return {fila["id"]: fila for fila in resp.json()}


async def test_sin_compuerta_los_campos_salen_en_null(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    a, _, _ = await _escenario(db_session, sample_tenant.tenant_id)

    fila = _por_id(await client.get("/api/v1/products", headers=auth_headers))[str(a.id)]

    assert (fila["avg_cost_ars"], fila["avg_cost_estado"], fila["avg_cost_motivo"]) == (
        None, None, None,
    )


async def test_con_compuerta_muestra_el_promedio_y_nada_mas_cambia(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitar: Any,
) -> None:
    tid = sample_tenant.tenant_id
    a, b, c = await _escenario(db_session, tid)
    antes = _por_id(await client.get("/api/v1/products", headers=auth_headers))
    habilitar([str(tid)])

    despues = _por_id(await client.get("/api/v1/products", headers=auth_headers))
    detalle = await client.get(f"/api/v1/products/{a.id}", headers=auth_headers)

    vigente = await svc.costo_a(db_session, tid, a.id, today_ar())
    assert vigente is not None and vigente.estado == "medido"
    assert vigente.costo_promedio is not None
    fila_a = despues[str(a.id)]
    assert fila_a["avg_cost_estado"] == "medido"
    assert Decimal(fila_a["avg_cost_ars"]) == vigente.costo_promedio.quantize(Decimal("0.01"))
    assert detalle.status_code == 200
    assert detalle.json()["avg_cost_ars"] == fila_a["avg_cost_ars"]
    # Sin historial: sin dato (no «sin compras»: puede faltar el backfill).
    assert despues[str(c.id)]["avg_cost_estado"] is None
    assert despues[str(c.id)]["avg_cost_ars"] is None
    # Un estado sin medición no trae importe.
    fila_b = despues[str(b.id)]
    assert fila_b["avg_cost_estado"] in ("incompleto", "sin_medicion", "medido")
    if fila_b["avg_cost_estado"] == "sin_medicion":
        assert fila_b["avg_cost_ars"] is None
    # Lo demás de la respuesta (costo de reposición, margen) no cambia.
    campos = ("avg_cost_ars", "avg_cost_estado", "avg_cost_motivo")
    for pid, fila in despues.items():
        assert {k: v for k, v in fila.items() if k not in campos} == {
            k: v for k, v in antes[pid].items() if k not in campos
        }


async def test_otro_tenant_habilitado_no_ve_ni_habilita_al_primero(
    client: AsyncClient,
    auth_headers: dict[str, str],
    second_auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
    habilitar: Any,
) -> None:
    a, _, _ = await _escenario(db_session, sample_tenant.tenant_id)
    habilitar([str(second_tenant.tenant_id)])

    propio = _por_id(await client.get("/api/v1/products", headers=auth_headers))
    ajeno = _por_id(await client.get("/api/v1/products", headers=second_auth_headers))

    assert propio[str(a.id)]["avg_cost_ars"] is None
    assert str(a.id) not in ajeno


def _registro(
    tid: uuid.UUID,
    pid: uuid.UUID,
    *,
    costo: str,
    dia: int = 5,
    clase_orden: int = 1,
    hora: int = 12,
    creado: int = 12,
    evento: int = 1,
) -> ProductCostHistory:
    return ProductCostHistory(
        tenant_id=tid,
        product_id=pid,
        dia=date(2026, 3, dia),
        clase_orden=clase_orden,
        fecha=datetime(2026, 3, dia, hora),
        creado=datetime(2026, 3, dia, creado),
        # Con letras: SQLite guarda como entero un UUID de puros dígitos.
        evento_id=uuid.UUID(f"aaaaaaaa-0000-4000-8000-{evento:012d}"),
        clase="compra",
        u_conocidas=Decimal("1"),
        u_sin_costo=Decimal("0"),
        costo_promedio=Decimal(costo),
        estado="medido",
        stock_conocido=True,
        aproximado=False,
        revision=1,
    )


@pytest.mark.parametrize(
    ("perdedor", "ganador"),
    [
        ({"dia": 4}, {"dia": 5}),
        ({"clase_orden": 1}, {"clase_orden": 2}),
        ({"hora": 10}, {"hora": 11}),
        ({"creado": 10}, {"creado": 11}),
        ({"evento": 1}, {"evento": 2}),
    ],
)
async def test_el_lote_desempata_igual_que_costo_a(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    perdedor: dict[str, int],
    ganador: dict[str, int],
) -> None:
    """Cada clave del orden, sola: si el lote ordenara distinto que ``costo_a`` en
    cualquiera, la pantalla mostraría otro promedio que el de las ventas."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Desempate")
    db_session.add_all(
        [
            _registro(tid, p.id, costo="111", **perdedor),
            _registro(tid, p.id, costo="222", **ganador),
        ]
    )
    await db_session.flush()

    lote = await svc.costos_a_lote(db_session, tid, [p.id], date(2026, 3, 20))
    uno = await svc.costo_a(db_session, tid, p.id, date(2026, 3, 20))

    assert uno is not None and uno.costo_promedio == Decimal("222")
    assert lote[p.id].costo_promedio == Decimal("222")
