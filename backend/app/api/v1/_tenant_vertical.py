"""Resolver el vertical del tenant dueño de la request — compartido entre
routers que necesitan el vertical para leer/combinar catálogos de campos
(`fields.py`, `export.py`). Extraído de `fields.py` para no duplicarlo al
sumar `export.py` (Cambio 3 del plan de conservación de datos).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import UnknownVerticalError, Vertical, parse_vertical
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def get_vertical_code(tenant_id: UUID, session: AsyncSession) -> Vertical:
    """Vertical del tenant dueño de la request.

    Dos estados rotos, dos respuestas de dominio explícitas (nunca un 500
    genérico, y nunca el set de campos de otro rubro):

    - Sin `BusinessProfile` → **404** `business_profile_not_found`. Un tenant
      logueado sin perfil es un estado roto real (los signups sociales viejos).
    - Con perfil pero `vertical_code` no canónico (p. ej. el código corto
      legado `"kiosco"`, hasta que corra la migración de unificación) → **409**
      `vertical_no_canonico`.
    """
    from app.persistence.repositories.business_profile_repository import BusinessProfileRepository

    profile = await BusinessProfileRepository(session).get_by_tenant_id(tenant_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="business_profile_not_found"
        )
    try:
        return parse_vertical(profile.vertical_code)
    except UnknownVerticalError as exc:
        logger.warning(
            "tenant_vertical_no_canonico",
            tenant_id=str(tenant_id),
            vertical_code=profile.vertical_code,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="vertical_no_canonico"
        ) from exc
