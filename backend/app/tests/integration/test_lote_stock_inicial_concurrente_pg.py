"""B·5c: el lote de stock inicial contra otra alta simultánea — contra Postgres.

El nombre admite homónimos: ningún índice único impide que, entre que el lote
calcula sus coincidencias y las inserta, otra sesión cree un «Vela». Lo frena el
lock de alta: todo alta toma el COMPARTIDO al insertar un producto (listener de
``before_flush``) y el lote pide el EXCLUSIVO sin esperar.

- Una alta en vuelo (con su producto ya escrito, sin confirmar) → el lote da 409
  ``PRODUCTS_BEING_CREATED`` y no escribe nada; confirmada el alta, el token
  viejo da 409 ``BATCH_CHANGED``: el usuario ve el homónimo antes de elegir.
- Un lote en vuelo → el alta espera a que termine (no se cuela en el medio).

En SQLite los advisory locks no existen y la suite pasaría sin probar nada.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.v1 import products as rutas
from app.application.services import stock_inicial_lote_service
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.product import FilaStockInicial, LoteStockInicialRequest
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4235 + 0xC  # "VEKTORB5" + c


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


_Escenario = tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID]


@pytest_asyncio.fixture
async def escenario(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[_Escenario, None]:
    # Sin broker: el recálculo del score después del commit no importa acá.
    monkeypatch.setattr(rutas, "trigger_score_recalculation_after_commit", lambda *a, **k: None)
    fabrica = async_sessionmaker(pg_engine, expire_on_commit=False)
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="B5c", display_name="B5c"))
        await s.flush()
        s.add(
            User(
                user_id=uid, tenant_id=tid, email=f"{uid}@b5c.test", full_name="Dueña",
                password_hash="x", role_code="OWNER", is_active=True,
            )
        )
        await s.commit()
    try:
        yield fabrica, tid, uid
    finally:
        async with fabrica() as s:
            for modelo in (
                DecisionAuditLog, ProductCostHistory, ProductCostState, InventoryMovement,
                InventoryBalance, Product, User,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


_FILAS = [
    FilaStockInicial(
        nombre="Vela", sale_price_ars=Decimal("1000"), unit_cost_ars=Decimal("400"),
        stock_units=10,
    )
]


async def _token(fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> str:
    async with fabrica() as s:
        analisis = await stock_inicial_lote_service.analizar(s, tid, _FILAS)
        return stock_inicial_lote_service.token(_FILAS, analisis)


def _body(token: str) -> LoteStockInicialRequest:
    hace_10 = today_ar() - timedelta(days=10)
    return LoteStockInicialRequest(
        filas=_FILAS,
        preview_token=token,
        fecha=datetime(hace_10.year, hace_10.month, hace_10.day, 9),
    )


async def _lote(s: AsyncSession, tid: uuid.UUID, uid: uuid.UUID, token: str) -> Any:
    tenant = await s.get(Tenant, tid)
    user = await s.get(User, uid)
    assert tenant is not None and user is not None
    return await rutas.confirm_initial_stock_batch(
        _body(token), tenant=tenant, user=user, _maintenance_guard=None, session=s,
        idempotency_key=None,
    )


async def _cantidad(fabrica: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> int:
    async with fabrica() as s:
        return int(
            (
                await s.execute(select(func.count(Product.id)).where(Product.tenant_id == tid))
            ).scalar_one()
        )


async def test_un_alta_en_vuelo_frena_el_lote_y_despues_cambia_el_token(
    escenario: _Escenario,
) -> None:
    fabrica, tid, uid = escenario
    token = await _token(fabrica, tid)

    async with fabrica() as alta:
        alta.add(Product(tenant_id=tid, name="VELA", sale_price_ars=Decimal("900")))
        await alta.flush()  # el listener toma el lock compartido acá

        async with fabrica() as lote:
            with pytest.raises(HTTPException) as exc:
                await _lote(lote, tid, uid, token)
            await lote.rollback()
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "PRODUCTS_BEING_CREATED"  # type: ignore[index]
        await alta.commit()

    async with fabrica() as lote:
        with pytest.raises(HTTPException) as exc:
            await _lote(lote, tid, uid, token)
        await lote.rollback()
    assert exc.value.detail["code"] == "BATCH_CHANGED"  # type: ignore[index]
    assert await _cantidad(fabrica, tid) == 1


async def test_un_lote_en_vuelo_hace_esperar_al_alta(escenario: _Escenario) -> None:
    fabrica, tid, uid = escenario
    token = await _token(fabrica, tid)
    async with fabrica() as lote:
        r = await _lote(lote, tid, uid, token)
        assert r.creados == 1

        async def alta() -> None:
            async with fabrica() as s:
                s.add(Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("900")))
                await s.commit()

        tarea = asyncio.create_task(alta())
        await asyncio.sleep(1.0)
        assert not tarea.done(), "el alta se coló mientras el lote no había confirmado"
        await lote.commit()
    await asyncio.wait_for(tarea, timeout=10)
    # El homónimo de después es una decisión de esa alta, no del lote.
    assert await _cantidad(fabrica, tid) == 2
