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

**F-F.4 — el eje dejó de ser una pregunta.** Hasta F-F.3 había cuatro modos y el
default de todo lo que movía unidades era ``informational``: se calculaba el
impacto y el stock no se tocaba hasta que el usuario lo pidiera hoja por hoja.
Eso desapareció, y no por conveniencia. Dos cosas lo hacen posible, y si alguna
se cae hay que volver atrás:

1. **El ancla del catálogo se aplica antes de todos los eventos.** Un catálogo
   declara un absoluto sin fecha de negocio (``inventory_temporal_service`` lo
   documenta), así que entra como saldo de apertura y la cronología gobierna sólo
   a compras y ventas entre sí. Ésa es la pieza que faltaba en el incidente don
   pedro, donde la parte ya contada en el saldo se descontó dos veces.
2. **El replay ordena por FECHA, no por solapa** (F-F.1): las compras del propio
   archivo entran como créditos datados, así que una compra del 20/03 no respalda
   una venta del 10/03.

Con las dos puestas, seguir preguntando «¿estas ventas modifican el stock?» era
pedirle al usuario que decidiera algo que el contenido de la hoja ya responde:
si es compra o venta de mercadería, mueve inventario. El eje pasa a **derivarse**
de la entidad efectiva de la hoja y de los campos que el mapeo cubre.

**Lo que el usuario sigue eligiendo** (y esto NO se toca acá): a qué sección
corresponde cada hoja, el mapeo de cada columna, y ``stock_treatment`` — que es
la pregunta contable de al lado, no ésta.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

#: Compras suman y ventas restan: el stock refleja la historia del archivo.
#: Es lo que le pasa a toda hoja de compra o venta de mercadería.
HISTORICAL_REPLAY = "historical_replay"
#: El archivo declara el saldo ABSOLUTO final (un catálogo con el stock de hoy).
#: No es una secuencia de movimientos: es una foto.
CURRENT_SNAPSHOT = "current_snapshot"

#: Los dos modos que F-F.4 eliminó, y que no se reemplazan por otro modo.
#:
#: ``informational`` era una DECISIÓN («calculá el impacto pero no toques el
#: stock») y se elimina porque dejó de existir como decisión. ``no_inventory``
#: era otra cosa: un modo que representaba la AUSENCIA de inventario, y su
#: reemplazo no es un valor sino ``None`` — la hoja no tiene efecto porque no
#: habla de unidades, y eso se dice no diciendo nada.
#:
#: Se siguen aceptando en el payload y se DESCARTAN (ver
#: ``discard_legacy_overrides``): Railway y Vercel redespliegan en paralelo y sin
#: orden garantizado, así que durante la ventana de deploy un frontend viejo los
#: manda contra un backend nuevo. Rechazarlos tiraría abajo el confirm por un
#: valor que ya no significa nada.
LEGACY_EFFECTS: frozenset[str] = frozenset({"informational", "no_inventory"})

#: Clave de ``custom_fields`` donde una venta importada guarda de QUÉ HOJA vino
#: (F-H3.d.2). No existe otro link: ``source_row_ref`` es el sha256 del ancla y no
#: se puede volver atrás. Sin esto el replay sólo podría aplicarse al archivo
#: entero, y un libro con una hoja de servicios y otra de mercadería terminaría
#: descontando las dos.
IMPORT_CONTEXT_FIELD = "_import_context"

InventoryEffect = Literal[
    "historical_replay",
    "current_snapshot",
]

VALID_EFFECTS: frozenset[str] = frozenset({HISTORICAL_REPLAY, CURRENT_SNAPSHOT})

#: Etiquetas para los mensajes de error y la UI. En castellano, describiendo lo
#: que PASA con el stock, no el nombre técnico del modo.
#:
#: **El alcance es la HOJA, no el archivo.** Un catálogo en `current_snapshot` y
#: una hoja de ventas en `historical_replay` conviven en el mismo libro: el
#: catálogo deja el stock en su saldo y las ventas lo descuentan desde ahí. Decir
#: algo sobre "este archivo" sería falso para alguna de las dos, así que las
#: etiquetas hablan de las filas de ESTA hoja.
#:
#: Están en INDICATIVO y no en infinitivo: desde F-F.4 describen lo que va a
#: pasar, no una opción que se ofrece. "Aplicar la historia" era el nombre de un
#: botón; ya no hay botón.
EFFECT_LABELS: dict[str, str] = {
    HISTORICAL_REPLAY: "Las compras suman y las ventas restan del inventario",
    CURRENT_SNAPSHOT: "El archivo declara el stock actual (saldo absoluto)",
}

#: Campos de mapeo que identifican al producto de una fila. Sin ninguno de estos,
#: la fila no habla de un producto y por lo tanto no puede mover inventario.
_PRODUCT_FIELDS: frozenset[str] = frozenset({"product_name", "name", "sku", "barcode"})
#: Sin cantidad no hay unidades que mover. `stock_units` es la cantidad de un
#: catálogo (saldo absoluto); `quantity`, la de un movimiento.
_QUANTITY_FIELDS: frozenset[str] = frozenset({"quantity", "stock_units"})


@dataclass(frozen=True)
class SheetInventoryProfile:
    """Lo que se sabe de una hoja para deducir su efecto. Sin sesión ni ORM."""

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


