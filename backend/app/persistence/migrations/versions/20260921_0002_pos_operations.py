"""Contrato de operación de caja, unidades base y descuentos (B3).

Revision ID: 20260921_0002
Revises: 20260921_0001

Aditiva y **sin backfill**:

* ``pos_operations`` / ``pos_operation_lines`` / ``pos_tenders`` — la operación
  como unidad con identidad propia. Lo que hoy agrupa una venta multi-línea es
  un UUID adentro de ``custom_fields``, que no se puede referenciar, bloquear
  ni auditar como un todo.
* ``sales_entries.created_by_user_id`` — nullable a propósito. Queda en NULL
  para todo el histórico y para lo importado: no se sabe quién cobró, y
  rellenarlo con el dueño convertiría "no consta" en una afirmación falsa.
* ``products.sale_unit`` / ``base_units_per_sale_unit`` — con default
  ``unit``/1, **ningún número del histórico cambia**. Es deliberado: migrar el
  TIPO de ``stock_units`` o ``quantity`` tocaría el motor de inventario, el
  chequeo de integridad y la reconciliación temporal a la vez, que es el mayor
  riesgo de regresión del programa. Contar en unidades base con factor 1 deja
  todo idéntico y habilita los gramos sin tocar aritmética.

Los CHECK nuevos sobre ``products`` se crean **sólo en PostgreSQL**: en SQLite
el esquema de los tests lo construye ``create_all`` desde los modelos (que ya
los declaran) y, además, SQLite no los valida en un ALTER.

Idempotente (convención E8c): el ``preDeployCommand`` de Railway corre
``alembic upgrade head`` en cada deploy, y un esquema por delante de
``alembic_version`` no puede tumbarlo. Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260921_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None

_OPERACIONES = "pos_operations"
_LINEAS = "pos_operation_lines"
_TENDERS = "pos_tenders"

_CHECKS_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("ck_products_sale_unit", "sale_unit IN ('unit','gram','milliliter')"),
    ("ck_products_base_units_positivo", "base_units_per_sale_unit > 0"),
)


def _tabla_existe(bind: sa.Connection, tabla: str) -> bool:
    return sa.inspect(bind).has_table(tabla)


def _columnas(bind: sa.Connection, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def _checks(bind: sa.Connection, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_check_constraints(tabla) if c.get("name")}


def _uuid(es_pg: bool) -> sa.types.TypeEngine[object]:
    from sqlalchemy.dialects import postgresql

    return postgresql.UUID(as_uuid=True) if es_pg else sa.String(36)


def upgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"
    uuid_t = _uuid(es_pg)

    # ── Columnas sobre tablas existentes ──────────────────────────────────
    if "created_by_user_id" not in _columnas(bind, "sales_entries"):
        op.add_column(
            "sales_entries",
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    columnas_producto = _columnas(bind, "products")
    if "sale_unit" not in columnas_producto:
        op.add_column(
            "products",
            sa.Column("sale_unit", sa.String(20), nullable=False, server_default="unit"),
        )
    if "base_units_per_sale_unit" not in columnas_producto:
        op.add_column(
            "products",
            sa.Column(
                "base_units_per_sale_unit", sa.Integer(), nullable=False, server_default="1"
            ),
        )

    if es_pg:
        presentes = _checks(bind, "products")
        for nombre, condicion in _CHECKS_PRODUCTS:
            if nombre not in presentes:
                op.create_check_constraint(nombre, "products", condicion)

    # ── Cabecera de la operación ──────────────────────────────────────────
    if not _tabla_existe(bind, _OPERACIONES):
        op.create_table(
            _OPERACIONES,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("client_operation_id", sa.String(64), nullable=False),
            sa.Column(
                "created_by_user_id",
                uuid_t,
                sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "customer_id",
                uuid_t,
                sa.ForeignKey("customers.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("operation_date", sa.DateTime(), nullable=False),
            sa.Column("subtotal_ars", sa.Numeric(12, 2), nullable=False),
            sa.Column("discount_ars", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("total_ars", sa.Numeric(12, 2), nullable=False),
            sa.Column("cash_received_ars", sa.Numeric(12, 2), nullable=True),
            sa.Column("cash_change_ars", sa.Numeric(12, 2), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="COMPLETED"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "client_operation_id", name="uq_pos_operations_tenant_client_id"
            ),
            sa.CheckConstraint(
                "status IN ('COMPLETED','VOIDED')", name="ck_pos_operations_status"
            ),
            sa.CheckConstraint("total_ars >= 0", name="ck_pos_operations_total_no_negativo"),
            sa.CheckConstraint(
                "discount_ars >= 0", name="ck_pos_operations_discount_no_negativo"
            ),
            sa.CheckConstraint(
                "(cash_received_ars IS NULL) = (cash_change_ars IS NULL)",
                name="ck_pos_operations_efectivo_consistente",
            ),
        )
        op.create_index("ix_pos_operations_tenant_id", _OPERACIONES, ["tenant_id"])
        op.create_index(
            "ix_pos_operations_tenant_fecha", _OPERACIONES, ["tenant_id", "operation_date"]
        )
        op.create_index(
            "ix_pos_operations_tenant_cajero", _OPERACIONES, ["tenant_id", "created_by_user_id"]
        )
        op.create_index("ix_pos_operations_operation_date", _OPERACIONES, ["operation_date"])

    # ── Líneas ────────────────────────────────────────────────────────────
    if not _tabla_existe(bind, _LINEAS):
        op.create_table(
            _LINEAS,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "operation_id",
                uuid_t,
                sa.ForeignKey("pos_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sale_entry_id",
                uuid_t,
                sa.ForeignKey("sales_entries.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "product_id",
                uuid_t,
                sa.ForeignKey("products.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price_list", sa.Numeric(12, 2), nullable=False),
            sa.Column(
                "discount_line_ars", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column(
                "discount_global_share_ars",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column("line_total_ars", sa.Numeric(12, 2), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "operation_id", "position", name="uq_pos_operation_lines_posicion"
            ),
            sa.CheckConstraint("quantity > 0", name="ck_pos_operation_lines_cantidad"),
            sa.CheckConstraint(
                "line_total_ars >= 0", name="ck_pos_operation_lines_total_no_negativo"
            ),
        )
        op.create_index("ix_pos_operation_lines_tenant_id", _LINEAS, ["tenant_id"])
        op.create_index("ix_pos_operation_lines_operation_id", _LINEAS, ["operation_id"])

    # ── Pagos ─────────────────────────────────────────────────────────────
    if not _tabla_existe(bind, _TENDERS):
        op.create_table(
            _TENDERS,
            sa.Column("id", uuid_t, primary_key=True),
            sa.Column(
                "tenant_id",
                uuid_t,
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "operation_id",
                uuid_t,
                sa.ForeignKey("pos_operations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("payment_method", sa.String(30), nullable=False),
            sa.Column("amount_ars", sa.Numeric(12, 2), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("operation_id", "position", name="uq_pos_tenders_posicion"),
            sa.CheckConstraint("amount_ars > 0", name="ck_pos_tenders_monto_positivo"),
        )
        op.create_index("ix_pos_tenders_tenant_id", _TENDERS, ["tenant_id"])
        op.create_index("ix_pos_tenders_operation_id", _TENDERS, ["operation_id"])
        op.create_index(
            "ix_pos_tenders_tenant_metodo", _TENDERS, ["tenant_id", "payment_method"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    es_pg = bind.dialect.name == "postgresql"

    for tabla in (_TENDERS, _LINEAS, _OPERACIONES):
        if _tabla_existe(bind, tabla):
            op.drop_table(tabla)

    if es_pg:
        presentes = _checks(bind, "products")
        for nombre, _ in _CHECKS_PRODUCTS:
            if nombre in presentes:
                op.drop_constraint(nombre, "products", type_="check")

    columnas_producto = _columnas(bind, "products")
    sobrantes = [
        c for c in ("sale_unit", "base_units_per_sale_unit") if c in columnas_producto
    ]
    if bind.dialect.name == "sqlite":
        if sobrantes:
            with op.batch_alter_table("products") as batch:
                for nombre in sobrantes:
                    batch.drop_column(nombre)
        if "created_by_user_id" in _columnas(bind, "sales_entries"):
            with op.batch_alter_table("sales_entries") as batch:
                batch.drop_column("created_by_user_id")
    else:
        for nombre in sobrantes:
            op.drop_column("products", nombre)
        if "created_by_user_id" in _columnas(bind, "sales_entries"):
            op.drop_column("sales_entries", "created_by_user_id")
