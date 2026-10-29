"""Chequeo del estado de cuenta en las puertas que EMITEN tokens (F1 del panel SUPERADMIN).

Login, refresh y Google OAuth lo comparten: es una sola regla
(`domain/tenant_status.py`), no tres copias. Verify-email la aplica aparte
porque su contrato es devolver siempre el mismo 400 genérico.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.domain.tenant_status import (
    MENSAJE_SUSPENDIDA,
    TENANT_SUSPENDED_CODE,
    puede_autenticarse,
)
from app.observability.logger import get_logger
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

logger = get_logger(__name__)


def exigir_cuenta_que_autentica(tenant: Tenant, user: User) -> None:
    """403 `TENANT_SUSPENDED` si la cuenta no puede abrir ni extender una sesión.

    Se llama DESPUÉS de verificar la credencial: el mensaje distinto sólo lo ve
    quien ya probó ser el usuario, así que no permite averiguar qué cuentas existen.
    """
    if puede_autenticarse(tenant.status, user.role_code):
        return
    logger.warning(
        "auth.tenant_blocked",
        tenant_id=str(tenant.tenant_id),
        user_id=str(user.user_id),
        tenant_status=tenant.status,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": TENANT_SUSPENDED_CODE, "message": MENSAJE_SUSPENDIDA},
    )
