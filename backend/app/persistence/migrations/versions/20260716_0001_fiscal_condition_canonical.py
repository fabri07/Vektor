"""Normaliza business_profiles.fiscal_condition a valores canónicos

Revision ID: 20260716_0001
Revises: 20260715_0001
Create Date: 2026-07-16

Contexto
--------
Migración ADDITIVE / reconciliadora. La migración previa (20260715_0001) dejó la
columna ``business_profiles.fiscal_condition`` como ``String(12)`` con un CHECK
``IN ('registered', 'informal')``. Pero la capa de dominio
(``app/domain/fiscal_condition.py``), el ORM (``Text``) y los schemas usan tres
valores canónicos: ``monotributo`` | ``responsable_inscripto`` | ``informal``.

Consecuencia: ``PATCH /settings/fiscal-condition`` persiste el valor canónico
elegido por Pydantic, y contra Postgres eso ROMPE por dos motivos:
  1. ``responsable_inscripto`` (21 chars) excede ``String(12)``.
  2. ``monotributo`` / ``responsable_inscripto`` no están en el viejo CHECK.

Esta migración reconcilia la DB con el contrato de la app:
  - Ensancha la columna a ``Text`` (sin límite, igual que el ORM).
  - Normaliza el legacy ``'registered'`` → ``'monotributo'`` in-place
    (mismo default que ``normalize_fiscal_condition`` al leer), reescribiendo la
    fila para no depender de la normalización on-read.
  - Reemplaza el CHECK por uno que acepta los 3 valores canónicos + NULL.

Es reversible: ``downgrade`` revierte ``'monotributo'`` → ``'registered'`` (best
effort, no distingue monotributo nativo de un RI; ambos caían en 'registered' en
el esquema viejo), reinstala el CHECK viejo y devuelve la columna a String(12).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260716_0001"
down_revision = "20260715_0001"
branch_labels = None
depends_on = None

_NEW_CHECK = "ck_business_profiles_fiscal_condition"
_CANONICAL_VALUES = "('monotributo', 'responsable_inscripto', 'informal')"
_LEGACY_VALUES = "('registered', 'informal')"


def upgrade() -> None:
    # 1. Soltar el CHECK viejo ANTES de tocar datos o tipo (no puede validar los
    #    valores canónicos que vamos a permitir).
    op.drop_constraint(_NEW_CHECK, "business_profiles", type_="check")

    # 2. Ensanchar la columna a Text (alinea con el ORM; permite
    #    'responsable_inscripto' de 21 chars).
    op.alter_column(
        "business_profiles",
        "fiscal_condition",
        type_=sa.Text(),
        existing_type=sa.String(12),
        existing_nullable=True,
    )

    # 3. Normalizar el legacy in-place. 'registered' agrupaba monotributo + RI;
    #    el default histórico de lectura es 'monotributo'.
    op.execute(
        "UPDATE business_profiles "
        "SET fiscal_condition = 'monotributo' "
        "WHERE fiscal_condition = 'registered'"
    )

    # 4. Reinstalar el CHECK con los valores canónicos + NULL.
    op.create_check_constraint(
        _NEW_CHECK,
        "business_profiles",
        f"fiscal_condition IS NULL OR fiscal_condition IN {_CANONICAL_VALUES}",
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CHECK, "business_profiles", type_="check")

    # Revertir canónicos al esquema viejo: 'monotributo' y 'responsable_inscripto'
    # caían ambos en 'registered'. 'informal' / NULL quedan igual.
    op.execute(
        "UPDATE business_profiles "
        "SET fiscal_condition = 'registered' "
        "WHERE fiscal_condition IN ('monotributo', 'responsable_inscripto')"
    )

    op.alter_column(
        "business_profiles",
        "fiscal_condition",
        type_=sa.String(12),
        existing_type=sa.Text(),
        existing_nullable=True,
    )

    op.create_check_constraint(
        _NEW_CHECK,
        "business_profiles",
        f"fiscal_condition IS NULL OR fiscal_condition IN {_LEGACY_VALUES}",
    )
