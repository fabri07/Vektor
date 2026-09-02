"""Código interno de producto — el que Véktor genera cuando el archivo no trae SKU.

Tres identificadores distintos conviven en un producto, y confundirlos es el
error que este módulo viene a evitar:

- ``products.id`` (UUID) — identidad TÉCNICA. Une las tablas
  (``sales_entries.product_id``, ``inventory_movements.product_id``). No se
  muestra ni se escribe en una etiqueta.
- ``products.internal_sku`` — código COMERCIAL propio. Estable, legible,
  buscable. Lo genera Véktor.
- ``products.sku`` — el código que aporta el ARCHIVO o el proveedor. Puede
  faltar, puede cambiar, puede repetirse entre proveedores distintos.

Sólo los productos tienen SKU. Ventas, gastos, clientes y proveedores tienen
``id`` y, si acaso, un código de otra semántica (número de comprobante, CUIT)
que no se llama SKU ni se genera acá.

**Derivado del UUID, no aleatorio.** El mismo producto da siempre el mismo
código, sin estado, sin consultar la base y sin depender del orden de inserción.
Eso lo vuelve reproducible en un test y, sobre todo, imposible de "regenerar
distinto" por accidente en una relectura.
"""

from __future__ import annotations

import uuid

#: Prefijo visible. Identifica de un vistazo que el código lo puso Véktor y no
#: el proveedor — la diferencia que el usuario necesita para saber si puede
#: buscarlo en el catálogo de su proveedor (no puede).
INTERNAL_SKU_PREFIX = "VKT-"

#: Crockford base32: sin ``I``, ``L``, ``O`` ni ``U``. Las tres primeras se
#: confunden con ``1``/``0`` cuando alguien copia un código a mano o lo lee de
#: una etiqueta; la ``U`` se saca para no formar palabras desafortunadas.
_ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 12 caracteres × 5 bits = 60 bits del UUID. Con 2^60 ≈ 1,15 × 10^18 valores,
#: un tenant con un millón de productos tiene ~4 × 10^-7 de probabilidad de
#: colisión (paradoja del cumpleaños). El índice único la convierte en un error
#: ruidoso, no en datos mezclados — ver la nota de ``_UQ_NAMES`` en
#: ``product_identity``.
_LARGO = 12
_BITS = _LARGO * 5


def generate_internal_sku(product_id: uuid.UUID) -> str:
    """Código interno determinístico para este producto.

    Toma los ``_BITS`` menos significativos del UUID. No se usan los más
    significativos porque en un UUID v4 ahí viven los bits de versión y variante,
    que son constantes: incluirlos gastaría caracteres del código en información
    que no distingue nada.
    """
    valor = product_id.int & ((1 << _BITS) - 1)
    chars = []
    for _ in range(_LARGO):
        chars.append(_ALFABETO[valor & 0x1F])
        valor >>= 5
    return INTERNAL_SKU_PREFIX + "".join(reversed(chars))


def is_internal_sku(code: str | None) -> bool:
    """¿Este código lo generó Véktor?

    Se usa para decidir si la pantalla lo muestra con el chip "Generado". Mira el
    prefijo y el largo: un código del proveedor que empiece con "VKT-" por
    casualidad no va a tener además exactamente 12 caracteres del alfabeto.
    """
    if not code or not code.startswith(INTERNAL_SKU_PREFIX):
        return False
    cuerpo = code[len(INTERNAL_SKU_PREFIX) :]
    return len(cuerpo) == _LARGO and all(c in _ALFABETO for c in cuerpo)
