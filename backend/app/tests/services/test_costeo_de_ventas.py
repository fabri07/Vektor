"""M·2a — cada venta congela su costo al confirmarse, en el servidor.

Contra la sesión real: la venta se crea como lo hacen las rutas (``SaleEntry``
+ ``commit``) y el listener la costea en la misma transacción.
"""

from __future__ import annotations

import unittest.mock
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.stock_service import increment_stock
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.costo_vigente import MANUAL_FIELD
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

HOY = today_ar()
AYER = HOY - timedelta(days=1)


def _a_las(dia: object, hora: int = 10) -> datetime:
    return datetime.combine(dia, datetime.min.time()).replace(hour=hora)  # type: ignore[arg-type]


CONFIRMADO = ContratoDeCosto(
    unit_cost_final=Decimal("100"), shipping_treatment="incluido", tax_treatment="no_aplica"
)


async def _producto(db: AsyncSession, tenant: Tenant, **kw: object) -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name=str(kw.pop("name", "Vela")),
        sale_price_ars=Decimal("200"),
        unit_cost_ars=kw.pop("unit_cost_ars", None),  # type: ignore[arg-type]
        stock_units=0,
        **kw,  # type: ignore[arg-type]
    )
    db.add(p)
    await db.flush()
    return p


async def _comprar(
    db: AsyncSession,
    tenant: Tenant,
    p: Product,
    costo: str,
    fecha: datetime,
    contrato: ContratoDeCosto | None = None,
    qty: int = 10,
) -> None:
    contrato = contrato or ContratoDeCosto(
        unit_cost_final=Decimal(costo), shipping_treatment="incluido", tax_treatment="no_aplica"
    )
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await increment_stock(
            p.id, tenant.tenant_id, qty, Decimal(costo), None, db,
            occurred_at=fecha, contrato=contrato,
        )


async def _vender(
    db: AsyncSession, tenant: Tenant, p: Product, fecha: datetime, qty: int = 2
) -> SaleEntry:
    venta = SaleEntry(
        tenant_id=tenant.tenant_id,
        product_id=p.id,
        amount=Decimal("400"),
        quantity=qty,
        transaction_date=fecha,
        payment_method="cash",
    )
    db.add(venta)
    await db.commit()
    return venta


async def _regimen(db: AsyncSession, tenant: Tenant, valor: str | None) -> None:
    from sqlalchemy import update  # noqa: PLC0415

    await db.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant.tenant_id)
        .values(fiscal_condition=valor)
    )


async def test_venta_con_compra_confirmada_congela_el_costo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(AYER))
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY), qty=3)

    assert venta.cost_unit_ars == Decimal("100.00")
    assert venta.cost_ars == Decimal("300.00")
    assert venta.cost_confirmed is True
    assert venta.cost_origin == "compra"
    assert venta.cost_reason is None
    assert venta.cost_snapshot_version == 1


async def test_una_compra_posterior_no_cambia_el_snapshot(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(AYER))
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    await _comprar(db_session, sample_tenant, p, "150", _a_las(HOY, 18))
    await db_session.commit()
    await db_session.refresh(venta)
    assert venta.cost_unit_ars == Decimal("100.00")


