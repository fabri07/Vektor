"""Tests for GET /api/v1/momentum/profile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.business import MomentumProfile
from app.persistence.models.tenant import Tenant


class TestMomentumProfileEndpoint:
    async def test_profile_saltea_milestone_sin_code_en_vez_de_500(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Al menos un tenant en producción tiene un milestone histórico en
        `milestones_json` sin `code`/`label`/`unlocked_at` (dato preexistente
        corrupto). Antes del fix, `MilestoneItem(code=m["code"], ...)` tiraba
        `KeyError` sobre esa entrada y el endpoint devolvía 500 para TODO el
        perfil, no solo el milestone corrupto."""
        now = datetime.now(tz=UTC)
        db_session.add(
            MomentumProfile(
                tenant_id=sample_tenant.tenant_id,
                milestones_json=[
                    {"label": "Milestone huérfano", "unlocked_at": now.isoformat()},
                    {
                        "code": "M1",
                        "label": "Primera semana de mejora",
                        "unlocked_at": now.isoformat(),
                    },
                ],
                updated_at=now,
            )
        )
        await db_session.commit()

        response = await client.get("/api/v1/momentum/profile", headers=auth_headers)

        assert response.status_code == 200
        codes = [m["code"] for m in response.json()["milestones_unlocked"]]
        assert codes == ["M1"]  # la corrupta se saltea; la válida sobrevive
