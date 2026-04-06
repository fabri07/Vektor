"""Tests de Sprint 4 — Pending Actions externas.

Cubre:
  - TestConfirmExecutionLifecycle : lifecycle completo de confirm (local, externa, fallos)
  - TestConfirmConcurrency        : doble confirm y vencimiento
  - TestRetryEndpoint             : retry, límite, fallos y edge cases
  - TestIdempotency               : idempotency_key y external_system en create
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import (
    EXTERNAL_SYSTEMS,
    create_pending_action,
)
from app.integrations.google_workspace.exceptions import WorkspaceTokenError
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction

# ── Fixtures ──────────────────────────────────────────────────────────────────

_EXEC_TARGET = "app.api.v1.agent.execute_pending_action"


@pytest.fixture
def mock_redis() -> AsyncMock:
    r = AsyncMock()
    r.incr = AsyncMock(return_value=1)
    r.expireat = AsyncMock()
    return r


@pytest_asyncio.fixture
async def agent_client(
    db_session: AsyncSession,
    mock_redis: AsyncMock,
) -> AsyncGenerator[AsyncClient, None]:
    """Cliente HTTP con DB y Redis mockeados."""
    from app.main import create_app, limiter  # noqa: PLC0415

    limiter._storage.reset()
    app = create_app()

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncMock:
        return mock_redis

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _make_pending_action(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    action_type: str = ActionType.REGISTER_SALE,
    status: str = "PENDING",
    execution_status: str = "NOT_STARTED",
    external_system: str | None = None,
    idempotency_key: str | None = None,
    expires_in_minutes: int = 10,
) -> PendingAction:
    """Crea un PendingAction en memoria listo para agregar a la sesión."""
    return PendingAction(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        action_type=action_type,
        payload={"amount": "1000.00", "description": "test"},
        risk_level="MEDIUM",
        status=status,
        execution_status=execution_status,
        external_system=external_system,
        idempotency_key=idempotency_key,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        created_at=datetime.now(UTC),
    )


# ── TestConfirmExecutionLifecycle ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestConfirmExecutionLifecycle:

    async def test_confirm_local_action_sets_succeeded(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción local confirmada → status=APPROVED, execution_status=SUCCEEDED."""
        action = _make_pending_action(sample_tenant.tenant_id, sample_user.user_id)
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            resp = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["execution_status"] == "SUCCEEDED"

        await db_session.refresh(action)
        assert action.status == "APPROVED"
        assert action.execution_status == "SUCCEEDED"

    async def test_confirm_external_action_sets_succeeded(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción externa exitosa → execution_status=SUCCEEDED."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            resp = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_status"] == "SUCCEEDED"
        assert "reconnect_required" not in data

        await db_session.refresh(action)
        assert action.execution_status == "SUCCEEDED"

    async def test_confirm_external_workspace_error_sets_requires_reconnect(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """WorkspaceTokenError(reason='refresh_failed') → REQUIRES_RECONNECT + failure_code."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        exc = WorkspaceTokenError("refresh_failed")
        with patch(_EXEC_TARGET, new=AsyncMock(side_effect=exc)):
            resp = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_status"] == "REQUIRES_RECONNECT"
        assert data.get("reconnect_required") is True

        await db_session.refresh(action)
        assert action.execution_status == "REQUIRES_RECONNECT"
        assert action.failure_code == "refresh_failed"
        assert action.failure_message is None
        assert action.status == "APPROVED"

    async def test_confirm_sets_approved_at(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """confirm() escribe approved_at en la fila."""
        action = _make_pending_action(sample_tenant.tenant_id, sample_user.user_id)
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        await db_session.refresh(action)
        assert action.approved_at is not None

    async def test_confirm_generic_exception_sets_failed(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Excepción genérica en acción externa → FAILED + failure_message, sin re-raise."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_status"] == "FAILED"

        await db_session.refresh(action)
        assert action.execution_status == "FAILED"
        assert action.failure_message == "boom"
        assert action.failure_code is None


# ── TestConfirmConcurrency ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConfirmConcurrency:

    async def test_double_confirm_second_gets_404(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Segunda confirmación del mismo ID → 404.

        Nota: la protección contra doble ejecución verdaderamente concurrente en
        producción la aporta SELECT FOR UPDATE en PostgreSQL. En tests SQLite
        verificamos la propiedad lógica: una vez APPROVED, el status deja de
        ser PENDING y la segunda llamada no encuentra la fila elegible.
        """
        action = _make_pending_action(sample_tenant.tenant_id, sample_user.user_id)
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            r1 = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )
            r2 = await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        assert r1.status_code == 200
        assert r2.status_code == 404

    async def test_confirm_expired_action_returns_410(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción vencida → 410 y status=EXPIRED en DB."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            expires_in_minutes=-5,  # ya venció
        )
        db_session.add(action)
        await db_session.commit()

        resp = await agent_client.post(
            f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
        )

        assert resp.status_code == 410
        await db_session.refresh(action)
        assert action.status == "EXPIRED"


# ── TestRetryEndpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRetryEndpoint:

    async def test_retry_failed_action_succeeds(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción APPROVED/FAILED → retry exitoso → execution_status=SUCCEEDED."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="FAILED",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            resp = await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "retried"
        assert data["execution_status"] == "SUCCEEDED"

        await db_session.refresh(action)
        assert action.execution_status == "SUCCEEDED"

    async def test_retry_requires_reconnect_action_succeeds(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción APPROVED/REQUIRES_RECONNECT → retry exitoso → SUCCEEDED."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="REQUIRES_RECONNECT",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            resp = await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        assert resp.json()["execution_status"] == "SUCCEEDED"

    async def test_retry_pending_action_returns_404(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción todavía PENDING → no es reintentable → 404."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            status="PENDING",
            execution_status="NOT_STARTED",
        )
        db_session.add(action)
        await db_session.commit()

        resp = await agent_client.post(
            f"/api/v1/agent/retry/{action.id}", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_retry_succeeded_action_returns_409(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Acción APPROVED/SUCCEEDED → ya ejecutada → 409."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="SUCCEEDED",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        resp = await agent_client.post(
            f"/api/v1/agent/retry/{action.id}", headers=auth_headers
        )
        assert resp.status_code == 409
        assert "exitosamente" in resp.json()["detail"]

    async def test_second_retry_returns_409(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Segundo retry → existe AGENT_ACTION_RETRIED en audit log → 409."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="FAILED",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.flush()

        # Pre-insertar el audit log del primer retry
        prev_audit = DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=action.tenant_id,
            decision_type="AGENT_ACTION_RETRIED",
            decision_data={
                "pending_action_id": str(action.id),
                "action_type": action.action_type,
                "execution_status": "FAILED",
                "failure_code": None,
            },
            triggered_by="agent:retry",
            actor_user_id=sample_user.user_id,
            context={"execution_status_after": "FAILED"},
            created_at=datetime.now(UTC),
        )
        db_session.add(prev_audit)
        await db_session.commit()

        resp = await agent_client.post(
            f"/api/v1/agent/retry/{action.id}", headers=auth_headers
        )
        assert resp.status_code == 409
        assert "reintentos" in resp.json()["detail"]

    async def test_retry_workspace_error_sets_requires_reconnect(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Retry que vuelve a fallar con WorkspaceTokenError → REQUIRES_RECONNECT."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="FAILED",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        exc = WorkspaceTokenError("not_connected")
        with patch(_EXEC_TARGET, new=AsyncMock(side_effect=exc)):
            resp = await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_status"] == "REQUIRES_RECONNECT"
        assert data.get("reconnect_required") is True

        await db_session.refresh(action)
        assert action.failure_code == "not_connected"

    async def test_retry_generic_exception_sets_failed(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Excepción genérica en retry → FAILED + failure_message, sin 500."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="REQUIRES_RECONNECT",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock(side_effect=RuntimeError("red error"))):
            resp = await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["execution_status"] == "FAILED"

        await db_session.refresh(action)
        assert action.failure_message == "red error"
        assert action.failure_code is None

    async def test_retry_writes_audit_log(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """Retry exitoso escribe un registro AGENT_ACTION_RETRIED en DecisionAuditLog."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            status="APPROVED",
            execution_status="FAILED",
            external_system="GOOGLE_GMAIL",
            idempotency_key=str(uuid.uuid4()),
        )
        db_session.add(action)
        await db_session.commit()

        with patch(_EXEC_TARGET, new=AsyncMock()):
            await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        audit_stmt = select(DecisionAuditLog).where(
            DecisionAuditLog.decision_type == "AGENT_ACTION_RETRIED",
            DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
        )
        audit_rows = (await db_session.execute(audit_stmt)).scalars().all()
        matching = [
            r for r in audit_rows
            if r.decision_data.get("pending_action_id") == str(action.id)
        ]
        assert len(matching) == 1
        assert matching[0].decision_data["execution_status"] == "SUCCEEDED"


# ── TestIdempotency ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIdempotency:

    async def test_create_sets_idempotency_key_for_external_actions(
        self,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """create_pending_action con CREATE_SUPPLIER_DRAFT → idempotency_key + external_system."""
        action = await create_pending_action(
            db=db_session,
            tenant_id=sample_tenant.tenant_id,
            user_id=sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            payload={"draft_text": "Hola proveedor"},
            risk_level="MEDIUM",
        )
        await db_session.commit()

        assert action.idempotency_key is not None
        assert action.external_system == EXTERNAL_SYSTEMS[ActionType.CREATE_SUPPLIER_DRAFT]
        assert action.external_system == "GOOGLE_GMAIL"

    async def test_create_does_not_set_idempotency_key_for_local_actions(
        self,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """create_pending_action con REGISTER_SALE → idempotency_key=None, external_system=None."""
        action = await create_pending_action(
            db=db_session,
            tenant_id=sample_tenant.tenant_id,
            user_id=sample_user.user_id,
            action_type=ActionType.REGISTER_SALE,
            payload={"amount": "500.00"},
            risk_level="MEDIUM",
        )
        await db_session.commit()

        assert action.idempotency_key is None
        assert action.external_system is None

    async def test_idempotency_key_is_stable_across_confirm_and_retry(
        self,
        agent_client: AsyncClient,
        auth_headers: dict,
        db_session: AsyncSession,
        sample_tenant,
        sample_user,
    ) -> None:
        """idempotency_key no cambia después de confirm fallido ni de retry."""
        action = _make_pending_action(
            sample_tenant.tenant_id,
            sample_user.user_id,
            action_type=ActionType.CREATE_SUPPLIER_DRAFT,
            external_system="GOOGLE_GMAIL",
            idempotency_key="original-key-abc",
        )
        db_session.add(action)
        await db_session.commit()

        exc = WorkspaceTokenError("refresh_failed")
        with patch(_EXEC_TARGET, new=AsyncMock(side_effect=exc)):
            await agent_client.post(
                f"/api/v1/agent/confirm/{action.id}", headers=auth_headers
            )

        await db_session.refresh(action)
        key_after_confirm = action.idempotency_key

        with patch(_EXEC_TARGET, new=AsyncMock()):
            await agent_client.post(
                f"/api/v1/agent/retry/{action.id}", headers=auth_headers
            )

        await db_session.refresh(action)
        assert action.idempotency_key == key_after_confirm == "original-key-abc"
