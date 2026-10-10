"""Integración PostgreSQL de la migración ``20261010_0001`` (M·1a, contrato de costo).

Contra Postgres real porque lo que se prueba es la idempotencia (presencia antes
de crear), los CHECK con el patrón ``NOT VALID`` → ``VALIDATE`` y el índice
parcial; SQLite no corre la migración e ignora los CHECK.

Todo pasa dentro de UNA transacción que se revierte al final. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import Connection, Table, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "inventory_movements"
_INDICE = "ix_inventory_movements_live_purchases"
_COLUMNAS = {
    "unit_cost_final", "shipping_treatment", "tax_treatment", "tax_rate", "tax_amount",
    "tax_recoverable", "document_ref", "cost_contract_version",
}
_CHECKS = {
    "ck_inventory_movements_shipping_treatment",
    "ck_inventory_movements_tax_treatment",
    "ck_inventory_movements_tax_rate",
    "ck_inventory_movements_tax_amount",
    "ck_inventory_movements_unit_cost_final",
    "ck_inventory_movements_cost_contract_version",
}


def _load_migration() -> Any:
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20261010_0001_purchase_cost_contract.py"
    )
    spec = importlib.util.spec_from_file_location("_m1a_purchase_cost_contract", ruta)
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
                if "unit_cost_final" not in await c.run_sync(_columnas):
                    pytest.skip("schema sin migrar (falta unit_cost_final): alembic upgrade head")
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
            " AND conname LIKE 'ck_inventory_movements_%'"
        ),
        {"t": _TABLA},
    )
    return {r[0]: r[1] for r in filas}


async def _predicado_del_indice(c: AsyncConnection) -> str | None:
    fila = (
        await c.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i"),
            {"t": _TABLA, "i": _INDICE},
        )
    ).first()
    return None if fila is None else str(fila[0])


async def test_upgrade_es_idempotente_y_el_downgrade_limpia() -> None:
    async with _transaccion() as conn:
        await conn.run_sync(_upgrade)  # sobre head: no-op
        await conn.run_sync(_downgrade)
        assert not (_COLUMNAS & await conn.run_sync(_columnas))
        assert not (_CHECKS & set(await _checks(conn)))
        assert await _predicado_del_indice(conn) is None

        await conn.run_sync(_upgrade)
        await conn.run_sync(_upgrade)
        assert await conn.run_sync(_columnas) >= _COLUMNAS
        checks = await _checks(conn)
        assert set(checks) >= _CHECKS
        # VALIDATE corrió: ninguna quedó NOT VALID.
        assert all(checks[n] for n in _CHECKS)
        indexdef = await _predicado_del_indice(conn)
        assert indexdef is not None
        assert "movement_type" in indexdef and "voided_at IS NULL" in indexdef


async def _base(conn: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    s = AsyncSession(bind=conn)
    s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
    await s.flush()
    product_id = uuid.uuid4()
    s.add(Product(
        id=product_id, tenant_id=tenant_id, name="P", sale_price_ars=Decimal("10"),
        stock_units=0, provenance="REAL", custom_fields={},
    ))
    await s.flush()
    return tenant_id, product_id


def _fila(tenant_id: uuid.UUID, product_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(), "tenant_id": tenant_id, "product_id": product_id,
        "movement_type": "purchase", "qty": 1, "unit_cost": Decimal("100"),
        "source_type": "purchase_import", **extra,
    }


async def test_null_es_desconocido_y_pasa() -> None:
    async with _transaccion() as conn:
        tenant_id, product_id = await _base(conn)
        tabla = cast(Table, InventoryMovement.__table__)
        await conn.execute(insert(tabla).values(**_fila(tenant_id, product_id)))
        await conn.execute(insert(tabla).values(**_fila(
            tenant_id, product_id, unit_cost_final=Decimal("131"),
            shipping_treatment="incluido", tax_treatment="no_aplica",
            tax_rate=Decimal("0.21"), tax_amount=Decimal("0"), tax_recoverable=False,
            document_ref="A-1", cost_contract_version=1,
        )))


@pytest.mark.parametrize(
    "malo",
    [
        {"shipping_treatment": "gratis"},
        {"tax_treatment": "sí"},
        {"tax_rate": Decimal("-0.01")},
        {"tax_rate": Decimal("1")},
        {"tax_amount": Decimal("-1")},
        {"unit_cost_final": Decimal("-1")},
        {"cost_contract_version": 0},
    ],
)
async def test_los_check_rechazan(malo: dict[str, Any]) -> None:
    async with _transaccion() as conn:
        tenant_id, product_id = await _base(conn)
        tabla = cast(Table, InventoryMovement.__table__)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(insert(tabla).values(**_fila(tenant_id, product_id, **malo)))
