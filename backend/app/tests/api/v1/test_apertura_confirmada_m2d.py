"""M·2d: la fecha del stock inicial la confirma el usuario.

- El alta manual y la de «Otros» con stock exigen ``acquired_at`` (422 si falta
  o es futura) y fechan la apertura ahí, confirmada por el usuario.
- El costo vigente sigue siendo ``stock_inicial``: la edición manual del alta no
  le gana a la apertura atrasada.
- ``.../opening-confirmation`` (por producto y por archivo) mueve y confirma la
  apertura, recalcula el costo vigente, audita, e informa las ventas ya
  costeadas que quedan antes de la fecha nueva (su costo no se revalúa).
- El aviso de ``/insights/margins`` lista lo que falta confirmar; nada se
  mueve solo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.apertura_service import DECISION_TYPE
from app.application.services.facts_provider import aperturas_por_confirmar
from app.application.services.inventory_movement_origin import (
    FECHA_DE_USUARIO,
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_MANUAL_ADJUSTMENT,
)
from app.domain.business_time import today_ar
from app.domain.costo_vigente import ORIGEN_FIELD
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.persistence.models.user import User

HOY = today_ar()
HACE_60 = HOY - timedelta(days=60)
HACE_30 = HOY - timedelta(days=30)
HACE_10 = HOY - timedelta(days=10)


def _iso(d: Any, hora: int = 9) -> str:
    return datetime(d.year, d.month, d.day, hora).isoformat()


_ALTA = {
    "name": "Vela aromática",
    "sale_price_ars": "300.00",
    "unit_cost_ars": "80.00",
    "stock_units": 50,
}


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _aperturas(db: AsyncSession, pid: uuid.UUID) -> list[InventoryMovement]:
    # El fixture ``client`` comparte la sesión y NO confirma: el commit dispara
    # los listeners (costeo, costo vigente) como en producción.
    await db.commit()
    return list(
        (
            await db.execute(
                select(InventoryMovement)
                .where(
                    InventoryMovement.product_id == pid,
                    InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _fila(db: AsyncSession, modelo: Any, id_: uuid.UUID) -> Any:
    await db.commit()
    return await db.get(modelo, id_, populate_existing=True)


async def _producto_con_apertura_vieja(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    nombre: str = "Yerba",
    cargada: datetime | None = None,
    upload_id: uuid.UUID | None = None,
) -> Product:
    """Una apertura SIN confirmar, como las de antes de M·2d (fecha de carga).

    Sin ``unit_cost_ars`` en la fila: la única fuente de costo es la apertura.
    """
    p = Product(
        tenant_id=tenant_id, name=nombre, sale_price_ars=Decimal("100"), stock_units=20,
    )
    db.add(p)
    await db.flush()
    db.add(
        InventoryMovement(
            tenant_id=tenant_id, product_id=p.id, movement_type="adjustment", qty=20,
            unit_cost=Decimal("60"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", source_upload_id=upload_id,
            occurred_at=cargada or datetime.combine(HOY, datetime.min.time(), UTC),
        )
    )
    await db.commit()
    await db.refresh(p)
    return p


async def _venta(
    db: AsyncSession, tenant_id: uuid.UUID, pid: uuid.UUID, dia: Any, **cols: Any
) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tenant_id, product_id=pid, amount=Decimal("100"), quantity=1,
        transaction_date=datetime(dia.year, dia.month, dia.day, 12), payment_method="cash",
        **cols,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def _archivo(db: AsyncSession, tenant_id: uuid.UUID) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tenant_id, uploaded_by=None,
        original_filename="catalogo.xlsx", s3_key="catalogo.xlsx",
        content_type="application/vnd.ms-excel", size_bytes=100, purpose="ingestion",
    )
    db.add(f)
    await db.commit()
    return f


# ── Alta manual y desde «Otros» ──────────────────────────────────────────────


async def test_alta_con_stock_sin_fecha_es_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post("/api/v1/products", json=_ALTA, headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "FECHA_STOCK_INICIAL_REQUERIDA"


async def test_alta_sin_stock_no_pide_fecha(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/products", json={**_ALTA, "stock_units": 0}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("metodo", ["post", "patch"])
async def test_acquired_at_futura_es_422(
    client: AsyncClient, auth_headers: dict[str, str], metodo: str
) -> None:
    futura = _iso(HOY + timedelta(days=1))
    if metodo == "post":
        resp = await client.post(
            "/api/v1/products", json={**_ALTA, "acquired_at": futura}, headers=auth_headers
        )
    else:
        alta = await client.post(
            "/api/v1/products", json={**_ALTA, "stock_units": 0}, headers=auth_headers
        )
        resp = await client.patch(
            f"/api/v1/products/{alta.json()['id']}",
            json={"acquired_at": futura},
            headers=auth_headers,
        )
    assert resp.status_code == 422, resp.text


async def test_alta_fecha_la_apertura_confirmada_en_acquired_at(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_user: User,
) -> None:
    resp = await client.post(
        "/api/v1/products", json={**_ALTA, "acquired_at": _iso(HACE_30)}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    pid = uuid.UUID(resp.json()["id"])
    (mov,) = await _aperturas(db_session, pid)
    assert mov.occurred_at is not None
    assert mov.occurred_at.date() == HACE_30
    assert mov.opening_confirmed_at is not None
    assert mov.opening_confirmed_by_user_id == sample_user.user_id
    assert mov.opening_date_source == FECHA_DE_USUARIO
    # La edición manual del alta no le gana a la apertura atrasada.
    producto = await _fila(db_session, Product, pid)
    assert producto is not None
    assert producto.custom_fields[ORIGEN_FIELD] == "stock_inicial"


async def test_una_venta_posterior_a_la_fecha_y_anterior_a_la_carga_tiene_costo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    resp = await client.post(
        "/api/v1/products", json={**_ALTA, "acquired_at": _iso(HACE_30)}, headers=auth_headers
    )
    pid = resp.json()["id"]
    venta = await client.post(
        "/api/v1/sales",
        json={
            "amount": "300.00", "quantity": 1, "product_id": pid,
            "transaction_date": _iso(HACE_10, 12), "payment_method": "cash",
        },
        headers=auth_headers,
    )
    assert venta.status_code == 201, venta.text
    fila = await _fila(db_session, SaleEntry, uuid.UUID(venta.json()["id"]))
    assert fila is not None
    assert fila.cost_origin == "stock_inicial"
    assert fila.cost_ars == Decimal("80.00")


async def _registro_otros(db: AsyncSession, tenant_id: uuid.UUID) -> UnclassifiedRecord:
    r = UnclassifiedRecord(
        tenant_id=tenant_id, source="ingestion", context_label="Hoja 1",
        headers=["nombre", "precio"], row_data={"nombre": "Vela", "precio": "300"},
        suggested_entity="product", status=UNCLASSIFIED_STATUS_PENDING,
    )
    db.add(r)
    await db.commit()
    return r


async def test_otros_con_stock_exige_la_fecha_y_la_confirma(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    sin = await _registro_otros(db_session, sample_tenant.tenant_id)
    resp = await client.post(
        f"/api/v1/others/{sin.id}/reclassify",
        json={"entity_type": "product", "fields": _ALTA},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "FECHA_STOCK_INICIAL_REQUERIDA"

    con = await _registro_otros(db_session, sample_tenant.tenant_id)
    resp = await client.post(
        f"/api/v1/others/{con.id}/reclassify",
        json={"entity_type": "product", "fields": {**_ALTA, "acquired_at": _iso(HACE_30)}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    pid = (
        await db_session.execute(
            select(Product.id).where(
                Product.tenant_id == sample_tenant.tenant_id, Product.name == _ALTA["name"]
            )
        )
    ).scalar_one()
    (mov,) = await _aperturas(db_session, pid)
    assert mov.occurred_at is not None and mov.occurred_at.date() == HACE_30
    assert mov.opening_date_source == FECHA_DE_USUARIO


async def test_un_ajuste_manual_no_es_una_apertura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El chat (UPDATE_STOCK) y el PATCH escriben un ajuste, nunca una apertura."""
    p = await _producto_con_apertura_vieja(db_session, sample_tenant.tenant_id)
    mov = await stock_service.registrar_ajuste_manual(
        p.id, sample_tenant.tenant_id, db_session, delta=5, contrato=None, actor="chat"
    )
    assert mov is not None
    assert mov.source_type == SOURCE_MANUAL_ADJUSTMENT
    assert mov.opening_confirmed_at is None and mov.opening_date_source is None


