"""Cambio 3 (docs/plans/conservacion-y-acceso-datos-negocio.md):
`GET /export/{entity_type}` — solo lo que se puede probar sin que el
generador toque la base (ver test_csv_export_service.py para la lógica real
de exportación, que corre con su propia sesión — motivo documentado en
csv_export_service.py).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_entidad_no_cubierta_da_404_antes_de_tocar_la_base(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    res = await client.get("/api/v1/export/marketing", headers=auth_headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "entity_type_not_covered"


async def test_sin_autenticar_no_exporta_nada(client: AsyncClient) -> None:
    res = await client.get("/api/v1/export/product")
    assert res.status_code in (401, 403)
