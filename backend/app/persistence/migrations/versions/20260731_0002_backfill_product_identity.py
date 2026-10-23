"""Backfill de las columnas *_normalized de productos legacy (Fase 2)

Revision ID: 20260731_0002
Revises: 20260731_0001
Create Date: 2026-07-31

Contexto
--------
La migración T1 (``20260731_0001``) agregó las 4 columnas ``*_normalized`` como
NULL y NO backfilleó: el listener de ``Product`` solo las llena en el próximo
insert/update de cada fila. Consecuencia (bloqueante de deploy detectado en el
review de F2): un producto EXISTENTE queda con ``name_normalized IS NULL`` y por
lo tanto NO entra en ningún índice de identidad ni matchea en el dup-check SQL de
``POST /products`` (``_find_active_product_by_identity``). Si F2 se despliega sin
este backfill, una importación puede concluir "no existe" y crear un duplicado —
exactamente lo que F2 busca evitar.

Esta migración recorre los productos legacy (``name_normalized IS NULL``) y
recomputa las 4 columnas con los MISMOS normalizadores canónicos que usa el
listener (``app/domain/text_norm``, fuente única de cálculo — no se reimplementa
la normalización en SQL, que no puede hacer NFKD/casefold de forma fiel).
``brand_normalized`` sale de ``custom_fields["marca"]`` igual que el listener.

Idempotente: ``WHERE name_normalized IS NULL`` selecciona exactamente el conjunto
legacy; una segunda corrida no toca nada (las filas ya backfilleadas dejan de
matchear el filtro). ``downgrade`` es un no-op consciente: revertir las columnas a
NULL no aporta y arriesga perder identidad recién persistida por el listener sobre
las mismas filas; la reversión estructural la hace el ``downgrade`` de T1.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op
from app.domain.text_norm import (
    normalize_barcode,
    normalize_brand,
    normalize_product_name,
    normalize_sku,
)

revision = "20260731_0002"
down_revision = "20260731_0001"
branch_labels = None
depends_on = None

_BATCH = 500


def _marca_from_custom_fields(raw: object) -> str | None:
    """``custom_fields`` puede llegar como dict (JSONB/psycopg) o str (JSON)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, dict):
        marca = raw.get("marca")
        return marca if isinstance(marca, str) else None
    return None


def upgrade() -> None:
    bind = op.get_bind()
    # ``name_normalized`` queda SIEMPRE non-NULL tras procesar una fila
    # (``normalize_product_name`` devuelve "" en el peor caso), así que cada lote
    # reduce estrictamente el conjunto ``IS NULL`` → basta releer siempre desde el
    # tope hasta agotarlo, sin offset.
    select_legacy = sa.text(
        "SELECT id, name, sku, barcode, custom_fields "
        "FROM products WHERE name_normalized IS NULL "
        "ORDER BY id LIMIT :batch"
    )
    update_row = sa.text(
        "UPDATE products SET "
        "name_normalized = :name_n, "
        "sku_normalized = :sku_n, "
        "barcode_normalized = :barcode_n, "
        "brand_normalized = :brand_n "
        "WHERE id = :id"
    )

    while True:
        rows = bind.execute(select_legacy, {"batch": _BATCH}).fetchall()
        if not rows:
            break
        for pid, name, sku, barcode, custom_fields in rows:
            marca = _marca_from_custom_fields(custom_fields)
            bind.execute(
                update_row,
                {
                    "id": pid,
                    "name_n": normalize_product_name(name),
                    "sku_n": normalize_sku(sku),
                    "barcode_n": normalize_barcode(barcode),
                    "brand_n": normalize_brand(marca),
                },
            )


def downgrade() -> None:
    # No-op consciente: ver docstring. La reversión estructural es de T1.
    pass
