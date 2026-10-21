"""Lote de stock inicial a mano: coincidencias y token (B·5c).

El stock inicial es mercadería que compraron el dueño o los socios: va al costo
y se vende a otro precio. Se puede cargar en tabla desde el primer día, sin un
archivo. El lote va en dos pasos para no duplicar productos sin que el usuario
lo elija:

1. **Preview:** por fila, los errores y las coincidencias con productos activos
   por SKU, código de barras y nombre normalizado, más un ``preview_token`` (el
   hash de las filas normalizadas y sus coincidencias).
2. **Confirmar:** se recalcula todo con el lock exclusivo de alta tomado; si el
   token no coincide, algo cambió entre el preview y la confirmación (409).

Este módulo no escribe: el alta de cada fila es la de ``POST /products``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.apertura_service import productos_con_apertura_viva
from app.domain.text_norm import normalize_barcode, normalize_product_name, normalize_sku
from app.persistence.models.product import Product
from app.schemas.product import FilaStockInicial

Por = Literal["sku", "barcode", "nombre"]
_CENTAVO = Decimal("0.01")
_ORDEN: dict[str, int] = {"sku": 0, "barcode": 1, "nombre": 2}


@dataclass(frozen=True)
class Coincidencia:
    product_id: uuid.UUID
    nombre: str
    por: Por
    necesita_stock_inicial: bool


@dataclass
class FilaAnalizada:
    fila: int
    errores: list[str] = field(default_factory=list)
    coincidencias: list[Coincidencia] = field(default_factory=list)

    @property
    def por_identidad(self) -> bool:
        """Coincide por SKU o barcode: crear otro chocaría con el índice único."""
        return any(c.por != "nombre" for c in self.coincidencias)


def _claves(f: FilaStockInicial) -> tuple[str, str | None, str | None]:
    return normalize_product_name(f.nombre), normalize_sku(f.sku), normalize_barcode(f.barcode)


async def analizar(
    session: AsyncSession, tenant_id: uuid.UUID, filas: list[FilaStockInicial]
) -> list[FilaAnalizada]:
    """Errores y coincidencias de cada fila, contra los productos ACTIVOS."""
    claves = [_claves(f) for f in filas]
    nombres = {n for n, _, _ in claves if n}
    skus = {s for _, s, _ in claves if s}
    barcodes = {b for _, _, b in claves if b}
    condiciones = [Product.name_normalized.in_(nombres)] if nombres else []
    if skus:
        condiciones.append(Product.sku_normalized.in_(skus))
    if barcodes:
        condiciones.append(Product.barcode_normalized.in_(barcodes))
    productos: list[Product] = []
    if condiciones:
        productos = list(
            (
                await session.execute(
                    select(Product)
                    .where(
                        Product.tenant_id == tenant_id,
                        Product.is_active.is_(True),
                        or_(*condiciones),
                    )
                    .order_by(Product.id)
                )
            ).scalars()
        )
    con_apertura = await productos_con_apertura_viva(
        session, tenant_id, [p.id for p in productos]
    )

    vistas: dict[tuple[str, str], int] = {}
    out: list[FilaAnalizada] = []
    for i, (nombre, sku, barcode) in enumerate(claves):
        a = FilaAnalizada(fila=i)
        if not nombre:
            a.errores.append("El nombre no puede quedar vacío.")
        # Dos filas del lote que son el mismo producto: el usuario tiene que
        # unirlas; el lote no elige cuál vale.
        for tipo, valor in (("sku", sku), ("barcode", barcode), ("nombre", nombre)):
            if not valor:
                continue
            otra = vistas.get((tipo, valor))
            if otra is not None:
                a.errores.append(f"Repite el {tipo} de la fila {otra + 1}.")
            else:
                vistas[(tipo, valor)] = i
        vistos: set[uuid.UUID] = set()
        for p in productos:
            por: Por | None = None
            if sku and p.sku_normalized == sku:
                por = "sku"
            elif barcode and p.barcode_normalized == barcode:
                por = "barcode"
            elif nombre and p.name_normalized == nombre:
                por = "nombre"
            if por is None or p.id in vistos:
                continue
            vistos.add(p.id)
            a.coincidencias.append(
                Coincidencia(
                    product_id=p.id,
                    nombre=p.name,
                    por=por,
                    necesita_stock_inicial=p.id not in con_apertura,
                )
            )
        a.coincidencias.sort(key=lambda c: (_ORDEN[c.por], str(c.product_id)))
        out.append(a)
    return out


def token(filas: list[FilaStockInicial], analisis: list[FilaAnalizada]) -> str:
    """Hash de lo que el usuario vio: las filas normalizadas y sus coincidencias.

    Cambia si aparece o desaparece un producto que coincide, o si uno pasa a
    tener stock inicial: la confirmación ya no es sobre lo que se eligió.
    """
    cuerpo = [
        {
            "claves": list(_claves(f)),
            # A centavos: «1000» y «1000.00» son el mismo precio, no un cambio.
            "precio": str(f.sale_price_ars.quantize(_CENTAVO)),
            "costo": str(f.unit_cost_ars.quantize(_CENTAVO)),
            "stock": f.stock_units,
            "envio": f.costo_envio,
            "categoria": (f.categoria or "").strip().lower(),
            "coincidencias": [
                [str(c.product_id), c.por, c.necesita_stock_inicial] for c in a.coincidencias
            ],
        }
        for f, a in zip(filas, analisis, strict=True)
    ]
    crudo = json.dumps(cuerpo, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(crudo.encode()).hexdigest()


__all__ = ["Coincidencia", "FilaAnalizada", "analizar", "token"]
