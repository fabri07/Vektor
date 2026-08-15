"""F-ID.8 — fusionar productos duplicados transfiere sus identificadores.

Extiende `product_dedup_service` (F3-T5, `_apply_one_group`) con un paso
puramente aditivo: los códigos externos del duplicado (`vektor_code` propio,
`business_code` capturado por F-ID.4/F-ID.7) no pueden perderse al
fusionarse — si el archivo que trajo ese código se re-importa, tiene que
seguir resolviendo, ahora contra el canónico. No toca stock/FKs/fingerprint;
mismos fixtures y patrón de siembra que `test_product_dedup_apply.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.services import product_dedup_service as svc
from app.application.services.entity_code_service import record_identifier
from app.application.services.inventory_movement_origin import (
    SOURCE_PURCHASE_IMPORT,
    SOURCE_RECEIPT,
)
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Mismo shadow que `test_product_dedup_apply.py` — el apply commitea por
    grupo (transacción propia), incompatible con el listener del `db_session`
    compartido de conftest."""
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


async def _add_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    sku: str | None = None,
    barcode: str | None = None,
    stock_units: int = 0,
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        barcode=barcode,
        sale_price_ars=Decimal("100.00"),
        stock_units=stock_units,
        custom_fields={},
        is_active=True,
    )
    session.add(p)
    await session.flush()
    return p


async def _add_movement(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, qty: int, source_type: str
) -> None:
    session.add(
        InventoryMovement(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product_id=product_id,
            movement_type="purchase",
            qty=qty,
            source_type=source_type,
        )
    )
    await session.flush()


async def _add_balance(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID, current_qty: int
) -> None:
    session.add(
        InventoryBalance(
            id=uuid.uuid4(), tenant_id=tenant_id, product_id=product_id, current_qty=current_qty
        )
    )
    await session.flush()


async def _seed_ledger_evidence(
    session: AsyncSession, tenant_id: uuid.UUID, canonical: Product, dup: Product
) -> None:
    """Movimientos+balance para que `classify_group_stock_decision` resuelva
    SUM automático (no REVIEW por falta de evidencia) — mismo patrón que
    `test_apply_simple_sum_group` en `test_product_dedup_apply.py`."""
    await _add_movement(
        session, tenant_id, canonical.id, canonical.stock_units, SOURCE_PURCHASE_IMPORT
    )
    await _add_movement(session, tenant_id, dup.id, dup.stock_units, SOURCE_RECEIPT)
    await _add_balance(session, tenant_id, canonical.id, canonical.stock_units)
    await _add_balance(session, tenant_id, dup.id, dup.stock_units)


async def _plan_and_persist(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    plan = await svc.plan_dedup(session, tenant_id)
    return await svc.persist_dedup_plan(session, tenant_id, plan)


async def _active_identifiers(
    session: AsyncSession, entity_id: uuid.UUID
) -> list[EntityIdentifier]:
    rows = await session.execute(
        select(EntityIdentifier).where(
            EntityIdentifier.entity_type == "product",
            EntityIdentifier.entity_id == entity_id,
            EntityIdentifier.revoked_at.is_(None),
        )
    )
    return list(rows.scalars().all())


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_business_code_del_duplicado_se_transfiere_al_canonico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Coca", barcode="7790011110001", stock_units=10
    )
    dup = await _add_product(
        db_session, tid, name="Coca 500", barcode="7790011110001", stock_units=7
    )
    await _seed_ledger_evidence(db_session, tid, canonical, dup)
    await record_identifier(
        db_session, tid, "product", dup.id, "business_code", "business", "ERP-42", "business"
    )
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "APPLIED"

    db_session.expunge_all()

    canonical_ids = await _active_identifiers(db_session, canonical.id)
    assert any(
        r.identifier_type == "business_code" and r.raw_value == "ERP-42" for r in canonical_ids
    )
    dup_ids = await _active_identifiers(db_session, dup.id)
    assert not any(r.identifier_type == "business_code" for r in dup_ids)


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revertir_el_merge_no_falla_aunque_el_identificador_quede_en_el_canonico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Deuda declarada verificada: T6 (`revert_dedup_run`) no participa de esta
    transferencia — reactivar el duplicado NO mueve su identificador de
    vuelta. Lo que este test prueba es que eso es un límite BENIGNO: el
    revert de por sí sigue funcionando (no crashea, no bloquea el grupo por
    esto), sólo el identificador queda apuntando al canónico."""
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Coca", barcode="7790011110001", stock_units=10
    )
    dup = await _add_product(
        db_session, tid, name="Coca 500", barcode="7790011110001", stock_units=7
    )
    await _seed_ledger_evidence(db_session, tid, canonical, dup)
    await record_identifier(
        db_session, tid, "product", dup.id, "business_code", "business", "ERP-42", "business"
    )
    await db_session.flush()

    plan = await svc.plan_dedup(db_session, tid)
    real_canonical_id = next(g.canonical_id for g in plan.groups if g.is_mergeable)
    source_run_id = await svc.persist_dedup_plan(db_session, tid, plan)
    applied = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert applied.status == "APPLIED"

    # Libera la clave fuerte del canónico para que el revert no rebote por
    # colisión de identidad (F5) al reactivar el duplicado — mismo patrón
    # que `_free_identities` en test_product_dedup_revert.py.
    real_canonical = await db_session.get(Product, real_canonical_id)
    assert real_canonical is not None
    real_canonical.barcode = None
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED"
    assert reverted.groups_reverted == 1

    db_session.expunge_all()
    # Límite documentado: el identificador SIGUE en el canónico, no volvió
    # al duplicado reactivado. No es un crash — es la deuda declarada.
    canonical_ids = await _active_identifiers(db_session, real_canonical_id)
    assert any(r.raw_value == "ERP-42" for r in canonical_ids)


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_vektor_code_propio_del_duplicado_tambien_se_transfiere(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El sku/vektor_code del duplicado (mirror de F-ID.4 en entity_identifiers)
    también se transfiere — no sólo business_code. Ninguno de los dos trae
    `sku` en la COLUMNA a propósito: `choose_canonical` desempata por
    `sku NOT NULL DESC`, y setearlo en el duplicado lo volvería canónico
    (invirtiendo roles) — acá sólo importa que la FILA de entity_identifiers
    se transfiera, no el valor de la columna."""
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Coca", barcode="7790011110001", stock_units=10
    )
    dup = await _add_product(
        db_session, tid, name="Coca 500", barcode="7790011110001", stock_units=7
    )
    await _seed_ledger_evidence(db_session, tid, canonical, dup)
    await record_identifier(
        db_session, tid, "product", dup.id, "sku", "business", "GEN-0042", "business"
    )
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "APPLIED"

    db_session.expunge_all()

    canonical_ids = await _active_identifiers(db_session, canonical.id)
    assert any(r.identifier_type == "sku" and r.raw_value == "GEN-0042" for r in canonical_ids)
