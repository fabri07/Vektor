"""Tests del dashboard de consumo de tokens (GET /admin/usage) + model_pricing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.model_pricing import estimate_cost_usd
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_USAGE = "/api/v1/admin/usage"


async def _superadmin_headers(db: AsyncSession, tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="super@vektor.app",
        full_name="Super Admin",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _audit_row(
    *,
    tenant_id: uuid.UUID,
    created_at: datetime,
    model: str,
    sub_agent: str,
    in_tok: int,
    out_tok: int,
) -> DecisionAuditLog:
    decision_data: dict[str, Any] = {
        "sub_agent_name": sub_agent,
        "ceo_target_agent": sub_agent,
        "token_calls": [
            {
                "source": sub_agent,
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            }
        ],
    }
    return DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        decision_type="AGENT_ACTION",
        decision_data=decision_data,
        triggered_by="chat",
        actor_user_id=None,
        tokens_input=in_tok,
        tokens_output=out_tok,
        tokens_total=in_tok + out_tok,
        created_at=created_at,
    )


# ── Unit: estimate_cost_usd ───────────────────────────────────────────────────


def test_estimate_cost_known_model() -> None:
    # Sonnet: input 3/1M, output 15/1M. 1M in + 1M out = 3 + 15 = 18.
    cost, priced = estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert priced is True
    assert cost == Decimal("18.000000")


def test_estimate_cost_unknown_model() -> None:
    cost, priced = estimate_cost_usd("modelo-inexistente", 1_000_000, 1_000_000)
    assert priced is False
    assert cost == Decimal("0.000000")


# ── RBAC ──────────────────────────────────────────────────────────────────────


async def test_owner_forbidden(client: AsyncClient, auth_headers: dict[str, Any]) -> None:
    resp = await client.get(_USAGE, headers=auth_headers)
    assert resp.status_code == 403


# ── Agregados ─────────────────────────────────────────────────────────────────


async def test_usage_aggregates(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    # 2 modelos, 2 agentes, 2 días, 2 tenants.
    rows = [
        # sonnet / agent_income / hoy / sample
        _audit_row(
            tenant_id=sample_tenant.tenant_id,
            created_at=now,
            model="claude-sonnet-4-6",
            sub_agent="agent_income",
            in_tok=1_000_000,
            out_tok=1_000_000,  # costo 18
        ),
        # haiku / agent_expense / ayer / sample
        _audit_row(
            tenant_id=sample_tenant.tenant_id,
            created_at=yesterday,
            model="claude-haiku-4-5",
            sub_agent="agent_expense",
            in_tok=1_000_000,
            out_tok=1_000_000,  # costo 1 + 5 = 6
        ),
        # sonnet / agent_income / hoy / second tenant
        _audit_row(
            tenant_id=second_tenant.tenant_id,
            created_at=now,
            model="claude-sonnet-4-6",
            sub_agent="agent_income",
            in_tok=2_000_000,
            out_tok=0,  # costo 6
        ),
    ]
    for r in rows:
        db_session.add(r)
    await db_session.commit()

    resp = await client.get(_USAGE, headers=headers, params={"days": 30})
    assert resp.status_code == 200
    data = resp.json()

    # Totals.
    assert data["totals"]["decisions"] == 3
    assert data["totals"]["tokens_input"] == 4_000_000
    assert data["totals"]["tokens_output"] == 2_000_000
    assert data["totals"]["tokens_total"] == 6_000_000
    assert data["totals"]["cost_usd"] == pytest.approx(30.0)  # 18 + 6 + 6

    # by_model: sonnet priced + costo > 0.
    by_model = {m["model"]: m for m in data["by_model"]}
    assert by_model["claude-sonnet-4-6"]["priced"] is True
    assert by_model["claude-sonnet-4-6"]["cost_usd"] == pytest.approx(24.0)  # 18 + 6
    assert by_model["claude-haiku-4-5"]["priced"] is True
    assert by_model["claude-haiku-4-5"]["cost_usd"] == pytest.approx(6.0)
    # Ordenado DESC por costo.
    assert data["by_model"][0]["model"] == "claude-sonnet-4-6"

    # by_agent ordenado DESC por costo: income (24) antes que expense (6).
    assert data["by_agent"][0]["agent"] == "agent_income"
    assert data["by_agent"][0]["cost_usd"] == pytest.approx(24.0)
    assert data["by_agent"][1]["agent"] == "agent_expense"

    # by_day: 2 días, orden ASC.
    assert len(data["by_day"]) == 2
    assert data["by_day"][0]["date"] < data["by_day"][1]["date"]

    # by_tenant: 2 tenants, DESC por costo. sample (24) > second (6).
    assert len(data["by_tenant"]) == 2
    assert data["by_tenant"][0]["tenant_id"] == str(sample_tenant.tenant_id)
    assert data["by_tenant"][0]["cost_usd"] == pytest.approx(24.0)


async def test_usage_filtered_by_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    now = datetime.now(UTC)
    db_session.add(
        _audit_row(
            tenant_id=sample_tenant.tenant_id,
            created_at=now,
            model="claude-sonnet-4-6",
            sub_agent="agent_income",
            in_tok=1_000_000,
            out_tok=0,  # costo 3
        )
    )
    db_session.add(
        _audit_row(
            tenant_id=second_tenant.tenant_id,
            created_at=now,
            model="claude-sonnet-4-6",
            sub_agent="agent_income",
            in_tok=1_000_000,
            out_tok=0,
        )
    )
    await db_session.commit()

    resp = await client.get(
        _USAGE, headers=headers, params={"tenant_id": str(sample_tenant.tenant_id)}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["decisions"] == 1
    assert len(data["by_tenant"]) == 1
    assert data["by_tenant"][0]["tenant_id"] == str(sample_tenant.tenant_id)


async def test_by_agent_uses_call_source_not_sub_agent(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """by_agent atribuye por el `source` real de la call, no por sub_agent_name (CEO)."""
    headers = await _superadmin_headers(db_session, sample_tenant)
    now = datetime.now(UTC)
    decision_data = {
        # El orquestador deja sub_agent_name = agent_ceo, pero el gasto real es de agent_health.
        "sub_agent_name": "agent_ceo",
        "ceo_target_agent": "agent_ceo",
        "token_calls": [
            {
                "source": "agent_health",
                "model": "claude-sonnet-4-6",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
            }
        ],
    }
    db_session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            decision_type="AGENT_ACTION",
            decision_data=decision_data,
            triggered_by="chat",
            actor_user_id=None,
            tokens_input=1_000_000,
            tokens_output=0,
            tokens_total=1_000_000,
            created_at=now,
        )
    )
    await db_session.commit()

    resp = await client.get(_USAGE, headers=headers)
    assert resp.status_code == 200
    agents = {a["agent"] for a in resp.json()["by_agent"]}
    assert "agent_health" in agents
    assert "agent_ceo" not in agents


async def test_unpriced_tokens_surfaced(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Modelo no mapeado y filas sin token_calls → cost 0 pero unpriced_tokens los cuenta."""
    headers = await _superadmin_headers(db_session, sample_tenant)
    now = datetime.now(UTC)
    # Modelo no mapeado → priced False, cost 0.
    db_session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            decision_type="AGENT_ACTION",
            decision_data={
                "sub_agent_name": "agent_income",
                "token_calls": [
                    {
                        "source": "agent_income",
                        "model": "modelo-futuro-sin-precio",
                        "input_tokens": 500_000,
                        "output_tokens": 500_000,
                    }
                ],
            },
            triggered_by="chat",
            actor_user_id=None,
            tokens_input=500_000,
            tokens_output=500_000,
            tokens_total=1_000_000,
            created_at=now,
        )
    )
    # Fila con tokens pero SIN token_calls → unpriced + atribución best-effort.
    db_session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            decision_type="AGENT_ACTION",
            decision_data={"sub_agent_name": "agent_stock"},
            triggered_by="chat",
            actor_user_id=None,
            tokens_input=200_000,
            tokens_output=0,
            tokens_total=200_000,
            created_at=now,
        )
    )
    await db_session.commit()

    resp = await client.get(_USAGE, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["unpriced_tokens"] == 1_200_000  # 1M sin precio + 200k sin calls
    assert data["totals"]["cost_usd"] == pytest.approx(0.0)
    by_model = {m["model"]: m for m in data["by_model"]}
    assert by_model["modelo-futuro-sin-precio"]["priced"] is False


async def test_usage_empty(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    resp = await client.get(_USAGE, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["decisions"] == 0
    assert data["totals"]["tokens_total"] == 0
    assert data["totals"]["cost_usd"] == 0.0
    assert data["by_agent"] == []
    assert data["by_model"] == []
    assert data["by_day"] == []
    assert data["by_tenant"] == []
