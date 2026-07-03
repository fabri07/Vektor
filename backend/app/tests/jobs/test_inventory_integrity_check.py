"""Tests del job inventory_integrity_check (fase 2). Todos llaman una versión
parcheada de ``_run()`` que inyecta la sesión de test — mismo patrón que
``test_recalculate_health_score.py`` (el job real crea su propio engine desde
DATABASE_URL, así que se reimplementa el cuerpo contra la sesión de test)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.persistence.db.base import Base
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.notification import Notification
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncSession:  # type: ignore[misc]
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


def _make_run(session: AsyncSession):
    """Versión parcheada de ``_run()`` que inyecta la sesión de test en vez de
    crear su propio engine desde DATABASE_URL."""
    import app.jobs.inventory_integrity_check as job_module  # noqa: PLC0415
    from app.application.services.inventory_integrity_service import (  # noqa: PLC0415
        check_tenant_inventory_integrity,
    )

    async def _patched_run(tenant_id_str: str) -> None:
        tenant_id = uuid.UUID(tenant_id_str)
        result = await check_tenant_inventory_integrity(session, tenant_id)
        divergences = result["divergences"]
        if not divergences:
            return

        now = datetime.now(UTC)
        body = "\n".join(
            f"- {d['product_name']}: sistema muestra {d['stock_units']}, "
            f"esperado {d['stock_esperado']} (diferencia {d['diff']})"
            for d in divergences
        )
        title = f"Posible inconsistencia de stock en {len(divergences)} producto(s)"

        owner_result = await session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.role_code == "OWNER",
                User.is_active.is_(True),
            )
        )
        for owner in owner_result.scalars().all():
            session.add(
                Notification(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=owner.user_id,
                    title=title,
                    body=body,
                    notification_type=job_module._DECISION_TYPE,
                    channel="in_app",
                    is_read=False,
                    metadata_={"divergences": divergences},
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                decision_type=job_module._DECISION_TYPE,
                decision_data={
                    "divergences": divergences,
                    "skipped_no_anchor": result["skipped_no_anchor"],
                    "skipped_complex_ledger": result["skipped_complex_ledger"],
                    "threshold": result["threshold"],
                },
                triggered_by=job_module._TRIGGERED_BY,
                actor_user_id=None,
                created_at=now,
            )
        )
        await session.commit()

    return _patched_run


async def _tenant(session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Test PYME",
        display_name="Test PYME",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    session.add(t)
    await session.flush()
    return t


async def _owner(session: AsyncSession, tenant_id: uuid.UUID) -> User:
    u = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="owner@test.com",
        full_name="Owner",
        password_hash="x",
        role_code="OWNER",
        is_active=True,
    )
    session.add(u)
    await session.flush()
    return u


async def test_no_divergences_persists_nothing(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    await _owner(session, tenant.tenant_id)
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Producto sano",
        sale_price_ars=Decimal("100"),
        stock_units=36,
    )
    session.add(product)
    await session.flush()
    session.add(
        InventoryMovement(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            movement_type="adjustment",
            qty=36,
            source_type=SOURCE_CATALOG_INITIAL_STOCK,
        )
    )
    await session.commit()

    run = _make_run(session)
    await run(str(tenant.tenant_id))

    notifications = (await session.execute(select(Notification))).scalars().all()
    audits = (await session.execute(select(DecisionAuditLog))).scalars().all()
    assert notifications == []
    assert audits == []


async def test_divergence_creates_notification_and_audit(session: AsyncSession) -> None:
    tenant = await _tenant(session)
    owner = await _owner(session, tenant.tenant_id)
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca Cola 1.5L",
        sale_price_ars=Decimal("2500"),
        stock_units=184,
    )
    session.add(product)
    await session.flush()
    session.add(
        InventoryMovement(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            movement_type="adjustment",
            qty=36,
            source_type=SOURCE_CATALOG_INITIAL_STOCK,
        )
    )
    session.add(
        InventoryMovement(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            movement_type="purchase",
            qty=217,
            source_type="purchase_import",
        )
    )
    session.add(
        SaleEntry(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            amount=Decimal("100"),
            quantity=249,
            transaction_date=datetime(2026, 6, 1),
        )
    )
    await session.commit()

    run = _make_run(session)
    await run(str(tenant.tenant_id))

    notifications = (await session.execute(select(Notification))).scalars().all()
    audits = (await session.execute(select(DecisionAuditLog))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].user_id == owner.user_id
    assert notifications[0].notification_type == "INVENTORY_INTEGRITY_DIVERGENCE"
    assert len(audits) == 1
    assert audits[0].decision_type == "INVENTORY_INTEGRITY_DIVERGENCE"
    assert audits[0].decision_data["divergences"][0]["stock_esperado"] == 4
    # Nunca escribe stock_units.
    await session.refresh(product)
    assert product.stock_units == 184
