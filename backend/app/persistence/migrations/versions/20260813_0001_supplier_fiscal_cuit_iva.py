"""Proveedores: CUIT y condición de IVA (espejo de la ficha fiscal de clientes)

Revision ID: 20260813_0001
Revises: 20260810_0001
Create Date: 2026-08-13

Contexto
--------
Migración ADDITIVE. La Reforma de Proveedores le dio a ``suppliers`` un solo
campo fiscal, ``cuil``, pensado para el proveedor PERSONA. Pero la mayoría de
los proveedores de una PYME son empresas, y una empresa no tiene CUIL: tiene
CUIT. El resultado es que el dato fiscal del proveedor típico no se podía
guardar en ningún lado.

Se detectó midiendo encabezados reales (``scripts/diag_unmapped_headers.py``):
en un padrón de proveedores, «CUIT» y «Condición IVA» quedaban sin destino. El
reconocedor F-M los entendía y contestaba "esta hoja no tiene un campo para eso"
— honesto, pero el dato se perdía. ``customers`` ya tenía los dos desde
``20260721_0001``; esto cierra la asimetría.

  1. ``cuit`` (String(13), nullable) — validado por formato y dígito verificador
     (módulo 11) en el schema, con el MISMO helper que clientes
     (``schemas/_ar_fiscal.py``). No se rellena ni se deriva de ``cuil``: son
     campos distintos y adivinar cuál es cuál sobre datos ya cargados es
     inventar (regla de no-invención).
  2. ``iva_condition`` (String(25), nullable) — consumidor_final | monotributo |
     responsable_inscripto | exento. Mismo catálogo cerrado que clientes.

``cuil`` se CONSERVA: un proveedor monotributista persona física sigue teniendo
CUIL, y borrarlo migraría datos ya cargados sin necesidad.

Deliberadamente SIN índice único por CUIT
-----------------------------------------
``customers`` tiene ``uq_customers_cuit_per_tenant`` (anti-duplicado de import).
Acá no se replica: el import de proveedores deduplica por nombre, no hay
evidencia de duplicados por CUIT, y un índice único nuevo puede hacer FALLAR
imports que hoy pasan. Es un cambio de comportamiento y merece su propia
decisión, no viajar de polizón en una migración additive.

``downgrade`` simétrico. Todo nullable — no reescribe ninguna fila.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260813_0001"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("cuit", sa.String(length=13), nullable=True))
    op.add_column(
        "suppliers", sa.Column("iva_condition", sa.String(length=25), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("suppliers", "iva_condition")
    op.drop_column("suppliers", "cuit")
