"""M·2b-3 — las rutas de salud con el margen AUSENTE (sin costo suficiente).

Revisión: `_build_dimensions` guardaba la dimensión ausente con un valor que
`DimensionScoreResponse.value` (float) no acepta, y `/current` y `/history`
daban 500 a todo tenant con cobertura de costo < 50%.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService

P = "/api/v1/health-scores"


async def test_las_rutas_de_salud_responden_con_margen_ausente(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant,
    sample_business_profile,
) -> None:
    snap = await HealthScoreService(db_session).recalculate_for_tenant(
        tenant_id=sample_tenant.tenant_id, triggered_by="test"
    )
    await db_session.commit()
    assert snap.score_margin is None

    current = await client.get(f"{P}/current", headers=auth_headers)
    assert current.status_code == 200, current.text
    margen = next(d for d in current.json()["dimensions"] if d["dimension"] == "margin")
    assert margen["value"] is None
    assert margen["weight"] == 0.0

    history = await client.get(f"{P}/history", headers=auth_headers)
    assert history.status_code == 200, history.text

    latest = await client.get(f"{P}/latest", headers=auth_headers)
    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["score_margin"] is None
    assert body["margin_status"] == "ausente"

    v2 = await client.get(f"{P}/history/v2", headers=auth_headers)
    assert v2.status_code == 200, v2.text
    assert v2.json()[0]["score_margin"] is None

    mh = await client.get(f"{P}/margin-history", headers=auth_headers)
    assert mh.status_code == 200, mh.text
    item = mh.json()[0]
    assert (item["score_margin"], item["margin_pct"], item["margin_basis"]) == (
        None, None, "costo_vendido"
    )