# ── Confirmar o corregir después ─────────────────────────────────────────────


async def test_confirmar_por_producto_mueve_confirma_y_audita(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto_con_apertura_vieja(db_session, tid)
    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "aperturas": 1, "productos": 1, "ventas_costeadas_antes": 0, "venta_ids": [],
    }
    (mov,) = await _aperturas(db_session, p.id)
    assert mov.occurred_at is not None and mov.occurred_at.date() == HACE_60
    assert mov.opening_confirmed_by_user_id == sample_user.user_id
    auditoria = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == tid, DecisionAuditLog.decision_type == DECISION_TYPE
            )
        )
    ).scalar_one()
    assert auditoria.decision_data["alcance"] == "producto"
    assert str(mov.id) in auditoria.decision_data["fechas_anteriores"]
    # El costo vigente se recalculó con la apertura como fuente.
    producto = await _fila(db_session, Product, p.id)
    assert producto.custom_fields[ORIGEN_FIELD] == "stock_inicial"


async def test_atrasar_la_fecha_informa_las_ventas_costeadas_que_quedan_antes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto_con_apertura_vieja(
        db_session, tid, cargada=datetime.combine(HACE_60, datetime.min.time(), UTC)
    )
    venta = await _venta(db_session, tid, p.id, HACE_30)
    fila = await _fila(db_session, SaleEntry, venta.id)
    assert fila is not None and fila.cost_origin == "stock_inicial"
    costo = fila.cost_ars

    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HACE_10)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ventas_costeadas_antes"] == 1
    assert resp.json()["venta_ids"] == [str(venta.id)]
    fila = await _fila(db_session, SaleEntry, venta.id)
    assert fila is not None
    # No se revalúa (M·2a).
    assert (fila.cost_origin, fila.cost_ars) == ("stock_inicial", costo)
    ap = await aperturas_por_confirmar(db_session, tid)
    assert ap.ventas_costeadas_antes == 1
    # Sola, no levanta el aviso: no hay nada que el usuario pueda resolver.
    assert not ap.hay_algo


