"""X3-1: ``users.deactivated_at`` — la baja del dueño no se deshace con el email.

Revision ID: 20261026_0001
Revises: 20261021_0004

``DELETE /users/{id}`` dejaba la cuenta en ``is_active=False``, el MISMO estado
que un registro pendiente de verificar el email; ``resend-verification`` le
mandaba un link y ``verify-email`` la activaba y devolvía un JWT. Un ex
empleado se reactivaba solo, con su rol, su PIN y sus permisos, salteando el
cupo de plazas. Con ``deactivated_at`` seteado, ninguno de los dos actúa.

**Backfill de las bajas anteriores** (``deactivated_at IS NULL`` e
``is_active = false``): se marca como baja sólo lo que NO puede ser un
registro pendiente:

- ``last_login_at IS NOT NULL``: el login, el link de Google y la identidad
  vinculada exigen ``is_active``, así que quien entró alguna vez estuvo activo;
  si hoy está inactivo, es una baja.
- sin ninguna fila en ``email_verification_tokens``: el registro con
  verificación crea el token en la misma transacción que el usuario (y nada
  borra tokens salvo el CASCADE del usuario); un usuario creado por el dueño
  (``POST /users``) o por una solicitud de acceso nace activo y sin token.
- su token más reciente está usado: ``verify-email`` lo consumió (un reenvío
  invalida los viejos pero crea uno nuevo sin usar, así que el último usado
  sólo puede venir de una verificación).

Lo demás (inactivo, sin login, con su último token sin usar) es
indistinguible de un registro pendiente y queda como está: ver el límite en
``docs/claude/maestros.md``. La fecha es ``updated_at`` (la baja hizo el
último ``save``), aproximada.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma; el backfill
sólo toca filas con ``deactivated_at IS NULL``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0001"
down_revision = "20261021_0004"
branch_labels = None
depends_on = None

_TABLA = "users"
_COLUMNA = "deactivated_at"

_BACKFILL = """
UPDATE users AS u
   SET deactivated_at = COALESCE(u.updated_at, CURRENT_TIMESTAMP)
 WHERE u.is_active = false
   AND u.deactivated_at IS NULL
   AND (
        u.last_login_at IS NOT NULL
     OR NOT EXISTS (
            SELECT 1 FROM email_verification_tokens t WHERE t.user_id = u.user_id
        )
     OR (
            SELECT t.used FROM email_verification_tokens t
             WHERE t.user_id = u.user_id
             ORDER BY t.created_at DESC
             LIMIT 1
        ) = true
   )
"""


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    if not _insp().has_table(_TABLA):
        return
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    if _COLUMNA not in existentes:
        op.add_column(_TABLA, sa.Column(_COLUMNA, sa.DateTime(timezone=True), nullable=True))
    if _insp().has_table("email_verification_tokens"):
        bind.execute(sa.text(_BACKFILL))


def downgrade() -> None:
    if not _insp().has_table(_TABLA):
        return
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    if _COLUMNA in existentes:
        op.drop_column(_TABLA, _COLUMNA)
