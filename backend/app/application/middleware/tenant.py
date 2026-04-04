"""TenantMiddleware — extrae tenant_id del JWT y lo almacena en request.state."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.observability.logger import get_logger

logger = get_logger(__name__)


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Extrae el tenant_id del JWT (sin verificar firma — la verificación real
    ocurre en deps.py). Almacena el valor en request.state.tenant_id para
    que tenant_context.set_tenant_context() lo use al abrir cada conexión DB.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token:
            try:
                # Decodificar sin verificar firma — solo necesitamos el tenant_id
                # para el contexto RLS. La autorización real ocurre en deps.py.
                import base64
                import json

                parts = token.split(".")
                if len(parts) == 3:
                    # Padding estándar de base64url
                    padded = parts[1] + "=" * (-len(parts[1]) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(padded))
                    tenant_id = payload.get("tenant_id")
                    if tenant_id:
                        request.state.tenant_id = str(tenant_id)
            except Exception:
                pass  # No interrumpir el request — deps.py rechazará si es inválido

        response = await call_next(request)
        return response
