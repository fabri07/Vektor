"""Campos fiscales de cliente (persona/empresa) + sentinela "Local" + anti-duplicado

Revision ID: 20260721_0001
Revises: 20260720_0004
Create Date: 2026-07-21

Contexto
--------
Migración ADDITIVE — reforma de la sección Clientes (Fase A). Espejo de la reforma
de Proveedores (``20260720_0003``). Un cliente puede ser PERSONA (nombre +
apellido) o EMPRESA (razón social en ``name``), y AFIP exige identificarlo para
facturar:

  1. ``customer_type`` (String(10), nullable) — "person" | "company".
  2. ``last_name`` (String(200), nullable) — apellido (NULL si es empresa).
  3. ``doc_type`` (String(10), nullable) — "dni" | "cuit".
  4. ``dni`` (String(15), nullable) · ``cuit`` (String(13), nullable) — documento
     fiscal; validados por formato/dígito verificador en el schema (no se rellenan).
  5. ``iva_condition`` (String(25), nullable) — consumidor_final | monotributo |
     responsable_inscripto | exento.
  6. ``address`` (Text) · ``locality`` (String(120)) · ``province`` (String(120)) ·
     ``postal_code`` (String(12)) — domicilio de entrega (logística AR).
  7. ``birthday`` (Date, nullable) — solo se guarda (sin automatización).

Índices (en ``customers``):

  - ``uq_customers_sentinel_per_tenant`` — UN solo cliente sentinela "Local" por
    tenant (``custom_fields->>'_sentinel' = 'true'``). Garantiza el invariante a
    nivel DB (concurrencia de ventas sin cliente).
  - ``uq_customers_dni_per_tenant`` / ``uq_customers_cuit_per_tenant`` — anti
    duplicado por documento. Parciales: solo filas con doc no nulo, activas y NO
    sentinela (un import no puede crear dos clientes con el mismo DNI/CUIT).

Todo nullable/additive — no reescribe datos. ``downgrade`` simétrico. El backfill
de ventas históricas sin cliente → "Local" va en el script acompañante
``scripts/backfill_local_customer.py`` (revisable/idempotente, no inline).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260721_0001"
down_revision = "20260720_0004"
branch_labels = None
depends_on = None

_SENTINEL_INDEX = "uq_customers_sentinel_per_tenant"
_DNI_INDEX = "uq_customers_dni_per_tenant"
_CUIT_INDEX = "uq_customers_cuit_per_tenant"

_NEW_COLUMNS = (
    ("customer_type", sa.String(length=10)),
    ("last_name", sa.String(length=200)),
    ("doc_type", sa.String(length=10)),
    ("dni", sa.String(length=15)),
    ("cuit", sa.String(length=13)),
    ("iva_condition", sa.String(length=25)),
    ("address", sa.Text()),
    ("locality", sa.String(length=120)),
    ("province", sa.String(length=120)),
    ("postal_code", sa.String(length=12)),
    ("birthday", sa.Date()),
)

# Excluye sentinela y soft-deleted: el documento solo debe ser único entre clientes
# reales y activos.
_DOC_PARTIAL = (
    "deactivated_at IS NULL "
    "AND coalesce(custom_fields->>'_sentinel', '') <> 'true'"
)


def upgrade() -> None:
    for name, type_ in _NEW_COLUMNS:
        op.add_column("customers", sa.Column(name, type_, nullable=True))
    # UN solo sentinela "Local" activo por tenant.
    op.execute(
        f"CREATE UNIQUE INDEX {_SENTINEL_INDEX} ON customers (tenant_id) "
        "WHERE (custom_fields->>'_sentinel') = 'true'"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_DNI_INDEX} ON customers (tenant_id, dni) "
        f"WHERE dni IS NOT NULL AND {_DOC_PARTIAL}"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_CUIT_INDEX} ON customers (tenant_id, cuit) "
        f"WHERE cuit IS NOT NULL AND {_DOC_PARTIAL}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_CUIT_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_DNI_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_SENTINEL_INDEX}")
    for name, _type in reversed(_NEW_COLUMNS):
        op.drop_column("customers", name)
