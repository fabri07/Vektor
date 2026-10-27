"""V-1 — los eventos de stock y el recálculo del score salen DESPUÉS del commit.

``stock_service`` emitía ``STOCK_DECREASED`` / ``STOCK_ALERT_CREATED`` /
``STOCK_INCREASED`` con ``EventBus.emit`` inmediato, en el medio de la
transacción. Sus handlers (``jobs/stock_tasks.py``) encolan el recálculo del
score, y el worker lee la base con otra sesión: calculaba sin la venta, o con una
que después hacía rollback.

Además el mecanismo diferido (``emit_after_commit`` y
``trigger_score_recalculation_after_commit``) colgaba un ``after_commit`` con
``once=True``, y SQLAlchemy despacha ``after_commit`` también al LIBERAR un
savepoint: el primer ``guarded_savepoint`` posterior lo consumía con la
transacción todavía abierta. Y un rollback no lo borraba.

Los tests usan una sesión que commitea de verdad (``db_session.commit()``) o un
cliente que commitea como ``get_db_session``: el ``client`` de la suite no dispara
``after_commit`` y con él no habría nada que medir.
"""

from __future__ import annotations

import unittest.mock
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.services import stock_service
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.domain.business_time import today_ar
from app.main import create_app
from app.persistence.db.despues_del_commit import al_confirmar
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.tests._respaldo import producto_con_compra, producto_con_compra_http

pytestmark = pytest.mark.asyncio


def _abierta(session: AsyncSession) -> bool:
    """¿La transacción de nivel superior sigue activa? En el ``after_commit`` real
    ya está COMMITTED; al liberar un savepoint, sigue activa."""
    tx = session.sync_session.get_transaction()
    return tx is not None and tx.is_active


@dataclass
class Emitido:
    evento: str
    transaccion_abierta: bool


@pytest.fixture
def emitidos(db_session: AsyncSession) -> Generator[list[Emitido], None, None]:
    """Cada ``EventBus.emit``, con el estado de la transacción en ese momento."""
    lista: list[Emitido] = []

    def _anotar(event_type: str, _payload: dict[str, Any]) -> None:
        lista.append(Emitido(event_type, _abierta(db_session)))

    with unittest.mock.patch.object(EventBus, "emit", staticmethod(_anotar)):
        yield lista


# ── El mecanismo: commit real, savepoints y rollback ───────────────────────────
async def test_liberar_un_savepoint_no_es_el_commit(db_session: AsyncSession) -> None:
    corridas: list[bool] = []
    al_confirmar(db_session, lambda: corridas.append(_abierta(db_session)))

    async with db_session.begin_nested():
        pass
    assert corridas == [], "corrió al liberar el savepoint, con la transacción abierta"

    await db_session.commit()
    assert corridas == [False]

    # Una sola vez: el commit siguiente no la repite.
    await db_session.commit()
    assert corridas == [False]


async def test_con_rollback_no_corre_ni_en_el_commit_siguiente(
    db_session: AsyncSession,
) -> None:
    corridas: list[str] = []
    al_confirmar(db_session, lambda: corridas.append("corrió"))

    await db_session.rollback()
    await db_session.commit()
    assert corridas == []


async def test_el_rollback_de_un_savepoint_descarta_solo_lo_de_adentro(
    db_session: AsyncSession,
) -> None:
    corridas: list[str] = []
    al_confirmar(db_session, lambda: corridas.append("afuera"))

    savepoint = await db_session.begin_nested()
    al_confirmar(db_session, lambda: corridas.append("adentro"))
    await savepoint.rollback()

    await db_session.commit()
    assert corridas == ["afuera"]


async def test_el_score_no_se_encola_al_liberar_un_savepoint(
    db_session: AsyncSession, mock_score_trigger: MagicMock
) -> None:
    trigger_score_recalculation_after_commit(db_session, "t-1", "prueba")
    async with db_session.begin_nested():
        pass
    mock_score_trigger.assert_not_called()

    await db_session.commit()
    mock_score_trigger.assert_called_once_with("t-1", "prueba")


async def test_el_score_no_se_encola_si_hay_rollback(
    db_session: AsyncSession, mock_score_trigger: MagicMock
) -> None:
    trigger_score_recalculation_after_commit(db_session, "t-1", "prueba")
    await db_session.rollback()
    await db_session.commit()
    mock_score_trigger.assert_not_called()