async def test_con_dos_aperturas_vivas_es_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto_con_apertura_vieja(db_session, tid)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=5,
            unit_cost=Decimal("60"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import",
        )
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HACE_30)},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "OPENING_AMBIGUOUS"


async def test_por_producto_confirma_sólo_la_carga_manual(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un producto con una carga manual y otra de un archivo: la manual se
    confirma por producto; la del archivo, desde el archivo. Ninguna queda sin
    camino."""
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    p = await _producto_con_apertura_vieja(db_session, tid)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=5,
            unit_cost=Decimal("60"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", source_upload_id=f.id,
        )
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HACE_30)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas"] == 1
    movs = {m.source_upload_id: m for m in await _aperturas(db_session, p.id)}
    assert movs[None].opening_confirmed_at is not None
    assert movs[f.id].opening_confirmed_at is None
    ap = await aperturas_por_confirmar(db_session, tid)
    assert ap.manuales == () and [a.upload_id for a in ap.por_archivo] == [f.id]


async def test_un_producto_dado_de_baja_no_se_confirma(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto_con_apertura_vieja(db_session, sample_tenant.tenant_id)
    p.is_active = False
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HACE_30)},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


async def test_sin_apertura_o_de_otro_tenant_es_404(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    sin = Product(tenant_id=sample_tenant.tenant_id, name="Sin", sale_price_ars=Decimal("1"))
    db_session.add(sin)
    await db_session.commit()
    ajeno = await _producto_con_apertura_vieja(db_session, second_tenant.tenant_id)
    for pid in (sin.id, ajeno.id):
        resp = await client.post(
            f"/api/v1/products/{pid}/opening-confirmation",
            json={"fecha": _iso(HACE_30)},
            headers=auth_headers,
        )
        assert resp.status_code == 404, resp.text
    (mov,) = await _aperturas(db_session, ajeno.id)
    assert mov.opening_confirmed_at is None


async def test_fecha_futura_al_confirmar_es_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto_con_apertura_vieja(db_session, sample_tenant.tenant_id)
    resp = await client.post(
        f"/api/v1/products/{p.id}/opening-confirmation",
        json={"fecha": _iso(HOY + timedelta(days=2))},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_confirmar_por_archivo_mueve_todas_sus_aperturas(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    a = await _producto_con_apertura_vieja(db_session, tid, nombre="A", upload_id=f.id)
    b = await _producto_con_apertura_vieja(db_session, tid, nombre="B", upload_id=f.id)
    otra = await _producto_con_apertura_vieja(db_session, tid, nombre="C")
    resp = await client.post(
        f"/api/v1/ingestion/files/{f.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas"] == 2
    for pid in (a.id, b.id):
        (mov,) = await _aperturas(db_session, pid)
        assert mov.occurred_at is not None and mov.occurred_at.date() == HACE_60
        assert mov.opening_confirmed_at is not None
    (mov_otra,) = await _aperturas(db_session, otra.id)
    assert mov_otra.opening_confirmed_at is None


async def test_confirmar_un_archivo_de_otro_tenant_es_404(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    second_tenant: Tenant,
) -> None:
    f = await _archivo(db_session, second_tenant.tenant_id)
    await _producto_con_apertura_vieja(db_session, second_tenant.tenant_id, upload_id=f.id)
    resp = await client.post(
        f"/api/v1/ingestion/files/{f.id}/opening-confirmation",
        json={"fecha": _iso(HACE_60)},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ── El aviso ─────────────────────────────────────────────────────────────────


async def test_el_aviso_lista_lo_que_falta_y_desaparece_al_confirmar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    await _producto_con_apertura_vieja(db_session, tid, nombre="A", upload_id=f.id)
    manual = await _producto_con_apertura_vieja(db_session, tid, nombre="Yerba")
    # Una venta anterior a la apertura (de carga): sin costo.
    await _venta(db_session, tid, manual.id, HACE_30)

    ap = await aperturas_por_confirmar(db_session, tid)
    assert ap.sin_confirmar == 2
    assert [(a.archivo, a.productos) for a in ap.por_archivo] == [("catalogo.xlsx", 1)]
    assert [m.producto for m in ap.manuales] == ["Yerba"]
    assert ap.ventas_sin_costo_antes == 1

    resp = await client.get("/api/v1/insights/margins", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["aperturas_por_confirmar"]["sin_confirmar"] == 2

    for url in (
        f"/api/v1/ingestion/files/{f.id}/opening-confirmation",
        f"/api/v1/products/{manual.id}/opening-confirmation",
    ):
        r = await client.post(url, json={"fecha": _iso(HACE_60)}, headers=auth_headers)
        assert r.status_code == 200, r.text

    ap = await aperturas_por_confirmar(db_session, tid)
    assert ap.sin_confirmar == 0
    # Ya no es anterior al stock inicial, pero sigue sin costo hasta que se la
    # recostee (M·2d-2): el aviso lo dice en vez de esconderla.
    assert ap.ventas_sin_costo_antes == 0
    assert ap.ventas_sin_costo_con_fuente == 1
    resp = await client.get("/api/v1/insights/margins", headers=auth_headers)
    aviso = resp.json()["aperturas_por_confirmar"]
    assert aviso["sin_confirmar"] == 0 and aviso["ventas_sin_costo_con_fuente"] == 1


async def test_el_aviso_no_ve_otro_tenant(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    await _producto_con_apertura_vieja(db_session, second_tenant.tenant_id)
    ap = await aperturas_por_confirmar(db_session, sample_tenant.tenant_id)
    assert not ap.hay_algo
