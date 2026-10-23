"""Registro de peticiones idempotentes con su resultado (bloque B2 del POS).

Revision ID: 20260921_0001
Revises: 20260919_0001

Aditiva y sin backfill. `operation_fingerprints` sigue intacto y sigue siendo el
mecanismo de las 10 rutas que todavía usan `claim_idempotency_key`: esta tabla NO
lo reemplaza, lo complementa para las rutas que necesitan poder DEVOLVER el
resultado original de un reintento (ver el docstring del modelo).

No se migran las claves viejas: un fingerprint de `operation_fingerprints` no
tiene ni el hash del pedido ni la respuesta, así que copiarlo produciría filas que
dicen "ya se usó" sin poder contestar nada — exactamente el estado que esta tabla
existe para superar, y encima disfrazado de completo.

Idempotente (convención E8c): el `preDeployCommand` de Railway corre
`alembic upgrade head` en cada deploy, y un esquema por delante de
`alembic_version` no puede tumbarlo. Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260921_0001"
down_revision = "20260919_0001"
branch_labels = None
depends_on = None

_TABLA = "idempotency_records"
_INDICE = "ix_idempotency_records_tenant_created"


def _tabla_existe(bind: sa.Connection, tabla: str) -> bool:
    return sa.inspect(bind).has_table(tabla)


def upgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"
    if _tabla_existe(bind, _TABLA):
        return

    uuid_t = postgresql.UUID(as_uuid=True) if es_pg else sa.String(36)
    json_t = postgresql.JSONB() if es_pg else sa.JSON()
    op.create_table(
        _TABLA,
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("action_type", sa.String(60), nullable=False),
        # sha256 hex del contenido comercial YA VALIDADO del pedido. No de los bytes
        # crudos: el orden de las claves del JSON no puede producir un conflicto falso.
        sa.Column("request_hash", sa.String(64), nullable=False),
        # Nullable a propósito: una ruta que reclama la clave y no registra resultado
        # deja NULL, y el replay degrada al 409 de siempre en vez de inventar.
        sa.Column("response_json", json_t, nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False, server_default="201"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # El candado de la carrera: dos peticiones simultáneas con la misma clave no
        # pueden reclamar las dos. No se resuelve con un SELECT previo.
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_records_tenant_key"),
    )
    op.create_index(_INDICE, _TABLA, ["tenant_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _tabla_existe(bind, _TABLA):
        op.drop_table(_TABLA)
