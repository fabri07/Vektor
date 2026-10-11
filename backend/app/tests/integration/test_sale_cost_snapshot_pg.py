"""Integración PostgreSQL de M·2a: migración ``20261011_0001`` y costeo al commit.

Contra Postgres real porque se prueba la idempotencia de la migración, los CHECK
``NOT VALID`` → ``VALIDATE`` y que el listener de ``before_commit`` (SAVEPOINT +
consultas sincrónicas dentro del greenlet de ``AsyncSession``) funcione con
asyncpg, con el corte de fecha en ``timestamptz``. Todo dentro de UNA
transacción externa que se revierte. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "sales_entries"
_COLUMNAS = {
    "cost_unit_ars", "cost_ars", "cost_confirmed", "cost_origin", "cost_reason",
    "cost_snapshot_version",
}
_CHECKS = {
    "ck_sales_entries_cost_origin",
    "ck_sales_entries_cost_unit_ars",
    "ck_sales_entries_cost_ars",
    "ck_sales_entries_cost_snapshot_version",
}


def _load_migration() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence" / "migrations" / "versions" / "20261011_0001_sale_cost_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("_m2a_sale_cost_snapshot", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


def _upgrade(sync_conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        mig.upgrade()


def _downgrade(sync_conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        mig.downgrade()


def _columnas(sync_conn: Connection) -> set[str]:
    return {c["name"] for c in sa_inspect(sync_conn).get_columns(_TABLA)}


@contextlib.asynccontextmanager
async def _transaccion() -> AsyncIterator[AsyncConnection]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                if "cost_snapshot_version" not in await c.run_sync(_columnas):
                    pytest.skip("schema sin migrar (falta cost_snapshot_version)")
                yield c
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def _checks(c: AsyncConnection) -> dict[str, bool]:
    filas = await c.execute(
        text(
            "SELECT conname, convalidated FROM pg_constraint"
            " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
            " AND conname LIKE 'ck_sales_entries_cost%'"
        ),
        {"t": _TABLA},
    )
    return {r[0]: r[1] for r in filas}


async def test_upgrade_es_idempotente_y_el_downgrade_limpia() -> None:
    async with _transaccion() as conn:
        await conn.run_sync(_upgrade)  # sobre head: no-op
        await conn.run_sync(_downgrade)
        assert not (_COLUMNAS & await conn.run_sync(_columnas))
        assert not (_CHECKS & set(await _checks(conn)))

        await conn.run_sync(_upgrade)
        await conn.run_sync(_upgrade)
        assert await conn.run_sync(_columnas) >= _COLUMNAS
        checks = await _checks(conn)
        assert set(checks) >= _CHECKS
        assert all(checks[n] for n in _CHECKS)


@pytest.mark.parametrize(
    "malo",
    [
        {"cost_origin": "inventado"},
        {"cost_unit_ars": Decimal("-1")},
        {"cost_ars": Decimal("-1")},
        {"cost_snapshot_version": 0},
    ],
)
async def test_los_check_rechazan(malo: dict[str, Any]) -> None:
    from sqlalchemy import Table, insert  # noqa: PLC0415
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    async with _transaccion() as conn:
        tenant_id = uuid.uuid4()
        await conn.execute(
            insert(Tenant.__table__).values(  # type: ignore[arg-type]
                tenant_id=tenant_id, legal_name="T", display_name="T"
            )
        )
        tabla: Table = SaleEntry.__table__  # type: ignore[assignment]
        fila = {
            "id": uuid.uuid4(), "tenant_id": tenant_id, "amount": Decimal("1"), "quantity": 1,
            "transaction_date": datetime(2026, 10, 1), "payment_method": "cash",
            "provenance": "REAL", "has_user_edits": False, "custom_fields": {},
        }
        # Una fila válida pasa (NULL = sin costear).
        await conn.execute(insert(tabla).values(**fila))
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(insert(tabla).values(**{**fila, "id": uuid.uuid4(), **malo}))


async def test_el_commit_costea_contra_postgres() -> None:
    async with _transaccion() as conn:
        s = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        tenant_id = uuid.uuid4()
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(BusinessProfile(
            profile_id=uuid.uuid4(), tenant_id=tenant_id, vertical_code="decoracion_hogar",
            fiscal_condition="monotributo",
        ))
        p = Product(
            id=uuid.uuid4(), tenant_id=tenant_id, name="P", sale_price_ars=Decimal("200"),
            stock_units=10, provenance="REAL", custom_fields={},
        )
        s.add(p)
        await s.flush()
        hoy = today_ar()
        ayer = datetime.combine(hoy - timedelta(days=1), datetime.min.time())
        # Compra de AYER a las 23:30 hora AR, rotulada UTC (como las escribe M·1b):
        # tiene que ser elegible para la venta de hoy y no para la de anteayer.
        s.add(InventoryMovement(
            tenant_id=tenant_id, product_id=p.id, movement_type="purchase", qty=10,
            unit_cost=Decimal("100"), unit_cost_final=Decimal("100"),
            shipping_treatment="incluido", tax_treatment="no_aplica", cost_contract_version=1,
            occurred_at=(ayer + timedelta(hours=23, minutes=30)).replace(tzinfo=UTC),
        ))
        hoy_10 = datetime.combine(hoy, datetime.min.time()).replace(hour=10)
        venta = SaleEntry(
            tenant_id=tenant_id, product_id=p.id, amount=Decimal("400"), quantity=2,
            transaction_date=hoy_10, payment_method="cash",
        )
        vieja = SaleEntry(
            tenant_id=tenant_id, product_id=p.id, amount=Decimal("200"), quantity=1,
            transaction_date=ayer - timedelta(days=1), payment_method="cash",
        )
        s.add_all([venta, vieja])
        await s.commit()

        assert venta.cost_ars == Decimal("200.00")
        assert venta.cost_confirmed is True
        assert venta.cost_origin == "compra"
        assert vieja.cost_origin == "sin_costo"
        fila = (
            await conn.execute(
                text("SELECT cost_ars, cost_snapshot_version FROM sales_entries WHERE id = :i"),
                {"i": venta.id},
            )
        ).one()
        assert fila == (Decimal("200.00"), 1)
        await s.close()
