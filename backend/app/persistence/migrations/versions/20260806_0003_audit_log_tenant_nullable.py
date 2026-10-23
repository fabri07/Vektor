"""decision_audit_log.tenant_id NULLABLE (auditoría de solicitudes de acceso)

Revision ID: 20260806_0003
Revises: 20260806_0002
Create Date: 2026-08-06

Contexto
--------
El flujo de solicitudes de acceso (registro cerrado con aprobación manual) toma
cuatro decisiones que hay que auditar: **crear** la solicitud, **verificar** el
email, **rechazar** y **poner en lista de espera**. Ninguna de las cuatro tiene
tenant: el tenant recién existe cuando el dueño **aprueba**. Con
``decision_audit_log.tenant_id NOT NULL`` esas cuatro decisiones eran
literalmente inauditables — que es exactamente al revés de lo que pide la regla
del repo ("toda decisión relevante → ``decision_audit_log``, insert-only").

Esta migración hace la columna NULLABLE. ``approve()`` sigue grabando el
``tenant_id`` (para cuando escribe la decisión ya lo acuñó en la misma
transacción), así que las decisiones que SÍ tienen tenant no pierden nada, y
todas las consultas existentes —que filtran ``WHERE tenant_id = :x``— siguen
devolviendo lo mismo: un filtro por valor nunca matchea NULL.

Política RLS
------------
``20260401_0001`` habilitó RLS sobre ``decision_audit_log`` con una política
``USING (tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid)`` y
**sin** ``WITH CHECK``. En PostgreSQL, una política sin ``WITH CHECK`` reusa la
expresión de ``USING`` para validar las filas nuevas: insertar una fila con
``tenant_id IS NULL`` da ``NULL = ...`` → NULL → se trata como falso → INSERT
rechazado. O sea que aflojar el NOT NULL sin tocar la política dejaría el
INSERT bloqueado igual en cualquier despliegue donde RLS esté efectivamente
activo.

Se recrea la política con ``USING`` intacto y un ``WITH CHECK`` que además
admite ``tenant_id IS NULL``. Es estrictamente más permisivo SOLO en escritura:
la lectura sigue acotada al tenant de la sesión, así que las filas globales del
flujo de solicitudes NO se le hacen visibles a ningún tenant.

Todo el DDL es PostgreSQL-only y está guardado por dialecto: en SQLite (tests)
el esquema se crea desde ``Base.metadata``, no desde las migraciones, y
``ALTER COLUMN`` no existe.

``downgrade`` vuelve la columna a NOT NULL y restaura la política original.
Falla ruidoso si quedaron filas con ``tenant_id IS NULL`` — es correcto: son
justamente las que no se pueden representar en el esquema viejo.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260806_0003"
down_revision = "20260806_0002"
branch_labels = None
depends_on = None

TABLE_NAME = "decision_audit_log"
POLICY_NAME = "decision_audit_log_tenant"
_TENANT_MATCH = "tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid"


def _es_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _es_postgres():
        return

    op.alter_column(
        TABLE_NAME,
        "tenant_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # La política solo existe si 20260401_0001 corrió con RLS habilitado; el
    # DROP ... IF EXISTS lo cubre. Se recrea con WITH CHECK explícito.
    op.execute(sa.text(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {TABLE_NAME}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {POLICY_NAME} ON {TABLE_NAME} "
            f"USING ({_TENANT_MATCH}) "
            f"WITH CHECK (tenant_id IS NULL OR {_TENANT_MATCH})"
        )
    )


def downgrade() -> None:
    if not _es_postgres():
        return

    op.execute(sa.text(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {TABLE_NAME}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {POLICY_NAME} ON {TABLE_NAME} USING ({_TENANT_MATCH})"
        )
    )
    # Sin cláusula de escape para filas viejas: si hay decisiones sin tenant,
    # este ALTER falla y hay que decidir qué hacer con esas filas a mano.
    op.alter_column(
        TABLE_NAME,
        "tenant_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
