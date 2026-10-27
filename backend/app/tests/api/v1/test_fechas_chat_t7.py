"""T7 — el «hoy» del chat es el día ARGENTINO, no el del server (E0-A-1/2/3).

El server de producción corre en UTC: de 21 a 24 ART, `date.today()` ya es
mañana. Por chat, eso fechaba en el futuro:

- E0-A-1: la compra (el prompt le decía al LLM «Hoy es <mañana>»), y con B·5e
  la venta de HOY de ese producto quedaba sin respaldo y el POS la rechazaba.
- E0-A-2: el gasto o la salida de caja con entidades del CEO (default
  `date.today()`, además a las 00:00 y no a la hora real).
- E0-A-3: «sin rotación» comparaba el DATETIME de la venta contra la fecha
  pelada (`<= hoy 00:00`) y sacaba las ventas de hoy a cualquier hora.

No hay freezegun en el repo: `_reloj` fija el instante como lo vería el server
en UTC. Parchea el `datetime` de `business_time` (lo usan `today_ar()` y
`now_ar_naive()` de todos lados) y el `date` de los módulos bajo prueba, cuyo
`date.today()` devuelve el día UTC, como en Railway.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import unittest.mock
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import _extract_action_payload
from app.application.agents.expense.agent import AgentExpense
from app.application.agents.google.agent import AgentGoogle
from app.application.agents.income.agent import AgentIncome
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    RiskLevel,
)
from app.application.agents.supplier.agent import AgentSupplier
from app.domain.business_time import AR_TZ, today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.user import User
from app.tests._respaldo import producto_con_compra

# Un día AR DISTINTO del real (hace tres días): cualquier reloj que `_reloj` no
# alcance —p. ej. un `from datetime import date` dentro de una función— da otro
# día a cualquier hora y con cualquier TZ del server, y el test lo caza. Lo que
# mira el reloj de verdad (vencimientos, JWT) queda en hoy, que es posterior.
_DIA = today_ar() - timedelta(days=3)
_MANANA = _DIA + timedelta(days=1)
_AR_2230 = datetime(_DIA.year, _DIA.month, _DIA.day, 22, 30, tzinfo=AR_TZ)
_AR_1500 = datetime(_DIA.year, _DIA.month, _DIA.day, 15, 0, tzinfo=AR_TZ)

#: Módulos cuyo `date.today()` tiene que dar el día UTC (si todavía lo usan).
_MODULOS_CON_DATE = (
    "app.application.agents.supplier.agent",
    "app.application.agents.expense.agent",
    "app.application.services.cash_service",
    "app.api.v1.insights",
    "app.api.v1.agent",
    "app.application.agents.income.agent",
    "app.application.agents.google.agent",
)


@contextlib.contextmanager
def _reloj(instante: datetime) -> Iterator[None]:
    """El server en UTC en ``instante``: `datetime.now(tz)` y `date.today()`."""

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            if tz is None:
                return instante.astimezone(UTC).replace(tzinfo=None)
            return instante.astimezone(tz)

    class _Date(date):
        @classmethod
        def today(cls) -> Any:
            return instante.astimezone(UTC).date()

    with contextlib.ExitStack() as pila:
        pila.enter_context(
            unittest.mock.patch("app.domain.business_time.datetime", _Datetime)
        )
        for nombre in _MODULOS_CON_DATE:
            if hasattr(importlib.import_module(nombre), "date"):
                pila.enter_context(unittest.mock.patch(f"{nombre}.date", _Date))
        yield


def test_el_reloj_reproduce_el_server_en_utc() -> None:
    """Sanidad del andamio: a las 22:30 ART el día UTC ya es mañana."""
    insights = importlib.import_module("app.api.v1.insights")
    with _reloj(_AR_2230):
        assert today_ar() == _DIA
        assert insights.date.today() == _MANANA


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> Iterator[None]:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _llm_que_repite_el_hoy_del_prompt(**kw: Any) -> tuple[str, None]:
    """El LLM hace lo que el prompt le pide: si no se dice fecha, «la de hoy»."""
    hoy = re.search(r"Hoy es (\d{4}-\d{2}-\d{2})", kw["system"])
    assert hoy is not None
    return (
        '{"amount": "45000", "supplier_name": "Arcor", "product_name": "Alfajor T7",'
        ' "sku": null, "qty": 10, "unit_cost": null,'
        f' "transaction_date": "{hoy.group(1)}"}}',
        None,
    )


async def _pendiente(
    db: AsyncSession, tenant: Tenant, user: User, action_type: str, payload: dict[str, Any]
) -> PendingAction:
    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type=action_type,
        payload=payload,
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(action)
    await db.commit()
    return action


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        "/api/v1/pos/terminals", json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


# ── E0-A-1: compra por chat a las 22:30 ART ──────────────────────────────────


async def test_compra_por_chat_a_las_2230_queda_hoy_y_no_bloquea_la_venta_de_hoy(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    alfajor = Product(
        tenant_id=sample_tenant.tenant_id, name="Alfajor T7",
        sale_price_ars=Decimal("1500"), stock_units=0,
    )
    db_session.add(alfajor)
    await db_session.commit()

    with _reloj(_AR_2230):
        agente = AgentSupplier(session=db_session)
        agente._client = object()  # el LLM es `call_llm`, parcheado abajo
        with unittest.mock.patch(
            "app.application.agents.supplier.agent.call_llm",
            side_effect=_llm_que_repite_el_hoy_del_prompt,
        ):
            resp = await agente.process(AgentRequest(
                user_id=str(sample_user.user_id),
                business_id=str(sample_tenant.tenant_id),
                message="registrar compra de 10 Alfajor T7 a Arcor por $45000",
            ))
        assert resp.result["action_type"] == ActionType.REGISTER_PURCHASE, resp.result
        assert resp.result["payload"]["transaction_date"] == _DIA.isoformat()

        action = await _pendiente(
            db_session, sample_tenant, sample_user, "REGISTER_PURCHASE",
            _extract_action_payload(resp),
        )
        r = await client.post(f"/api/v1/agent/confirm/{action.id}", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json().get("execution_status") == "SUCCEEDED", r.json()

        gasto = (
            await db_session.execute(
                select(ExpenseEntry).where(ExpenseEntry.product_id == alfajor.id)
            )
        ).scalar_one()
        mov = (
            await db_session.execute(
                select(InventoryMovement).where(InventoryMovement.product_id == alfajor.id)
            )
        ).scalar_one()
        assert gasto.transaction_date.date() == _DIA
        assert mov.occurred_at is not None and mov.occurred_at.date() == _DIA

        # La venta de HOY de lo que se acaba de comprar entra (B·5e la respalda).
        caja = await _caja(client, auth_headers)
        venta = await client.post(
            "/api/v1/pos/operations",
            json={
                "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
                "operation_date": f"{_DIA.isoformat()}T22:35:00",
                "charged_at": f"{_DIA.isoformat()}T22:35:00",
                "items": [{"product_id": str(alfajor.id), "quantity": 1}],
                "tenders": [{"payment_method": "cash", "amount_ars": "1500.00"}],
            },
            headers=caja,
        )
    assert venta.status_code == 201, venta.text
    assert len((await db_session.execute(select(PosOperation))).scalars().all()) == 1


async def test_compra_por_chat_fechada_manana_se_rechaza_y_no_escribe_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    """La defensa: aunque el LLM (o el usuario) traiga mañana, no se persiste."""
    p = await producto_con_compra(db_session, sample_tenant.tenant_id)
    with _reloj(_AR_2230):
        action = await _pendiente(
            db_session, sample_tenant, sample_user, "REGISTER_PURCHASE",
            {"amount": 1600, "product_id": str(p.id), "qty": 1,
             "transaction_date": _MANANA.isoformat()},
        )
        r = await client.post(f"/api/v1/agent/confirm/{action.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert "posterior a hoy" in r.json()["message"]
    gastos = (
        await db_session.execute(select(ExpenseEntry).where(ExpenseEntry.product_id == p.id))
    ).scalars().all()
    assert gastos == []
    compras = (
        await db_session.execute(
            select(InventoryMovement).where(
                InventoryMovement.product_id == p.id,
                InventoryMovement.movement_type == "purchase",
            )
        )
    ).scalars().all()
    assert compras == []


# ── E0-A-2: gasto y salida de caja con entidades del CEO ─────────────────────


@pytest.mark.parametrize(
    ("tipo", "mensaje", "entidades"),
    [
        (ActionType.REGISTER_EXPENSE, "pagué 8000 de luz",
         {"amount": 8000, "category": "servicios", "description": "Luz"}),
        (ActionType.REGISTER_CASH_OUTFLOW, "le pagué 5000 al plomero",
         {"amount": 5000, "description": "Plomero"}),
    ],
)
async def test_gasto_por_chat_a_las_2230_queda_hoy_a_la_hora_real(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    tipo: ActionType,
    mensaje: str,
    entidades: dict[str, Any],
) -> None:
    with _reloj(_AR_2230):
        resp = await AgentExpense(db=db_session).process(
            AgentRequest(
                user_id=str(sample_user.user_id),
                business_id=str(sample_tenant.tenant_id),
                message=mensaje,
            ),
            SimpleNamespace(action_type=tipo, entities=entidades),
        )
        assert resp.result["action_type"] == tipo, resp.result
        action = await _pendiente(
            db_session, sample_tenant, sample_user, str(tipo), _extract_action_payload(resp)
        )
        r = await client.post(f"/api/v1/agent/confirm/{action.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json().get("execution_status") == "SUCCEEDED", r.json()
    (gasto,) = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert gasto.transaction_date == _AR_2230.replace(tzinfo=None)


# ── E0-A-3: «sin rotación» con una venta de hoy ──────────────────────────────


@pytest.mark.parametrize("instante", [_AR_1500, _AR_2230], ids=["15h", "22h30"])
async def test_una_venta_de_hoy_saca_al_producto_de_sin_rotacion(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    instante: datetime,
) -> None:
    tid = sample_tenant.tenant_id
    vendido = await producto_con_compra(db_session, tid, nombre="Vendido hoy", stock=10)
    await producto_con_compra(db_session, tid, nombre="Quieto", stock=10)
    hace_una_hora = (instante - timedelta(hours=1)).replace(tzinfo=None)
    db_session.add(SaleEntry(
        tenant_id=tid, product_id=vendido.id, amount=Decimal("1000"), quantity=1,
        transaction_date=hace_una_hora, payment_method="cash",
    ))
    await db_session.commit()

    with _reloj(instante):
        r = await client.get("/api/v1/insights/breakdown?days=30", headers=auth_headers)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    sin_rotacion = {p["name"] for p in cuerpo["no_rotation_products"]}
    assert "Vendido hoy" not in sin_rotacion
    assert "Quieto" in sin_rotacion
    assert cuerpo["no_rotation_count"] == 1
    assert cuerpo["to_date"] == _DIA.isoformat()


# ── El cupo diario del chat es del día AR (GET /agent/usage) ─────────────────


async def test_el_uso_del_chat_a_las_2230_es_el_de_hoy(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    fake_redis: Any,
) -> None:
    await fake_redis.set(f"rate:chat:{sample_tenant.tenant_id}:{_DIA}", "7")
    with _reloj(_AR_2230):
        r = await client.get("/api/v1/agent/usage", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["messages_today"] == 7


async def test_el_cupo_del_chat_vence_a_la_medianoche_ar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    fake_redis: Any,
) -> None:
    """El primer mensaje del día fija el vencimiento: fin del día AR, no del UTC."""
    vencimientos: list[tuple[str, datetime]] = []

    async def _espia(clave: str, cuando: datetime) -> bool:
        vencimientos.append((clave, cuando))
        return True

    respuesta = AgentResponse(
        request_id=str(uuid.uuid4()), agent_name="agent_chat", status="success",
        risk_level=RiskLevel.LOW, requires_approval=False, result={"summary": "ok"},
    )
    with (
        _reloj(_AR_2230),
        unittest.mock.patch("app.api.v1.agent.ChatOrchestrator") as orquestador,
        # El FakeRedis no tiene `expireat`: se agrega como espía.
        unittest.mock.patch.object(fake_redis, "expireat", side_effect=_espia, create=True),
    ):
        orquestador.return_value.handle = unittest.mock.AsyncMock(return_value=respuesta)
        r = await client.post(
            "/api/v1/agent/chat", json={"message": "hola"}, headers=auth_headers
        )
    assert r.status_code == 200, r.text
    clave = f"rate:chat:{sample_tenant.tenant_id}:{_DIA}"
    assert vencimientos == [(clave, datetime.combine(_DIA, time.max, tzinfo=AR_TZ))]


# ── E0-A-13: ventanas de 30 días del agente de ingresos ──────────────────────


@pytest.mark.parametrize(
    ("tipo", "intent"),
    [
        (ActionType.ANALYZE_SALES_DATA, "detectar_productos_estrella"),
        (ActionType.ANSWER_DATA_QUERY, None),
    ],
)
async def test_las_ventanas_del_agente_de_ingresos_terminan_hoy_ar(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    tipo: ActionType,
    intent: str | None,
) -> None:
    repo = "app.persistence.repositories.transaction_repository.SaleRepository"
    with (
        _reloj(_AR_2230),
        unittest.mock.patch(f"{repo}.get_sales_by_product", return_value=[]) as por_producto,
        unittest.mock.patch(f"{repo}.get_sales_by_customer", return_value=[]),
    ):
        await AgentIncome(db=db_session).process(
            AgentRequest(
                user_id=str(sample_user.user_id),
                business_id=str(sample_tenant.tenant_id),
                message="¿qué productos se venden más?",
            ),
            SimpleNamespace(action_type=tipo, entities={"_intent": intent} if intent else {}),
        )
    (_tid, desde, hasta) = por_producto.call_args.args
    assert (desde, hasta) == (_DIA - timedelta(days=30), _DIA)


# ── E0-A-4: el «hoy» del asistente de Calendar ───────────────────────────────


async def test_el_prompt_de_calendar_dice_hoy_ar() -> None:
    sistemas: list[str] = []

    async def _llm(**kw: Any) -> tuple[None, None]:
        sistemas.append(kw["system"])
        return None, None

    agente = AgentGoogle()
    agente._client = object()  # el LLM es `call_llm`, parcheado abajo
    with (
        _reloj(_AR_2230),
        unittest.mock.patch("app.application.agents.google.agent.call_llm", side_effect=_llm),
    ):
        await agente._extract_calendar_event_data("agendame mañana a las 10 con el proveedor")
    assert sistemas and sistemas[0].startswith(f"Hoy es {_DIA.isoformat()}.")
