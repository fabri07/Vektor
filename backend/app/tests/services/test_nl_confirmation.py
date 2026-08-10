"""Tests del router de confirmación por lenguaje natural (Workstream C2).

Cubre:
- `_parse_bool_es`: afirmación / negación / ninguna.
- `_try_nl_confirmation`: "sí" confirma acción única; "sí" sin pending no ejecuta;
  múltiples pending (grupo) → ejecuta el grupo; VIEWER no confirma por texto;
  acción vencida no se ejecuta por texto; negación cancela.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import ChatRequest, _parse_bool_es, _try_nl_confirmation
from app.application.agents.shared.schemas import ActionType
from app.application.services.conversation_service import ConversationService
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.user import User


class FakeRedis:
    """Redis mínimo en memoria (get/setex) para ConversationService."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self._store[key] = value

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ── _parse_bool_es ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", ["sí", "si", "dale", "ok", "confirmo", "hacelo", "ejecutá"])
def test_parse_bool_affirmations(msg: str) -> None:
    assert _parse_bool_es(msg) is True


@pytest.mark.parametrize("msg", ["no", "cancelá", "rechazo", "mejor no", "nop"])
def test_parse_bool_negations(msg: str) -> None:
    assert _parse_bool_es(msg) is False


@pytest.mark.parametrize(
    "msg",
    ["registrá una venta de 1200", "mostrame los gastos", "cuánto gané ayer", ""],
)
def test_parse_bool_neither(msg: str) -> None:
    assert _parse_bool_es(msg) is None


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_pending(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
    group_id: str | None = None,
) -> PendingAction:
    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = tenant_id
    action.user_id = user_id
    action.action_type = ActionType.RECLASSIFY_EXPENSE
    action.payload = {"target": "insumo", "category": "SUPPLIES"}
    action.risk_level = "MEDIUM"
    action.status = "PENDING"
    action.external_system = None
    action.expires_at = expires_at or (datetime.now(UTC) + timedelta(hours=1))
    if group_id:
        action.approval_group_id = group_id
        action.group_execution_status = "PENDING"
        action.group_position = 0
    return action


async def _seed_pending_pointer(
    redis: FakeRedis,
    db: AsyncSession,
    conversation_id: str,
    *,
    pending_action_id: str | None,
    approval_group_id: str | None = None,
) -> None:
    svc = ConversationService(redis, db)  # type: ignore[arg-type]
    await svc.record_pending_action(
        conversation_id,
        pending_action_id=pending_action_id,
        approval_group_id=approval_group_id,
        status="requires_approval",
    )


# ── _try_nl_confirmation ──────────────────────────────────────────────────────


async def test_si_confirms_single_action(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    tenant_id = sample_tenant.tenant_id
    # Gasto a reclasificar (para que la acción ejecute con datos).
    expense = ExpenseEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=Decimal("5000"),
        category="OTHER",
        expense_type="OPEX",
        transaction_date=datetime(2026, 6, 1, 10, 0, 0),
        description="rollos",
        payment_method="cash",
        provenance="REAL",
    )
    db_session.add(expense)
    action = _make_pending(tenant_id, sample_user.user_id)
    action.payload = {"expense_id": str(expense.id), "target": "insumo", "category": "SUPPLIES"}
    db_session.add(action)
    await db_session.commit()

    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(
        redis, db_session, conv_id, pending_action_id=str(action.id)
    )

    body = ChatRequest(message="sí", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is not None
    assert resp.status == "success"
    assert resp.result["nl_confirmation"] == "confirmed"
    await db_session.refresh(action)
    assert action.status == "APPROVED"
    await db_session.refresh(expense)
    assert expense.category == "SUPPLIES"


async def test_si_without_pending_does_not_execute(
    db_session: AsyncSession, sample_user: User
) -> None:
    redis = FakeRedis()
    body = ChatRequest(message="sí", conversation_id=str(uuid.uuid4()))
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is None  # sin puntero → sigue flujo normal (no ejecuta)


async def test_non_affirmation_returns_none(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    tenant_id = sample_tenant.tenant_id
    action = _make_pending(tenant_id, sample_user.user_id)
    db_session.add(action)
    await db_session.commit()
    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(redis, db_session, conv_id, pending_action_id=str(action.id))

    body = ChatRequest(message="mostrame otra cosa", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is None  # mensaje no es afirmación/negación pura


async def test_si_confirms_group(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    tenant_id = sample_tenant.tenant_id
    group_id = str(uuid.uuid4())
    e1 = ExpenseEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=Decimal("1000"),
        category="OTHER",
        expense_type="OPEX",
        transaction_date=datetime(2026, 6, 1, 10, 0, 0),
        description="a",
        payment_method="cash",
        provenance="REAL",
    )
    db_session.add(e1)
    a1 = _make_pending(tenant_id, sample_user.user_id, group_id=group_id)
    a1.payload = {"expense_id": str(e1.id), "target": "insumo", "category": "SUPPLIES"}
    db_session.add(a1)
    await db_session.commit()

    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(
        redis, db_session, conv_id, pending_action_id=None, approval_group_id=group_id
    )

    body = ChatRequest(message="dale", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is not None
    assert resp.result["nl_confirmation"] == "confirmed"
    await db_session.refresh(a1)
    assert a1.status == "APPROVED"


async def test_viewer_cannot_confirm_by_text(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tenant_id = sample_tenant.tenant_id
    viewer = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="viewer-nl@kiosco.com",
        full_name="V",
        password_hash="x",
        role_code="VIEWER",
        is_active=True,
    )
    db_session.add(viewer)
    action = _make_pending(tenant_id, viewer.user_id)
    db_session.add(action)
    await db_session.commit()

    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(redis, db_session, conv_id, pending_action_id=str(action.id))

    body = ChatRequest(message="sí", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=viewer, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is not None
    assert resp.status == "error"
    assert resp.result["nl_confirmation"] == "forbidden"
    await db_session.refresh(action)
    assert action.status == "PENDING"  # NO se ejecutó


async def test_expired_action_not_executed_by_text(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    tenant_id = sample_tenant.tenant_id
    action = _make_pending(
        tenant_id, sample_user.user_id, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db_session.add(action)
    await db_session.commit()

    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(redis, db_session, conv_id, pending_action_id=str(action.id))

    body = ChatRequest(message="sí", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is not None
    assert resp.status == "error"
    assert resp.result["nl_confirmation"] == "expired"


async def test_no_cancels_single_action(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    tenant_id = sample_tenant.tenant_id
    action = _make_pending(tenant_id, sample_user.user_id)
    db_session.add(action)
    await db_session.commit()

    redis = FakeRedis()
    conv_id = str(uuid.uuid4())
    await _seed_pending_pointer(redis, db_session, conv_id, pending_action_id=str(action.id))

    body = ChatRequest(message="no", conversation_id=conv_id)
    resp = await _try_nl_confirmation(
        body=body, current_user=sample_user, db=db_session, redis=redis  # type: ignore[arg-type]
    )
    assert resp is not None
    assert resp.result["nl_confirmation"] == "cancelled"
    await db_session.refresh(action)
    assert action.status == "REJECTED"
