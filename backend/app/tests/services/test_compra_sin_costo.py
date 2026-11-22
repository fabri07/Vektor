"""``compra_sin_costo``: completa el costo de una compra importada desde su gasto.

Caso real (Jeans azules T42, ruta A): una compra ``purchase_import`` con el costo
vacío y UN gasto vivo de la misma fila del archivo. Se prueba la evidencia estricta
(lo que NO entra), el costo exacto, el recosteo de ventas y el «Deshacer» exacto.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.mejoras_de_datos import registro, servicio
from app.application.services.mejoras_de_datos.compra_sin_costo import CompraSinCosto
from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.services.test_costeo_fuente_tardia_m2d import _a_las

pytestmark = pytest.mark.usefixtures("mock_score_trigger")

HOY = today_ar()
HACE_40 = HOY - timedelta(days=40)
HACE_10 = HOY - timedelta(days=10)
MEJORA = "compra_sin_costo"


@pytest.fixture(autouse=True)
def _sin_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.jobs import recalcular_costo_promedio as job  # noqa: PLC0415
    from app.persistence import costo_promedio_pendiente as pend  # noqa: PLC0415

    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *_, **__: None)


def test_registrada_de_fabrica() -> None:
    assert isinstance(registro.obtener(MEJORA), CompraSinCosto)


async def _escenario(
    db: AsyncSession,
    tenant: Tenant,
    *,
    nombre: str = "Jeans azules T42",
    qty: int = 3,
    importe: str | None = "4500.00",
    con_confirm: bool = True,
    ref: str = "ref-fila-1",
    version: int | None = 1,
    source_type: str = "purchase_import",
    editado: bool = False,
) -> tuple[Product, InventoryMovement, uuid.UUID]:
    await db.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant.tenant_id)
        .values(fiscal_condition="monotributo")
    )
    p = Product(
        tenant_id=tenant.tenant_id, name=nombre, sale_price_ars=Decimal("2000"), stock_units=qty
    )
    db.add(p)
    await db.flush()
    upload = uuid.uuid4()
    mov = InventoryMovement(
        tenant_id=tenant.tenant_id,
        product_id=p.id,
        movement_type="purchase",
        qty=qty,
        unit_cost=None,
        source_type=source_type,
        source_upload_id=upload,
        source_row_ref=ref,
        cost_contract_version=version,
        occurred_at=_a_las(HACE_40),
    )
    db.add(mov)
    if importe is not None:
        db.add(
            ExpenseEntry(
                tenant_id=tenant.tenant_id,
                amount=Decimal(importe),
                category="INVENTORY",
                expense_type="COGS",
                transaction_date=_a_las(HACE_40),
                description="Compra",
                product_id=p.id,
                source_upload_id=upload,
                source_row_ref=ref,
                has_user_edits=editado,
            )
        )
    if con_confirm:
        db.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                file_id=upload,
                stage="confirm",
                detail={"mappings": {"flat": {"Costo total": "line_total_cost_ars"}}},
            )
        )
    await db.commit()
    return p, mov, upload


async def _vender(db: AsyncSession, tenant: Tenant, p: Product, dia: Any) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tenant.tenant_id,
        product_id=p.id,
        amount=Decimal("4000"),
        quantity=2,
        transaction_date=_a_las(dia),
        payment_method="cash",
    )
    db.add(v)
    await db.commit()
    return v


async def _releer(db: AsyncSession, modelo: Any, pk: uuid.UUID) -> Any:
    fila = await db.get(modelo, pk, populate_existing=True)
    assert fila is not None
    return fila


async def _detectar(db: AsyncSession, tenant: Tenant) -> Any:
    return await CompraSinCosto().detectar(db, tenant.tenant_id)


async def test_completa_el_costo_y_deja_estimado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p, mov, _ = await _escenario(db_session, sample_tenant)
    det = await _detectar(db_session, sample_tenant)
    assert [c.detalle["unit_cost"] for c in det.candidatos] == ["1500.00"]

    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    await db_session.commit()

    assert r.aplicadas == 1
    m = await _releer(db_session, InventoryMovement, mov.id)
    assert (m.unit_cost, m.unit_cost_final) == (Decimal("1500.00"), Decimal("1500.00"))
    assert (m.shipping_treatment, m.tax_treatment) == ("desconocido", "desconocido")
    assert (await _releer(db_session, Product, p.id)).unit_cost_ars == Decimal("1500.00")
    # Idempotente: el detector es el libro de cuentas.
    assert (await _detectar(db_session, sample_tenant)).candidatos == []


@pytest.mark.parametrize(
    "kw, motivo",
    [
        ({"con_confirm": False}, "costo total"),
        ({"importe": None}, "gasto único"),
        ({"importe": "4500.01"}, "exacto"),
        ({"editado": True}, "editado"),
        ({"ref": "x" * 65}, "demasiado larga"),
    ],
)
async def test_sin_evidencia_va_a_mano_y_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant, kw: dict[str, Any], motivo: str
) -> None:
    _, mov, _ = await _escenario(db_session, sample_tenant, **kw)
    det = await _detectar(db_session, sample_tenant)
    assert det.candidatos == []
    assert any(motivo in v for v in det.a_mano.values()), det.a_mano

    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    assert r.aplicadas == 0
    assert (await _releer(db_session, InventoryMovement, mov.id)).unit_cost is None


@pytest.mark.parametrize(
    "kw",
    [
        {"source_type": "catalog_initial_stock"},  # ruta B: no es compra importada
        {"version": None},
    ],
)
async def test_otras_rutas_no_son_candidatas(
    db_session: AsyncSession, sample_tenant: Tenant, kw: dict[str, Any]
) -> None:
    await _escenario(db_session, sample_tenant, **kw)
    det = await _detectar(db_session, sample_tenant)
    assert det.candidatos == [] and det.a_mano == {}


async def test_gasto_anulado_no_cuenta(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    await _escenario(db_session, sample_tenant)
    await db_session.execute(
        update(ExpenseEntry).values(voided_at=datetime.now(UTC), void_reason="USER_CANCELLED")
    )
    assert (await _detectar(db_session, sample_tenant)).candidatos == []


async def test_otro_tenant_no_ve_la_compra(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    await _escenario(db_session, sample_tenant)
    assert (await _detectar(db_session, second_tenant)).candidatos == []


async def test_recostea_la_venta_y_deshacer_la_devuelve_a_sin_costo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p, mov, _ = await _escenario(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    assert (await _releer(db_session, SaleEntry, venta.id)).cost_origin == "sin_costo"
    antes = await _releer(db_session, SaleEntry, venta.id)
    foto_antes = (antes.cost_origin, antes.cost_ars, antes.cost_confirmed)

    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    await db_session.commit()
    assert r.run_id is not None

    v = await _releer(db_session, SaleEntry, venta.id)
    assert (v.cost_origin, v.cost_ars, v.cost_confirmed) == ("compra", Decimal("3000.00"), False)
    items = (
        (await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == r.run_id)))
        .scalars()
        .all()
    )
    assert [i.sale_entry_id for i in items] == [venta.id]

    rev = await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)
    await db_session.commit()

    assert rev.completa and len(rev.revertidos) == 2  # la compra y la venta
    m = await _releer(db_session, InventoryMovement, mov.id)
    assert (m.unit_cost, m.unit_cost_final) == (None, None)
    v = await _releer(db_session, SaleEntry, venta.id)
    assert (v.cost_origin, v.cost_ars, v.cost_confirmed) == foto_antes
    assert (await _releer(db_session, Product, p.id)).unit_cost_ars in (None, Decimal("0"))


async def test_deshacer_no_se_reaplica_solo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    _, mov, _ = await _escenario(db_session, sample_tenant)
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    await db_session.commit()
    assert r.run_id is not None
    await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)
    await db_session.commit()

    again = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    assert again.aplicadas == 0
    assert (await _releer(db_session, InventoryMovement, mov.id)).unit_cost is None
    # A mano sí se puede forzar.
    manual = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, {mov.id})
    assert manual.aplicadas == 1


async def test_venta_editada_despues_queda_pendiente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p, mov, _ = await _escenario(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, HACE_10)
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, MEJORA, automatica=True)
    await db_session.commit()
    assert r.run_id is not None
    await db_session.execute(
        update(SaleEntry).where(SaleEntry.id == venta.id).values(cost_reason="editada")
    )
    await db_session.commit()

    rev = await servicio.revertir(db_session, sample_tenant.tenant_id, r.run_id)
    assert not rev.completa and str(venta.id) in rev.pendientes
    # La compra sí volvió atrás.
    assert (await _releer(db_session, InventoryMovement, mov.id)).unit_cost is None
