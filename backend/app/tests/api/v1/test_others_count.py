"""``GET /others/count`` informa cuántos pendientes tienen destino sugerido.

El frontend necesita el número GLOBAL, no el de la página: "Importar todo lo
sugerido" opera sobre todos los pendientes del tenant, así que decidir si
habilitarlo mirando 50 filas de 2.288 se equivoca en las dos direcciones —
habilitar cuando no hay nada que importar (el estado real de ASTERIA: 2.285 de
2.288 sin sugerencia) o deshabilitar habiendo sugerencias más adelante.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

pytestmark = pytest.mark.asyncio


def _fila(
    tenant: Tenant, sugerida: str | None, status: str = UNCLASSIFIED_STATUS_PENDING
) -> UnclassifiedRecord:
    return UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        uploaded_file_id=None,
        source="ingestion",
        context_label="Hoja 1",
        headers=["detalle"],
        row_data={"detalle": "algo"},
        suggested_entity=sugerida,
        status=status,
    )


async def test_count_separa_pendientes_de_pendientes_con_sugerencia(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, Any],
) -> None:
    db_session.add_all(
        [
            _fila(sample_tenant, None),
            _fila(sample_tenant, None),
            _fila(sample_tenant, "sale"),
            # Ya resuelta: no cuenta ni como pendiente ni como sugerida.
            _fila(sample_tenant, "expense", UNCLASSIFIED_STATUS_DISMISSED),
        ]
    )
    await db_session.commit()

    res = await client.get("/api/v1/others/count", headers=auth_headers)

    assert res.status_code == 200
    assert res.json() == {"pending": 3, "pending_suggested": 1}


async def test_count_sin_ninguna_sugerencia(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, Any],
) -> None:
    """El estado de ASTERIA: pendientes de sobra, cero destinos sugeridos. Es lo
    que apaga el botón de importación masiva."""
    db_session.add_all([_fila(sample_tenant, None), _fila(sample_tenant, None)])
    await db_session.commit()

    res = await client.get("/api/v1/others/count", headers=auth_headers)

    assert res.json() == {"pending": 2, "pending_suggested": 0}
