"""B·8a — fusionar duplicados mueve sus alias al canónico, y la reversa los devuelve.

Mismo patrón de siembra que ``test_product_dedup_external_code_b3.py``.
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
from app.persistence.models.product import Product
from app.persistence.models.product_alias import ProductAlias
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


async def _producto(session: AsyncSession, tid: uuid.UUID, **campos: object) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tid,
        sale_price_ars=Decimal("100.00"),
        stock_units=0,
        custom_fields={},
        is_active=True,
        **campos,
    )
    session.add(p)
    await session.flush()
    return p


async def _alias(session: AsyncSession, tid: uuid.UUID, pid: uuid.UUID, texto: str) -> uuid.UUID:
    from app.domain.text_norm import normalize_product_name

    a = ProductAlias(
        tenant_id=tid, product_id=pid, alias=texto, alias_normalizado=normalize_product_name(texto)
    )
    session.add(a)
    await session.flush()
    return a.id


async def _duenio(session: AsyncSession, alias_id: uuid.UUID) -> uuid.UUID:
    session.expunge_all()
    return (
        await session.execute(select(ProductAlias.product_id).where(ProductAlias.id == alias_id))
    ).scalar_one()


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_la_fusion_mueve_los_alias_y_la_reversa_los_devuelve(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Yerba", sku="YERBA1", barcode="7790088880008"
    )
    dup = await _producto(db_session, tid, name="Yerba x", sku="YERBA1")
    del_canonico = await _alias(db_session, tid, canonico.id, "yerba suelta")
    del_dup = await _alias(db_session, tid, dup.id, "yerba 1kg")
    await db_session.commit()

    plan = await svc.plan_dedup(db_session, tid)
    source_run_id = await svc.persist_dedup_plan(db_session, tid, plan)
    applied = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert applied.status == "APPLIED"

    assert await _duenio(db_session, del_dup) == canonico.id
    assert await _duenio(db_session, del_canonico) == canonico.id
    merge = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == applied.run_id, DataRepairItem.action == "MERGE_PRODUCT"
            )
        )
    ).scalar_one()
    assert merge.after_json is not None
    assert merge.after_json["aliases_moved"] == [
        {"id": str(del_dup), "from_product_id": str(dup.id)}
    ]

    # La reversa automática rebota por la clave que formó el grupo: se libera a mano.
    c = await db_session.get(Product, canonico.id)
    assert c is not None
    c.sku = None
    c.barcode = None
    await db_session.commit()
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results

    assert await _duenio(db_session, del_dup) == dup.id
    assert await _duenio(db_session, del_canonico) == canonico.id
