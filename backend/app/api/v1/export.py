"""Cambio 3 del plan de conservación y acceso a datos de negocio — exportación
completa del lado servidor (docs/plans/conservacion-y-acceso-datos-negocio.md).

`GET /export/{entity_type}` es la "Exportar todos los campos" del plan:
distinta de exportar la vista actual (eso ya lo hace `SmartTable` en el
frontend, con las columnas visibles y las filas cargadas). Acá se exportan
TODOS los campos autorizados y TODAS las filas del alcance, sin el techo de
acumulación del frontend.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._tenant_vertical import get_vertical_code
from app.api.v1.deps import get_current_tenant
from app.application.services.csv_export_service import (
    EXPORTABLE_ENTITY_TYPES,
    stream_entity_csv,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant

router = APIRouter()


@router.get("/{entity_type}", summary="Exportación completa (todos los campos, todas las filas)")
async def export_entity(
    entity_type: str,
    include_inactive: bool = Query(
        default=False,
        description="false = solo activos (default, mismo alcance que ve la pantalla); "
        "true = incluye inactivos/dados de baja también. Sale/expense no tienen "
        "este concepto y lo ignoran.",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """El `tenant_id` sale del JWT (`get_current_tenant`) — nunca del path, así
    que no hay forma de pedir la exportación de otra cuenta. `entity_type` se
    valida contra un allowlist fijo (`EXPORTABLE_ENTITY_TYPES`) antes de tocar
    la base: nunca se interpola en una consulta.
    """
    if entity_type not in EXPORTABLE_ENTITY_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity_type_not_covered")

    # Se resuelve con la sesión del REQUEST (ya corrió queries de auth) —
    # `stream_entity_csv` abre su propia sesión dedicada para el snapshot
    # estable, ver csv_export_service.py.
    vertical_code = await get_vertical_code(tenant.tenant_id, session)

    filename = f"{entity_type}_{datetime.now(UTC).date().isoformat()}.csv"
    return StreamingResponse(
        stream_entity_csv(
            tenant.tenant_id, vertical_code, entity_type, include_inactive=include_inactive
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
