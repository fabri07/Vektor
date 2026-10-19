"""M·2d-2 — una venta ``sin_costo`` se costea cuando aparece su fuente.

La fecha del documento y la de carga son distintas: un remito de agosto cargado
después de las ventas de septiembre es la fuente de esas ventas. La excepción a
«no se revalúa» (M·2a) es acotada: sólo ``sin_costo``, viva, sin unidad dudosa,
y con la MISMA regla que en vivo (confirmada o estimada según la fuente).
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import costo_promedio_service, stock_service
from app.application.services.apertura_service import confirmar_apertura_de_producto
from app.application.services.costeo_retroactivo_service import MOTIVO_RETROACTIVO
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.costo_de_venta import MOTIVO_UNIDAD_DUDOSA, es_unidad_dudosa
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.persistence.repositories.costo_de_referencia_repository import es_fuente_de_costo

HOY = today_ar()
HACE_50 = HOY - timedelta(days=50)
HACE_40 = HOY - timedelta(days=40)
HACE_10 = HOY - timedelta(days=10)

COMPLETO = ContratoDeCosto(
    unit_cost_final=Decimal("100"), shipping_treatment="incluido", tax_treatment="no_aplica"
)
SIN_ENVIO = ContratoDeCosto(
    unit_cost_final=Decimal("100"), shipping_treatment="desconocido", tax_treatment="no_aplica"
)


def _a_las(dia: object, hora: int = 10) -> datetime:
    return datetime.combine(dia, datetime.min.time()).replace(hour=hora)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _sin_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    # El encolado del costo promedio después del commit no llega a ningún lado.
    from app.jobs import recalcular_costo_promedio as job  # noqa: PLC0415
    from app.persistence import costo_promedio_pendiente as pend  # noqa: PLC0415

    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)


async def _monotributo(db: AsyncSession, tenant: Tenant) -> None:
    await db.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant.tenant_id)
        .values(fiscal_condition="monotributo")
    )


async def _producto(db: AsyncSession, tenant: Tenant, **kw: object) -> Product:
    p = Product(
        tenant_id=tenant.tenant_id, name=str(kw.pop("name", "Vela")),
        sale_price_ars=Decimal("200"), stock_units=0, **kw,
    )
    db.add(p)
    await db.flush()
    return p


async def _comprar(
    db: AsyncSession, tenant: Tenant, p: Product, dia: object,
    contrato: ContratoDeCosto = COMPLETO,
) -> InventoryMovement:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        mov = await stock_service.increment_stock(
            p.id, tenant.tenant_id, 10, Decimal("100"), None, db,
            occurred_at=_a_las(dia), contrato=contrato,
        )
    await db.commit()
    return mov


async def _vender(db: AsyncSession, tenant: Tenant, p: Product, dia: object) -> SaleEntry:
    venta = SaleEntry(
        tenant_id=tenant.tenant_id, product_id=p.id, amount=Decimal("400"), quantity=2,
        transaction_date=_a_las(dia), payment_method="cash",
    )
    db.add(venta)
    await db.commit()
    return venta


async def _leer(db: AsyncSession, venta: SaleEntry) -> SaleEntry:
    fila = await db.get(SaleEntry, venta.id, populate_existing=True)
    assert fila is not None
    return fila


async def test_un_remito_atrasado_costea_la_venta_y_la_confirma(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _monotributo(db_session, sample_tenant)
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"

    await _comprar(db_session, sample_tenant, p, HACE_40)

    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_ars, v.cost_confirmed) == ("compra", Decimal("200.00"), True)
    assert v.cost_source_date == HACE_40


async def test_con_envio_desconocido_queda_estimada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _monotributo(db_session, sample_tenant)
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    await _comprar(db_session, sample_tenant, p, HACE_40, contrato=SIN_ENVIO)
    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_confirmed) == ("compra", False)
    assert v.cost_reason


async def test_una_venta_anterior_a_la_compra_sigue_sin_costo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_50)
    await _comprar(db_session, sample_tenant, p, HACE_40)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"


async def test_una_venta_anulada_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    venta.voided_at = datetime.now(UTC)
    venta.void_reason = "USER_CANCELLED"
    await db_session.commit()
    await _comprar(db_session, sample_tenant, p, HACE_40)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"


@pytest.mark.parametrize(
    "motivo", [MOTIVO_UNIDAD_DUDOSA, f"{MOTIVO_RETROACTIVO}; {MOTIVO_UNIDAD_DUDOSA}"]
)
async def test_la_unidad_dudosa_no_la_resuelve_ninguna_compra(
    db_session: AsyncSession, sample_tenant: Tenant, motivo: str
) -> None:
    p = await _producto(db_session, sample_tenant, base_units_per_sale_unit=1000,
                        sale_unit="gram")
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    await db_session.execute(
        update(SaleEntry).where(SaleEntry.id == venta.id).values(
            cost_origin="sin_costo", cost_reason=motivo, cost_unit_ars=None, cost_ars=None,
        )
    )
    await db_session.commit()
    await _comprar(db_session, sample_tenant, p, HACE_40)
    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_reason) == ("sin_costo", motivo)


async def test_otro_tenant_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    ajeno = await _producto(db_session, second_tenant)
    venta = await _vender(db_session, second_tenant, ajeno, HACE_10)
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, HACE_40)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"


async def test_reactivar_una_compra_anulada_costea(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    mov = await _comprar(db_session, sample_tenant, p, HACE_40)
    await stock_service.void_movement(mov, db_session)
    # Sin la compra no queda ningún costo: tampoco el cargado en el producto.
    p.unit_cost_ars = None
    p.custom_fields = {}
    await db_session.commit()
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"

    await stock_service.unvoid_movement(mov, db_session)
    await db_session.commit()
    assert (await _leer(db_session, venta)).cost_origin == "compra"


async def test_confirmar_el_stock_inicial_antes_costea_las_ventas(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    p = await _producto(db_session, sample_tenant)
    db_session.add(
        InventoryMovement(
            tenant_id=sample_tenant.tenant_id, product_id=p.id, movement_type="adjustment",
            qty=10, unit_cost=Decimal("70"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="manual:alta",
            occurred_at=datetime.combine(HOY, datetime.min.time(), UTC),
        )
    )
    await db_session.commit()
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"

    await confirmar_apertura_de_producto(
        db_session, sample_tenant.tenant_id, p.id, _a_las(HACE_40), sample_user.user_id
    )
    await db_session.commit()
    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_ars) == ("stock_inicial", Decimal("140.00"))


async def test_una_escritura_core_la_costea_la_publicacion_del_promedio(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dedup, relectura y M·R escriben con Core: el listener no las ve. Esas
    rutas marcan el producto (``marcar_todo_el_historial``) y la publicación del
    costo promedio lo recostea; acá se simula la publicación con ``reconstruir``."""
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    await db_session.execute(
        insert(InventoryMovement).values(
            id=uuid.uuid4(), tenant_id=sample_tenant.tenant_id, product_id=p.id,
            movement_type="purchase", qty=10, unit_cost=Decimal("100"),
            unit_cost_final=Decimal("100"),
            occurred_at=datetime.combine(HACE_40, datetime.min.time(), UTC),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    assert (await _leer(db_session, venta)).cost_origin == "sin_costo"

    await costo_promedio_service.reconstruir(db_session, sample_tenant.tenant_id, [p.id])
    await db_session.commit()
    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_ars) == ("compra", Decimal("200.00"))


async def test_es_fuente_de_costo_coincide_con_el_filtro_sql(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El predicado en Python y ``_filtro_candidatas`` dicen lo mismo."""
    from app.persistence.repositories.costo_de_referencia_repository import (  # noqa: PLC0415
        filtro_de_fuentes,
    )

    p = await _producto(db_session, sample_tenant)
    casos = [
        ("purchase", None, Decimal("10"), None, None),
        ("purchase", None, None, Decimal("10"), None),
        ("purchase", None, Decimal("0"), None, None),
        ("purchase", None, Decimal("10"), None, datetime.now(UTC)),
        ("adjustment", SOURCE_CATALOG_INITIAL_STOCK, Decimal("10"), None, None),
        ("adjustment", "manual_adjustment", Decimal("10"), None, None),
        ("sale", None, Decimal("10"), None, None),
    ]
    movs = []
    for tipo, fuente, costo, final, anulado in casos:
        m = InventoryMovement(
            tenant_id=sample_tenant.tenant_id, product_id=p.id, movement_type=tipo, qty=1,
            unit_cost=costo, unit_cost_final=final, source_type=fuente, voided_at=anulado,
            occurred_at=datetime.combine(HACE_40, datetime.min.time(), UTC),
        )
        db_session.add(m)
        movs.append(m)
    await db_session.flush()
    en_sql = set(
        (
            await db_session.execute(
                select(InventoryMovement.id).where(
                    *filtro_de_fuentes(sample_tenant.tenant_id, [p.id], HOY)
                )
            )
        ).scalars()
    )
    assert {m.id for m in movs if es_fuente_de_costo(m)} == en_sql
    assert en_sql  # no es un test vacío


def test_es_unidad_dudosa_por_contenido() -> None:
    assert es_unidad_dudosa(MOTIVO_UNIDAD_DUDOSA)
    assert es_unidad_dudosa(f"{MOTIVO_RETROACTIVO}; {MOTIVO_UNIDAD_DUDOSA}")
    assert not es_unidad_dudosa(None)
    assert not es_unidad_dudosa("sin compra ni costo cargado")


# ── El script (``--sin-costo``): lo que ya quedó en la base ─────────────────


async def _venta_sin_costo_con_compra_core(
    db: AsyncSession, tenant: Tenant
) -> tuple[Product, SaleEntry]:
    """Lo que dejó la base antes de M·2d-2: la venta quedó ``sin_costo`` y la
    compra con fecha anterior entró después, por un camino que no la recostea."""
    p = await _producto(db, tenant)
    venta = await _vender(db, tenant, p, HACE_10)
    await db.execute(
        insert(InventoryMovement).values(
            id=uuid.uuid4(), tenant_id=tenant.tenant_id, product_id=p.id,
            movement_type="purchase", qty=10, unit_cost=Decimal("100"),
            unit_cost_final=Decimal("100"), shipping_treatment="incluido",
            tax_treatment="no_aplica",
            occurred_at=datetime.combine(HACE_40, datetime.min.time(), UTC),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return p, venta


async def test_el_script_ensaya_aplica_es_idempotente_y_revierte(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from app.application.services.costeo_retroactivo_service import (  # noqa: PLC0415
        REPAIR_TYPE_SIN_COSTO,
        recostear_sin_costo_historico,
        revertir,
    )
    from app.persistence.models.repair import DataRepairItem, DataRepairRun  # noqa: PLC0415

    await _monotributo(db_session, sample_tenant)
    await db_session.commit()
    tid = sample_tenant.tenant_id
    _, venta = await _venta_sin_costo_con_compra_core(db_session, sample_tenant)
    # El id antes del rollback del ensayo: después la instancia queda vencida.
    vid = venta.id

    async def leer() -> SaleEntry:
        fila = await db_session.get(SaleEntry, vid, populate_existing=True)
        assert fila is not None
        return fila

    motivo_original = (await leer()).cost_reason

    ensayo = await recostear_sin_costo_historico(db_session, tid, aplicar=False)
    await db_session.rollback()
    assert (ensayo.candidatas, ensayo.costeadas, ensayo.run_id) == (1, 1, None)
    assert (await leer()).cost_origin == "sin_costo"

    r = await recostear_sin_costo_historico(db_session, tid, aplicar=True)
    await db_session.commit()
    assert (r.costeadas, r.confirmadas) == (1, 1)
    assert r.run_id is not None
    v = await leer()
    assert (v.cost_origin, v.cost_confirmed) == ("compra", True)
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.repair_type == REPAIR_TYPE_SIN_COSTO
    (item,) = (
        await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == r.run_id))
    ).scalars()
    assert (item.before_json or {})["cost_origin"] == "sin_costo"

    otra = await recostear_sin_costo_historico(db_session, tid, aplicar=True)
    await db_session.commit()
    assert (otra.candidatas, otra.run_id) == (0, None)

    revertidas, salteadas = await revertir(db_session, r.run_id)
    await db_session.commit()
    assert (revertidas, salteadas) == (1, 0)
    v = await leer()
    assert (v.cost_origin, v.cost_reason, v.cost_ars) == ("sin_costo", motivo_original, None)


async def test_el_script_no_mueve_aperturas_y_las_informa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from app.application.services.costeo_retroactivo_service import (  # noqa: PLC0415
        recostear_sin_costo_historico,
    )

    p = await _producto(db_session, sample_tenant, acquired_at=_a_las(HACE_50))
    carga = datetime.combine(HOY, datetime.min.time(), UTC)
    db_session.add(
        InventoryMovement(
            tenant_id=sample_tenant.tenant_id, product_id=p.id, movement_type="adjustment",
            qty=10, unit_cost=Decimal("70"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="manual:alta", occurred_at=carga,
        )
    )
    await db_session.commit()
    await _vender(db_session, sample_tenant, p, HACE_10)

    r = await recostear_sin_costo_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    assert r.candidatas == 0
    assert (r.aperturas_sin_confirmar, r.ventas_antes_de_apertura) == (1, 1)
    (mov,) = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == p.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    # ``acquired_at`` no prueba desde cuándo existía el stock: no se toca.
    assert mov.occurred_at is not None and mov.occurred_at.date() == HOY


async def test_una_venta_editada_en_la_misma_transaccion_que_su_fuente_se_costea(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    venta.quantity = 3
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await stock_service.increment_stock(
            p.id, sample_tenant.tenant_id, 10, Decimal("100"), None, db_session,
            occurred_at=_a_las(HACE_40), contrato=COMPLETO,
        )
    await db_session.commit()
    v = await _leer(db_session, venta)
    assert (v.cost_origin, v.cost_ars) == ("compra", Decimal("300.00"))


async def test_el_revert_no_se_frena_porque_cambio_el_costo_promedio(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """``cost_avg_*`` es derivado: una publicación entre el apply y el revert lo
    reescribe, y el revert igual restaura el costo principal."""
    from app.application.services.costeo_retroactivo_service import (  # noqa: PLC0415
        recostear_sin_costo_historico,
        revertir,
    )

    p, venta = await _venta_sin_costo_con_compra_core(db_session, sample_tenant)
    vid = venta.id
    r = await recostear_sin_costo_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    assert r.run_id is not None

    async def promedio() -> tuple[object, ...]:
        f = await db_session.get(SaleEntry, vid, populate_existing=True)
        assert f is not None
        return (f.cost_avg_estado, f.cost_avg_pendiente, f.cost_avg_motivo, f.cost_avg_ars)

    tras_el_apply = await promedio()
    await costo_promedio_service.reconstruir(db_session, sample_tenant.tenant_id, [p.id])
    await db_session.commit()
    # Si la publicación no cambiara nada, el test no probaría el caso.
    assert await promedio() != tras_el_apply, tras_el_apply

    revertidas, salteadas = await revertir(db_session, r.run_id)
    await db_session.commit()
    assert (revertidas, salteadas) == (1, 0)
    fila = await db_session.get(SaleEntry, vid, populate_existing=True)
    assert fila is not None and fila.cost_origin == "sin_costo"