async def test_venta_atrasada_toma_la_fuente_de_su_dia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una venta sin red de ayer, sincronizada hoy, no ve la compra de hoy."""
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(AYER - timedelta(days=1)))
    await _comprar(db_session, sample_tenant, p, "150", _a_las(HOY))
    venta = await _vender(db_session, sample_tenant, p, _a_las(AYER))
    assert venta.cost_unit_ars == Decimal("100.00")


async def test_envio_desconocido_queda_como_estimacion_con_motivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(
        db_session, sample_tenant, p, "100", _a_las(AYER),
        contrato=ContratoDeCosto(unit_cost_final=Decimal("100")),
    )
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    assert venta.cost_unit_ars == Decimal("100.00")
    assert venta.cost_confirmed is False
    assert venta.cost_origin == "compra"
    assert venta.cost_reason == "no se sabe si el costo incluye el envío"


async def test_ri_con_iva_recuperable_compara_neto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "responsable_inscripto")
    p = await _producto(db_session, sample_tenant)
    await _comprar(
        db_session, sample_tenant, p, "121", _a_las(AYER),
        contrato=ContratoDeCosto(
            unit_cost_final=Decimal("121"),
            shipping_treatment="no_aplica",
            tax_treatment="incluido",
            tax_amount=Decimal("21"),
            tax_recoverable=True,
        ),
    )
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY), qty=1)
    assert venta.cost_unit_ars == Decimal("100.00")
    assert venta.cost_confirmed is True


async def test_sin_costo_queda_pendiente_nunca_cero(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    assert venta.cost_origin == "sin_costo"
    assert venta.cost_unit_ars is None
    assert venta.cost_ars is None
    assert venta.cost_confirmed is False
    assert venta.cost_snapshot_version == 1


async def test_edicion_manual_es_estimacion_y_una_posterior_no_es_elegible(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(
        db_session,
        sample_tenant,
        unit_cost_ars=Decimal("90"),
        custom_fields={MANUAL_FIELD: {"costo": "90", "fecha": _a_las(AYER).isoformat()}},
    )
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    assert venta.cost_origin == "manual"
    assert venta.cost_unit_ars == Decimal("90.00")
    assert venta.cost_confirmed is False

    p.custom_fields = {MANUAL_FIELD: {"costo": "95", "fecha": _a_las(HOY).isoformat()}}
    vieja = await _vender(db_session, sample_tenant, p, _a_las(AYER - timedelta(days=3)))
    # La edición es posterior a la venta: no es elegible; queda el costo cargado
    # sin fecha como estimación.
    assert vieja.cost_origin == "costo_cargado"
    assert vieja.cost_confirmed is False


async def test_venta_por_peso_divide_por_el_factor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(
        db_session, sample_tenant, sale_unit="gram", base_units_per_sale_unit=1000
    )
    await _comprar(db_session, sample_tenant, p, "800", _a_las(AYER), qty=5000)
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY), qty=750)
    assert venta.cost_unit_ars == Decimal("800.00")
    assert venta.cost_ars == Decimal("600.00")


async def test_cambiar_la_cantidad_mantiene_el_costo_unitario(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(AYER))
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY), qty=2)
    await _comprar(db_session, sample_tenant, p, "150", _a_las(HOY, 18))
    venta.quantity = 4
    await db_session.commit()
    assert venta.cost_unit_ars == Decimal("100.00")
    assert venta.cost_ars == Decimal("400.00")


async def test_venta_sin_producto_o_anulada_no_se_costea(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    suelta = SaleEntry(
        tenant_id=sample_tenant.tenant_id,
        amount=Decimal("50"),
        quantity=1,
        transaction_date=_a_las(HOY),
        payment_method="inflow",
    )
    db_session.add(suelta)
    await db_session.commit()
    assert suelta.cost_snapshot_version is None


async def test_otro_tenant_no_aporta_candidatas(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    ajeno = await _producto(db_session, second_tenant)
    await _comprar(db_session, second_tenant, ajeno, "100", _a_las(AYER))
    # Venta del tenant 1 apuntando (mal) al producto del tenant 2: no se costea
    # con datos ajenos.
    venta = await _vender(db_session, sample_tenant, ajeno, _a_las(HOY))
    assert venta.cost_snapshot_version is None


async def test_si_el_costeo_falla_la_venta_se_guarda_sin_snapshot(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    with unittest.mock.patch(
        "app.persistence.costeo_de_ventas._costear_del_tenant", side_effect=RuntimeError("x")
    ):
        venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    assert venta.id is not None
    assert venta.cost_snapshot_version is None
