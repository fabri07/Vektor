"""M·1a — lectura del costo de referencia contra la base (SQLite de la suite).

Lo que importa acá es lo que el dominio no ve: anulados, tenant, tipo de
movimiento, el corte por fecha de NEGOCIO hecho en SQL y que el orden del SQL
coincida con el del dominio.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.domain.business_time import today_ar
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.costo_de_referencia_repository import costos_de_referencia

pytestmark = pytest.mark.asyncio

HOY = today_ar()


def _dt(dias_atras: int, hora: int = 12) -> datetime:
    return datetime.combine(HOY - timedelta(days=dias_atras), time(hora), tzinfo=UTC)


async def _producto(s: AsyncSession, tenant_id: uuid.UUID, costo: str | None = None) -> Product:
    p = Product(
        id=uuid.uuid4(), tenant_id=tenant_id, name=f"P {uuid.uuid4().hex[:6]}",
        sale_price_ars=Decimal("200"), stock_units=5, provenance="REAL", custom_fields={},
        unit_cost_ars=Decimal(costo) if costo is not None else None,
    )
    s.add(p)
    await s.flush()
    return p


async def _mov(
    s: AsyncSession, p: Product, costo: str | None, *, dias_atras: int,
    cargado_dias_atras: int = 0, tipo: str = "purchase", anulado: bool = False,
    source_type: str | None = "purchase_import", **extra: object,
) -> InventoryMovement:
    m = InventoryMovement(
        id=uuid.uuid4(), tenant_id=p.tenant_id, product_id=p.id, movement_type=tipo,
        qty=1, unit_cost=Decimal(costo) if costo is not None else None,
        occurred_at=_dt(dias_atras), created_at=_dt(cargado_dias_atras),
        source_type=source_type, voided_at=datetime.now(UTC) if anulado else None,
        **extra,
    )
    s.add(m)
    await s.flush()
    return m


async def test_elige_por_fecha_de_negocio_y_anular_vuelve_a_la_anterior(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    nueva = await _mov(db_session, p, "120", dias_atras=2, cargado_dias_atras=2)
    # Comprobante atrasado cargado hoy: no pisa a la más nueva.
    await _mov(db_session, p, "90", dias_atras=30, cargado_dias_atras=0)

    r = (await costos_de_referencia(db_session, sample_tenant.tenant_id, [p.id], HOY))[p.id]
    assert (r.origen, r.costo_unitario, r.movement_id) == ("compra", Decimal("120"), nueva.id)

    nueva.voided_at = datetime.now(UTC)
    await db_session.flush()
    r = (await costos_de_referencia(db_session, sample_tenant.tenant_id, [p.id], HOY))[p.id]
    assert r.costo_unitario == Decimal("90")


async def test_el_corte_deja_afuera_la_compra_posterior(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _mov(db_session, p, "100", dias_atras=10)
    await _mov(db_session, p, "150", dias_atras=1)
    corte = HOY - timedelta(days=5)
    r = (await costos_de_referencia(db_session, sample_tenant.tenant_id, [p.id], corte))[p.id]
    assert r.costo_unitario == Decimal("100")


async def test_ignora_ventas_ajustes_y_compras_sin_costo(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _mov(db_session, p, "100", dias_atras=10)
    await _mov(db_session, p, "999", dias_atras=1, tipo="sale", source_type=None)
    await _mov(db_session, p, "888", dias_atras=1, tipo="adjustment",
               source_type="manual_adjustment")
    await _mov(db_session, p, "0", dias_atras=1)
    await _mov(db_session, p, None, dias_atras=1)
    r = (await costos_de_referencia(db_session, sample_tenant.tenant_id, [p.id], HOY))[p.id]
    assert r.costo_unitario == Decimal("100")


async def test_compra_real_antes_que_stock_inicial_y_este_antes_que_lo_cargado(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    con_compra = await _producto(db_session, sample_tenant.tenant_id, costo="70")
    await _mov(db_session, con_compra, "100", dias_atras=20)
    await _mov(db_session, con_compra, "80", dias_atras=1, source_type=SOURCE_CATALOG_INITIAL_STOCK)

    solo_inicial = await _producto(db_session, sample_tenant.tenant_id, costo="70")
    await _mov(db_session, solo_inicial, "80", dias_atras=1,
               source_type=SOURCE_CATALOG_INITIAL_STOCK)

    solo_cargado = await _producto(db_session, sample_tenant.tenant_id, costo="70")
    sin_nada = await _producto(db_session, sample_tenant.tenant_id)

    r = await costos_de_referencia(
        db_session, sample_tenant.tenant_id,
        [con_compra.id, solo_inicial.id, solo_cargado.id, sin_nada.id], HOY,
    )
    assert (r[con_compra.id].origen, r[con_compra.id].costo_unitario) == ("compra", Decimal("100"))
    assert (r[solo_inicial.id].origen, r[solo_inicial.id].costo_unitario) == (
        "stock_inicial", Decimal("80"))
    assert (r[solo_cargado.id].origen, r[solo_cargado.id].costo_unitario) == (
        "costo_cargado", Decimal("70"))
    assert (r[sin_nada.id].origen, r[sin_nada.id].costo_unitario) == ("sin_costo", None)


async def test_otro_tenant_no_aparece_ni_presta_sus_compras(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    otro = Tenant(tenant_id=uuid.uuid4(), legal_name="Otro", display_name="Otro")
    db_session.add(otro)
    await db_session.flush()
    ajeno = await _producto(db_session, otro.tenant_id, costo="50")
    await _mov(db_session, ajeno, "500", dias_atras=1)
    propio = await _producto(db_session, sample_tenant.tenant_id)
    # Defensa en profundidad: un movimiento con tenant ajeno que apunta al
    # producto propio (la FK no cruza tenant) tampoco se usa.
    db_session.add(InventoryMovement(
        id=uuid.uuid4(), tenant_id=otro.tenant_id, product_id=propio.id,
        movement_type="purchase", qty=1, unit_cost=Decimal("777"), occurred_at=_dt(1),
        source_type="purchase_import",
    ))
    await db_session.flush()

    r = await costos_de_referencia(db_session, sample_tenant.tenant_id, [ajeno.id, propio.id], HOY)
    assert set(r) == {propio.id}
    assert r[propio.id].origen == "sin_costo"


async def test_el_contrato_se_lee_de_la_compra_elegida(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    await _mov(
        db_session, p, "100", dias_atras=1, unit_cost_final=Decimal("131.00"),
        shipping_treatment="adicional", tax_treatment="no_aplica",
        tax_recoverable=None, document_ref="R-77", cost_contract_version=1,
    )
    r = (await costos_de_referencia(db_session, sample_tenant.tenant_id, [p.id], HOY))[p.id]
    assert r.costo_unitario == Decimal("131.00")
    assert (r.envio, r.impuesto, r.document_ref) == ("adicional", "no_aplica", "R-77")
    assert r.contrato_confirmado


async def test_sin_productos_no_consulta_nada(
    db_session: AsyncSession, sample_tenant: Tenant,
) -> None:
    assert await costos_de_referencia(db_session, sample_tenant.tenant_id, [], HOY) == {}
