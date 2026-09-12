"""``operation_identities`` + ``operation_identity_links`` — dedup por clave fuerte (E6b)

Revision ID: 20260910_0001
Revises: 20260909_0001
Create Date: 2026-09-10

Contexto
--------
Migración ADDITIVE — dos tablas nuevas, ninguna columna tocada. Nada existente
las lee todavía; el importador las empieza a usar en el commit siguiente.

Qué agujero abren la puerta a tapar
-----------------------------------
La deduplicación de hoy (``operation_fingerprints`` con ancla
``archivo:contexto:índice``) sólo reconoce una fila que ya entró **desde el mismo
archivo**. Medido contra Postgres real: el mismo contenido subido como archivo
nuevo duplica todo — 3 gastos → 6, $12.000 → $24.000, stock 10 → 20. El guard de
``content_hash`` del upload tampoco lo ve, porque una planilla reexportada desde
Excel cambia de hash (el zip guarda timestamps).

Por qué el UNIQUE es el corazón de la migración
-----------------------------------------------
``uq_operation_identities_tenant_key`` no es una prolijidad de esquema: es el
candado. La deduplicación tiene que resistir **dos confirmaciones simultáneas del
mismo contenido**, y ninguna comprobación en memoria puede — las dos leerían la
tabla vacía y las dos insertarían. La reclamación se hace con
``INSERT ... ON CONFLICT DO NOTHING RETURNING``: la fila que vuelve es la que ganó
el candado, y la que no vuelve es la que perdió. Sin el unique, ese INSERT es un
insert cualquiera y las dos cargas duplican.

Y es ``(tenant_id, identity_key)``, no ``identity_key`` sola: el aislamiento entre
tenants lo da el índice, no el texto de la clave. Dos negocios pueden recibir la
misma factura del mismo proveedor.

Por qué dos tablas
------------------
La identidad es del DOCUMENTO; los efectos son las FILAS. Un remito de tres
renglones es una identidad y tres gastos. Con una sola tabla no habría forma de
liberar la identidad cuando se revierten dos renglones y el tercero se conserva
—porque el usuario lo editó—, que es exactamente lo que hace una relectura.
Liberar de más significa que el próximo import vuelve a aplicar un efecto que
sigue vivo.

``ix_operation_identity_links_efecto`` sirve a esa liberación: se busca por
``(tenant, tipo, id de entidad)`` para cada efecto revertido.

Orden de despliegue
-------------------
Sin riesgo de orden invertido: los dos escritores (api y worker) sólo tocan estas
tablas desde el camino de import, y el camino de import no las lee hasta el
commit siguiente. Un servicio viejo contra el esquema nuevo ignora las tablas; un
servicio nuevo contra el esquema viejo todavía no existe.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260910_0001"
down_revision = "20260909_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_identities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(10), nullable=False),
        sa.Column(
            "first_seen_upload_id",
            UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # El candado. Ver el encabezado: sin esto la reclamación masiva no
        # protege de dos confirmaciones simultáneas.
        sa.UniqueConstraint("tenant_id", "identity_key", name="uq_operation_identities_tenant_key"),
    )

    op.create_table(
        "operation_identity_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "identity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operation_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(10), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_upload_id",
            UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # Un efecto no puede estar dos veces bajo la misma identidad: hace
        # idempotente el registro del vínculo ante un reintento.
        sa.UniqueConstraint(
            "identity_id", "entity_type", "entity_id", name="uq_operation_identity_links_efecto"
        ),
    )
    op.create_index(
        "ix_operation_identity_links_efecto",
        "operation_identity_links",
        ["tenant_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_operation_identity_links_efecto", table_name="operation_identity_links")
    op.drop_table("operation_identity_links")
    op.drop_table("operation_identities")
