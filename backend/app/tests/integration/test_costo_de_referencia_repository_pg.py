"""M·1a — el repositorio del costo de referencia contra PostgreSQL real.

SQLite no prueba lo que importa acá: ``row_number`` y el corte sobre
``timestamptz``, el desempate por ``uuid`` y que el resultado no dependa del
``TimeZone`` de la sesión. Todo dentro de una transacción que se revierte.
Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.costo_de_referencia_repository import costos_de_referencia

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

CORTE = date(2026, 9, 15)


@contextlib.asynccontextmanager
async def _sesion(zona: str = "UTC") -> AsyncIterator[AsyncSession]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                await c.execute(text(f"SET LOCAL TIME ZONE '{zona}'"))
                yield AsyncSession(bind=c)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def _producto(s: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
    await s.flush()
    pid = uuid.uuid4()
    s.add(Product(id=pid, tenant_id=tenant_id, name="P", sale_price_ars=Decimal("10"),
                  stock_units=0, provenance="REAL", custom_fields={}))
    await s.flush()
    return tenant_id, pid


def _compra(
    tenant_id: uuid.UUID, pid: uuid.UUID, costo: str, occurred_at: datetime,
    created_at: datetime, mid: uuid.UUID | None = None,
) -> InventoryMovement:
    return InventoryMovement(
        id=mid or uuid.uuid4(), tenant_id=tenant_id, product_id=pid,
        movement_type="purchase", qty=1, unit_cost=Decimal(costo),
        occurred_at=occurred_at, created_at=created_at, source_type="purchase_import",
    )


async def test_empate_total_desempata_por_id_igual_que_el_dominio() -> None:
    async with _sesion() as s:
        tenant_id, pid = await _producto(s)
        cuando = datetime(2026, 9, 10, 12, tzinfo=UTC)
        bajo = uuid.UUID("00000000-0000-4000-8000-000000000001")
        alto = uuid.UUID("ffffffff-0000-4000-8000-000000000001")
        s.add_all([
            _compra(tenant_id, pid, "100", cuando, cuando, bajo),
            _compra(tenant_id, pid, "200", cuando, cuando, alto),
        ])
        await s.flush()
        r = (await costos_de_referencia(s, tenant_id, [pid], CORTE))[pid]
        assert (r.movement_id, r.costo_unitario) == (alto, Decimal("200"))


async def test_empate_de_fecha_gana_la_cargada_despues() -> None:
    async with _sesion() as s:
        tenant_id, pid = await _producto(s)
        dia = datetime(2026, 9, 10, 12, tzinfo=UTC)
        s.add_all([
            _compra(tenant_id, pid, "100", dia, datetime(2026, 9, 11, 9, tzinfo=UTC)),
            _compra(tenant_id, pid, "110", dia, datetime(2026, 9, 11, 10, tzinfo=UTC)),
        ])
        await s.flush()
        r = (await costos_de_referencia(s, tenant_id, [pid], CORTE))[pid]
        assert r.costo_unitario == Decimal("110")


@pytest.mark.parametrize("zona", ["UTC", "America/Argentina/Buenos_Aires", "Asia/Tokyo"])
async def test_el_corte_no_depende_del_time_zone_de_la_sesion(zona: str) -> None:
    async with _sesion(zona) as s:
        tenant_id, pid = await _producto(s)
        carga = datetime(2026, 9, 20, tzinfo=UTC)
        s.add_all([
            # Último instante del día del corte (en UTC): entra.
            _compra(tenant_id, pid, "100", datetime(2026, 9, 15, 23, 30, tzinfo=UTC), carga),
            # Primer instante del día siguiente: no entra, aunque en la zona de
            # la sesión pudiera caer el 15.
            _compra(tenant_id, pid, "999", datetime(2026, 9, 16, 0, 30, tzinfo=UTC), carga),
        ])
        await s.flush()
        r = (await costos_de_referencia(s, tenant_id, [pid], CORTE))[pid]
        assert r.costo_unitario == Decimal("100")
