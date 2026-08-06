"""F-H3.a — qué le hace al inventario cada hoja de un archivo importado.

Es un eje **propio**, al lado de ``stock_treatment`` y no encima. Los dos hablan
de stock, pero responden preguntas distintas:

``stock_treatment`` (F10)   ¿el stock inicial de un CATÁLOGO es una compra real
                            (COGS + baja de caja) o un saldo de apertura?
                            → pregunta CONTABLE, sobre plata.

``inventory_effect`` (acá)  ¿cómo afectan al INVENTARIO las filas de esta hoja?
                            → pregunta de stock, sobre unidades.

Fusionarlos —que es lo que proponía el plan original con la equivalencia
``historical_replay ≈ purchase``— haría que un usuario que elige "las ventas de
esta hoja descuentan stock" declare **en silencio** que su catálogo genera COGS
y baja de caja. Son dos decisiones y el usuario toma las dos por separado.

**El default nunca es ``historical_replay``.** Aplicar el histórico
automáticamente es peligroso cuando el archivo puede estar incompleto o
solaparse con saldos ya cargados: un archivo real de 10.931 ventas movería el
inventario entero en una sola confirmación, y la parte que ya estaba contada en
el saldo de apertura se descontaría dos veces. Véktor calcula el impacto y lo
muestra; el replay es una elección explícita por hoja. La regla está clavada en
``test_historical_replay_nunca_es_un_default``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

#: Calcula el impacto y lo reporta, SIN tocar stock. Default de todo lo que
#: mueve unidades: es el único modo que no puede arruinar un inventario.
INFORMATIONAL = "informational"
#: Compras suman y ventas restan: el stock final refleja la historia del archivo.
#: NUNCA por default — sólo por elección explícita del usuario para esa hoja.
HISTORICAL_REPLAY = "historical_replay"
#: El archivo declara el saldo ABSOLUTO final (un catálogo con el stock de hoy).
#: No es una secuencia de movimientos: es una foto.
CURRENT_SNAPSHOT = "current_snapshot"
#: La cantidad es un dato transaccional que no habla de inventario (una venta de
#: servicios, un resumen contable, el libro diario).
NO_INVENTORY = "no_inventory"

#: Clave de ``custom_fields`` donde una venta importada guarda de QUÉ HOJA vino
#: (F-H3.d.2). No existe otro link: ``source_row_ref`` es el sha256 del ancla y no
#: se puede volver atrás. Sin esto el replay sólo podría aplicarse al archivo
#: entero, y un libro con una hoja `informational` y otra `historical_replay`
#: terminaría aplicando las dos — o sea, ignorando lo que el usuario declaró.
IMPORT_CONTEXT_FIELD = "_import_context"

InventoryEffect = Literal[
    "informational",
    "historical_replay",
    "current_snapshot",
    "no_inventory",
]

VALID_EFFECTS: frozenset[str] = frozenset(
    {INFORMATIONAL, HISTORICAL_REPLAY, CURRENT_SNAPSHOT, NO_INVENTORY}
)

#: Etiquetas para los mensajes de error y la UI. En castellano, describiendo lo
#: que PASA con el stock, no el nombre técnico del modo.
EFFECT_LABELS: dict[str, str] = {
    INFORMATIONAL: "Calcular el impacto sin modificar el stock",
    HISTORICAL_REPLAY: "Aplicar la historia: las compras suman y las ventas restan",
    CURRENT_SNAPSHOT: "El archivo declara el stock actual (saldo absoluto)",
    NO_INVENTORY: "Estas cantidades no afectan el inventario",
}

#: Campos de mapeo que identifican al producto de una fila. Sin ninguno de estos,
#: la fila no habla de un producto y por lo tanto no puede mover inventario.
_PRODUCT_FIELDS: frozenset[str] = frozenset({"product_name", "name", "sku", "barcode"})
#: Sin cantidad no hay unidades que mover. `stock_units` es la cantidad de un
#: catálogo (saldo absoluto); `quantity`, la de un movimiento.
_QUANTITY_FIELDS: frozenset[str] = frozenset({"quantity", "stock_units"})


@dataclass(frozen=True)
class SheetInventoryProfile:
    """Lo que se sabe de una hoja para elegir su default. Sin sesión ni ORM."""

    context_id: str
    #: `sale` | `expense` | `product` | `customer` | `supplier` | None
    entity: str | None
    #: Campos canónicos a los que apunta al menos una columna de esta hoja.
    mapped_fields: frozenset[str]

    @property
    def identifies_product(self) -> bool:
        return bool(self.mapped_fields & _PRODUCT_FIELDS)

    @property
    def has_quantity(self) -> bool:
        return bool(self.mapped_fields & _QUANTITY_FIELDS)

    @property
    def moves_units(self) -> bool:
        """¿Esta hoja habla de unidades de un producto identificable?

        Las dos condiciones juntas: una venta con cantidad pero sin producto no
        mueve inventario (servicios, honorarios), y un producto sin cantidad
        tampoco (una lista de precios).
        """
        return self.identifies_product and self.has_quantity


def default_effect_for(profile: SheetInventoryProfile) -> str:
    """El modo que Véktor propone para una hoja. Nunca ``historical_replay``.

    - Un **catálogo** declara el stock que hay hoy → ``current_snapshot``. Es una
      foto, no una secuencia: aplicarle un replay sería leer un saldo como si
      fuera un movimiento.
    - Ventas y compras **históricas** → ``informational``. Se calcula el impacto
      y se muestra; el usuario decide si lo aplica.
    - Todo lo demás —una venta sin producto, un resumen contable, una hoja de
      maestros— → ``no_inventory``.
    """
    if profile.entity == "product":
        # Un catálogo SIN cantidad es una lista de precios: no declara saldo.
        return CURRENT_SNAPSHOT if profile.has_quantity else NO_INVENTORY
    if profile.entity in ("sale", "expense") and profile.moves_units:
        return INFORMATIONAL
    return NO_INVENTORY


class InvalidInventoryEffectError(ValueError):
    """Valor o contexto inválido en el `inventory_effect` que mandó el cliente."""


def resolve_inventory_effects(
    profiles: list[SheetInventoryProfile],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Modo EFECTIVO por hoja: el default de Véktor, pisado por lo que eligió el usuario.

    Levanta ``InvalidInventoryEffectError`` si un override trae un modo desconocido o
    apunta a una hoja que no existe en el archivo. Un override que no se puede
    honrar no se ignora en silencio: significa que el usuario cree haber decidido
    algo sobre el inventario que no va a pasar.
    """
    resolved = {p.context_id: default_effect_for(p) for p in profiles}
    if not overrides:
        return resolved
    for context_id, effect in overrides.items():
        if effect not in VALID_EFFECTS:
            raise InvalidInventoryEffectError(
                f"«{effect}» no es un efecto de inventario válido. "
                f"Opciones: {', '.join(sorted(VALID_EFFECTS))}."
            )
        if context_id not in resolved:
            raise InvalidInventoryEffectError(
                f"El efecto de inventario apunta a una hoja que no está en el archivo "
                f"(«{context_id}»)."
            )
        resolved[context_id] = effect
    return resolved
