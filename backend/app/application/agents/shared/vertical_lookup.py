"""Resolución del vertical de un tenant desde su `BusinessProfile`.

Único lugar donde la capa de agentes traduce `business_profiles.vertical_code`
al enum canónico. **Sin fallback**: si el tenant no tiene perfil —o el código
guardado no es uno de los canónicos— levanta `UnknownVerticalError` en vez
de asumir kiosco. Un negocio sin rubro configurado es un estado roto, y
scorearlo con las heurísticas de otro rubro lo esconde.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import UnknownVerticalError, Vertical, parse_vertical


async def load_tenant_vertical(db: AsyncSession, tenant_id: uuid.UUID) -> Vertical:
    """Devuelve el vertical canónico del tenant.

    Levanta `UnknownVerticalError` si el tenant no tiene `BusinessProfile` o si
    su `vertical_code` no es canónico.
    """
    from app.persistence.models.business import BusinessProfile  # noqa: PLC0415

    result = await db.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    code = result.scalar_one_or_none()
    if code is None:
        raise UnknownVerticalError(
            f"El tenant {tenant_id} no tiene BusinessProfile: no hay vertical que aplicar."
        )
    return parse_vertical(code)