# ── Los eventos de stock ───────────────────────────────────────────────────────
async def test_descontar_stock_emite_despues_del_commit(
    db_session: AsyncSession, sample_tenant: Tenant, emitidos: list[Emitido]
) -> None:
    # Stock 1 → queda en 0: también sale la alerta.
    producto = await producto_con_compra(db_session, sample_tenant.tenant_id, stock=1)
    emitidos.clear()

    await stock_service.decrement_stock(
        producto.id, sample_tenant.tenant_id, 1, "prueba:v1", db_session
    )
    async with db_session.begin_nested():
        pass
    assert emitidos == [], "el evento salió con la transacción abierta"

    await db_session.commit()
    assert emitidos == [
        Emitido("STOCK_DECREASED", False),
        Emitido("STOCK_ALERT_CREATED", False),
    ]


async def test_descontar_stock_con_rollback_no_emite(
    db_session: AsyncSession, sample_tenant: Tenant, emitidos: list[Emitido]
) -> None:
    producto = await producto_con_compra(db_session, sample_tenant.tenant_id, stock=1)
    emitidos.clear()

    await stock_service.decrement_stock(
        producto.id, sample_tenant.tenant_id, 1, "prueba:v1", db_session
    )
    await db_session.rollback()
    await db_session.commit()
    assert emitidos == []


async def test_sumar_stock_emite_despues_del_commit(
    db_session: AsyncSession, sample_tenant: Tenant, emitidos: list[Emitido]
) -> None:
    producto = await producto_con_compra(db_session, sample_tenant.tenant_id, stock=1)
    emitidos.clear()

    await stock_service.increment_stock(
        producto.id, sample_tenant.tenant_id, 5, None, "prueba:v1", db_session
    )
    assert emitidos == []

    await db_session.commit()
    assert emitidos == [Emitido("STOCK_INCREASED", False)]


async def test_sumar_stock_con_rollback_no_emite(
    db_session: AsyncSession, sample_tenant: Tenant, emitidos: list[Emitido]
) -> None:
    producto = await producto_con_compra(db_session, sample_tenant.tenant_id, stock=1)
    emitidos.clear()

    await stock_service.increment_stock(
        producto.id, sample_tenant.tenant_id, 5, None, "prueba:v1", db_session
    )
    await db_session.rollback()
    await db_session.commit()
    assert emitidos == []


async def test_el_descuento_por_lote_emite_despues_del_commit(
    db_session: AsyncSession, sample_tenant: Tenant, emitidos: list[Emitido]
) -> None:
    producto = await producto_con_compra(db_session, sample_tenant.tenant_id, stock=10)
    emitidos.clear()

    await stock_service.decrement_stock_bulk(
        sample_tenant.tenant_id,
        [
            stock_service.BulkDecrementItem(
                product=producto, qty=2, source_event_id="prueba:v1-lote", occurred_at=None
            )
        ],
        db_session,
    )
    assert emitidos == []

    await db_session.commit()
    assert [e.evento for e in emitidos] == ["STOCK_DECREASED"]
    assert not emitidos[0].transaccion_abierta


# ── Por HTTP: una venta con producto ───────────────────────────────────────────
@pytest_asyncio.fixture
async def cliente_que_commitea(
    db_session: AsyncSession, fake_redis: Any, mock_score_trigger: MagicMock
) -> AsyncGenerator[AsyncClient, None]:
    """Como ``client``, pero la sesión commitea al salir, igual que ``get_db_session``."""
    from app.main import limiter  # noqa: PLC0415
    from app.persistence.db.redis_client import get_redis  # noqa: PLC0415

    limiter._storage.reset()
    app = create_app()

    async def _sesion() -> AsyncGenerator[AsyncSession, None]:
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    async def _redis() -> AsyncGenerator[Any, None]:
        yield fake_redis

    app.dependency_overrides[get_db_session] = _sesion
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_una_venta_por_http_emite_el_descuento_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    emitidos: list[Emitido],
) -> None:
    p1 = await producto_con_compra_http(cliente_que_commitea, auth_headers, nombre="Yerba")
    p2 = await producto_con_compra_http(cliente_que_commitea, auth_headers, nombre="Fideos")
    emitidos.clear()

    resp = await cliente_que_commitea.post(
        "/api/v1/sales/manual-batch",
        json={
            "payment_method": "cash",
            "transaction_date": str(today_ar()),
            "items": [
                {"product_id": p1, "quantity": 2, "unit_price": "1000.00"},
                {"product_id": p2, "quantity": 1, "unit_price": "1000.00"},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    descuentos = [e for e in emitidos if e.evento == "STOCK_DECREASED"]
    assert len(descuentos) == 2, emitidos
    assert not any(e.transaccion_abierta for e in emitidos), (
        "un evento de stock salió con la transacción del request abierta: el "
        "recálculo del score puede leer la base sin la venta"
    )