def default_effect_for(profile: SheetInventoryProfile) -> str | None:
    """Qué le hace al inventario esta hoja. ``None`` = no habla de inventario.

    - Un **catálogo** con cantidad declara el stock que hay hoy →
      ``current_snapshot``. Es una foto, no una secuencia: aplicarle un replay
      sería leer un saldo como si fuera un movimiento.
    - Compra o venta de **mercadería** —producto identificable y cantidad— →
      ``historical_replay``. Es la función central: lo que se compró suma y lo que
      se vendió resta, en el orden en que pasó.
    - Todo lo demás —una venta de servicios sin producto, una lista de precios,
      una hoja de gastos fijos, un maestro de clientes— → ``None``. No es un modo
      que dice "no toques el stock": es que la pregunta no aplica.

    El ``None`` NO es un descuido de tipos: es lo que reemplaza a ``no_inventory``
    y lo que permite que la pantalla no muestre una línea sobre inventario en una
    hoja que no habla de eso.
    """
    if profile.entity == "product":
        # Un catálogo SIN cantidad es una lista de precios: no declara saldo.
        return CURRENT_SNAPSHOT if profile.has_quantity else None
    if profile.entity in ("sale", "expense") and profile.moves_units:
        return HISTORICAL_REPLAY
    return None


def options_for(profile: SheetInventoryProfile) -> list[str]:
    """Lo que la pantalla muestra para esta hoja: su efecto, o nada.

    **Ya no ofrece: explica.** Hasta F-F.3 devolvía entre dos y tres modos y el
    usuario elegía. Desde F-F.4 el efecto se deriva del contenido de la hoja, así
    que la lista tiene exactamente un elemento —el efecto— o ninguno.

    Se conserva como LISTA, y no se reemplaza por el escalar, por una razón
    concreta: `/inventory-effects` la sirve tal cual y el frontend la consume. Un
    frontend viejo (la ventana de deploy, que en este repo es real porque Vercel y
    Railway redespliegan en paralelo) renderiza una lista de un elemento como una
    línea informativa, que es exactamente lo correcto. Cambiar la forma lo
    rompería para no ganar nada.
    """
    default = default_effect_for(profile)
    return [] if default is None else [default]


class InvalidInventoryEffectError(ValueError):
    """Valor o contexto inválido en el `inventory_effect` que mandó el cliente."""


def discard_legacy_overrides(
    overrides: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """Saca del payload los modos que F-F.4 eliminó. Devuelve ``(limpios, descartados)``.

    Se separa de ``resolve_inventory_effects`` —que sigue siendo estricta— porque
    son dos situaciones distintas: un modo DESCONOCIDO es un bug del cliente y
    tiene que explotar; ``informational``/``no_inventory`` son un cliente viejo
    durante la ventana de deploy y descartarlos no pierde ninguna intención que
    siga existiendo (dejaron de ser decisiones).

    El caller loguea los descartados: acá no, porque este módulo es puro.
    """
    if not overrides:
        return {}, []
    limpios = {k: v for k, v in overrides.items() if v not in LEGACY_EFFECTS}
    descartados = [k for k, v in overrides.items() if v in LEGACY_EFFECTS]
    return limpios, descartados


def resolve_inventory_effects(
    profiles: list[SheetInventoryProfile],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Efecto EFECTIVO por hoja. Las hojas sin efecto NO entran en el dict.

    Que la hoja sin efecto se omita (en vez de mapear a un valor) es lo que hace
    que ``"sin dato"`` deje de ser ambiguo aguas abajo: hasta F-F.3 significaba a
    la vez «caller viejo que no mandó el modo» y «hoja que no habla de
    inventario», y el recorder tenía que adivinar cuál.

    Los ``overrides`` sobreviven por compatibilidad de API, pero ya no pueden
    cambiar nada: el efecto se deriva, así que lo único que un override puede
    hacer es coincidir. Se levanta ``InvalidInventoryEffectError`` si trae un modo
    desconocido, si apunta a una hoja que no existe, o si **contradice** el efecto
    derivado — los tres casos significan que el cliente cree haber decidido algo
    sobre el inventario que no va a pasar, y eso no se ignora en silencio. Los
    modos legacy hay que sacarlos ANTES con ``discard_legacy_overrides``.
    """
    resolved = {
        p.context_id: efecto
        for p in profiles
        if (efecto := default_effect_for(p)) is not None
    }
    if not overrides:
        return resolved
    conocidos = {p.context_id for p in profiles}
    for context_id, effect in overrides.items():
        if effect not in VALID_EFFECTS:
            raise InvalidInventoryEffectError(
                f"«{effect}» no es un efecto de inventario válido. "
                f"Opciones: {', '.join(sorted(VALID_EFFECTS))}."
            )
        if context_id not in conocidos:
            raise InvalidInventoryEffectError(
                f"El efecto de inventario apunta a una hoja que no está en el archivo "
                f"(«{context_id}»)."
            )
        if resolved.get(context_id) != effect:
            raise InvalidInventoryEffectError(
                f"El efecto de inventario de «{context_id}» ya no se elige: se deduce "
                f"de lo que la hoja contiene. Si es compra o venta de mercadería, mueve "
                f"el inventario; si no, no habla de inventario."
            )
    return resolved
