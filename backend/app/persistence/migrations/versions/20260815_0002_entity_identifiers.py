"""F-ID: entity_identifiers + vektor_code en customers/suppliers

Revision ID: 20260815_0002
Revises: 20260815_0001
Create Date: 2026-08-15

Contexto
--------
Segundo paso de F-ID. Dos piezas:

1. ``entity_identifiers`` — tabla NUEVA y vacía (sin ``CONCURRENTLY``, no hay
   tráfico que proteger todavía). Registro transversal de identificadores
   externos por entidad, con procedencia — ver el docstring del modelo
   (``persistence/models/entity_identifier.py``) para el porqué completo.

2. ``customers.vektor_code``/``suppliers.vektor_code`` (+ su ``_normalized``)
   — columnas denormalizadas nuevas sobre tablas EXISTENTES y pobladas. Nacen
   100% ``NULL`` en toda fila (no hay backfill acá, eso es F-ID.6), así que no
   hace falta preflight de datos sucios — a diferencia de ``20260802_0001``,
   que sí corría sobre ``sku``/``barcode`` ya poblados. Igual se usa
   ``CONCURRENTLY`` para el índice, mismo motivo que esa migración: esto corre
   contra Neon con tráfico vivo y un ``CREATE INDEX`` común bloquea escrituras
   sobre la tabla mientras dura el build.

El no-reciclo del VALOR del código Véktor lo garantiza la secuencia atómica de
``20260815_0001``, no estos índices — son parciales sobre ACTIVOS solamente
(mismo criterio que ``uq_products_tenant_sku_norm``) y sólo evitan que dos
filas activas colisionen por un bug de asignación, no evitan que un valor ya
entregado se reasigne (eso no puede pasar: la secuencia nunca lo vuelve a
entregar).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260815_0002"
down_revision = "20260815_0001"
branch_labels = None
depends_on = None

_VEKTOR_CODE_PREDICATE = (
    "deactivated_at IS NULL AND vektor_code_normalized IS NOT NULL "
    "AND vektor_code_normalized <> ''"
)

# (tabla, nombre del índice único, nombre del índice de búsqueda)
_VEKTOR_CODE_TABLES = (
    ("customers", "uq_customers_tenant_vektor_code_norm", "ix_customers_tenant_vektor_code_norm"),
    ("suppliers", "uq_suppliers_tenant_vektor_code_norm", "ix_suppliers_tenant_vektor_code_norm"),
)


def _drop_concurrently(conn: sa.Connection, name: str) -> None:
    conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


def _create_unique_index_concurrently(
    conn: sa.Connection, table: str, name: str, predicate: str
) -> None:
    conn.execute(
        sa.text(
            f"CREATE UNIQUE INDEX CONCURRENTLY {name} "
            f"ON {table} (tenant_id, vektor_code_normalized) WHERE {predicate}"
        )
    )


def upgrade() -> None:
    op.create_table(
        "entity_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identifier_type", sa.String(length=30), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("raw_value", sa.String(length=300), nullable=False),
        sa.Column("normalized_value", sa.String(length=300), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_upload_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_upload_id"], ["uploaded_files.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.user_id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_entity_identifiers_entity",
        "entity_identifiers",
        ["tenant_id", "entity_type", "entity_id"],
    )
    op.create_index(
        "ix_entity_identifiers_lookup",
        "entity_identifiers",
        ["tenant_id", "entity_type", "identifier_type", "namespace", "normalized_value"],
    )
    op.create_index(
        "uq_entity_identifiers_active_value",
        "entity_identifiers",
        ["tenant_id", "entity_type", "identifier_type", "namespace", "normalized_value"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )

    for table, _, _ in _VEKTOR_CODE_TABLES:
        op.add_column(table, sa.Column("vektor_code", sa.String(length=20), nullable=True))
        op.add_column(
            table, sa.Column("vektor_code_normalized", sa.String(length=20), nullable=True)
        )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) obtiene los índices por create_all desde el ORM.
        return

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        for table, unique_name, search_name in _VEKTOR_CODE_TABLES:
            conn.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {search_name} "
                    f"ON {table} (tenant_id, vektor_code_normalized)"
                )
            )
            try:
                _create_unique_index_concurrently(conn, table, unique_name, _VEKTOR_CODE_PREDICATE)
            except Exception:
                _drop_concurrently(conn, unique_name)
                raise


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            conn = op.get_bind()
            for _, unique_name, search_name in _VEKTOR_CODE_TABLES:
                _drop_concurrently(conn, unique_name)
                _drop_concurrently(conn, search_name)

    for table, _, _ in _VEKTOR_CODE_TABLES:
        op.drop_column(table, "vektor_code_normalized")
        op.drop_column(table, "vektor_code")

    op.drop_index("uq_entity_identifiers_active_value", table_name="entity_identifiers")
    op.drop_index("ix_entity_identifiers_lookup", table_name="entity_identifiers")
    op.drop_index("ix_entity_identifiers_entity", table_name="entity_identifiers")
    op.drop_table("entity_identifiers")
