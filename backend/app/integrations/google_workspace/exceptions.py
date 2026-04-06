"""Excepciones del Google Workspace gateway."""

from __future__ import annotations


class WorkspaceTokenError(Exception):
    """Error en la obtención o renovación del token de Workspace.

    Atributo ``reason`` describe la causa:
      - ``"not_connected"``        — el usuario no tiene conexión Workspace activa
      - ``"refresh_failed"``       — Google rechazó el refresh (invalid_grant, etc.)
      - ``"insufficient_scope"``   — el token existe pero no tiene los permisos necesarios
      - ``"token_corrupted"``      — descifrado Fernet fallido (clave incorrecta o datos dañados)
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


class InsufficientScopeError(Exception):
    """Google devolvió 401 insufficient_scope o 403 insufficientPermissions.

    El gateway la captura y la convierte en WorkspaceTokenError(reason="insufficient_scope").
    """
