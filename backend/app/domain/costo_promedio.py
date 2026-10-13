"""Costo promedio ponderado móvil de un producto (M·P1, fase 2).

Puro: sin sesión, sin ORM y sin tenant. Recorre la historia de UN producto y
responde «¿cuál era el costo promedio de este producto a la fecha F?».

**Es un dato, no el costo principal.** El costo de cada venta sigue siendo el de
reposición (última compra, ``costo_de_referencia``): con inflación alta el
promedio arrastra compras viejas de nominal bajo y subestima lo que cuesta
reponer. El promedio alimenta el «margen estimado con costo promedio» y la
«brecha entre costo promedio y reposición» (``docs/plans/mp-costo-promedio-ponderado.md``).
Nunca se confirma.

Reglas:

- **Fórmula:** cada compra con costo conocido recalcula
  ``prom = (u_conocidas × prom + qty × costo) / (u_conocidas + qty)``. Las
  ventas, mermas y ajustes mueven las unidades, no el promedio. Las cantidades
  van en unidades BASE y el costo por unidad de venta: el factor se cancela al
  ponderar (todas las cantidades están en la misma unidad), así que el promedio
  sale por unidad de venta, como ``domain/sale_unit.importe`` lo espera.
- **Orden determinista:** día de negocio → clase (ancla, apertura, compra,
  entrada, complejo, salida: dentro del mismo día los créditos van antes que
  los débitos) → hora → ``creado`` → ``id``.
- **Compra o entrada sin costo:** suma a ``u_sin_costo`` y NO entra en el
  promedio. Nunca se valoriza en silencio una unidad sin costo al promedio
  anterior. El estado pasa a ``incompleto`` con la fracción sin costo; las
  salidas consumen las dos cantidades en proporción (las unidades son
  fungibles). Vuelve a ``medido`` cuando el stock llega a ≤ 0 o con un ancla.
- **Stock previo conocido ≤ 0:** la compra reinicia el promedio. Es una
  convención documentada, no una medición: el motivo lo dice.
- **Stock no reconstruible** (un ``return`` o un ajuste sin procedencia, o el
  llamador avisa que hay stock fuera del ledger): ``sin_medicion`` y promedio
  ``None``. Una compra sola NO lo recupera: no se sabe cuántas unidades quedan.
  Sólo lo recupera un ``ancla``: un conteo con stock ABSOLUTO. Hoy el ledger no
  guarda ninguno (los movimientos de catálogo son deltas); el tipo existe para
  cuando lo haya.
- **Apertura:** el stock que el negocio ya tenía, con su costo, EN SU FECHA de
  negocio (no al comienzo de todo, como el chequeo temporal de integridad). Es
  un delta, igual que en el ledger.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

Clase = Literal["apertura", "compra", "entrada", "salida", "complejo", "ancla"]
Estado = Literal["medido", "incompleto", "sin_medicion"]

#: Dentro del mismo día los créditos van antes que los débitos: una compra del
#: día cubre la venta del día. ``complejo`` va antes que las salidas para que la
#: pérdida de medición alcance a las ventas de ese día.
PRIORIDAD: Final[dict[str, int]] = {
    "ancla": 0,
    "apertura": 1,
    "compra": 2,
    "entrada": 3,
    "complejo": 4,
    "salida": 5,
}

#: Clases que dejan un registro en el historial materializado.
CLASES_CON_REGISTRO: Final = frozenset({"ancla", "apertura", "compra"})

CERO: Final = Decimal("0")

MOTIVO_SIN_COMPRAS: Final = "todavía no hay compras con costo"
MOTIVO_REINICIO: Final = "reinicio: el stock previo estaba agotado"
MOTIVO_SIN_COSTO: Final = "hay unidades sin costo en el stock"
MOTIVO_NO_RECONSTRUIBLE: Final = (
    "stock no reconstruible (devolución o ajuste sin procedencia): hace falta un conteo"
)
MOTIVO_FUERA_DEL_LEDGER: Final = "hay stock cargado por fuera del registro de movimientos"

# Valores de ``source_type`` (los mismos literales que
# ``application/services/inventory_movement_origin``; un test los ata).
_CATALOGO: Final = "catalog_initial_stock"
_AJUSTES_CON_PROCEDENCIA: Final = frozenset({"reconciliation", "manual_adjustment"})
#: Prefijos de ``source_event_id`` de un movimiento ``sale`` que descuenta una
#: venta registrada en ``sales_entries`` (``stock_service.sale_source_event_id``:
#: venta en vivo, POS y el replay histórico; ``manual_batch:`` es el formato
#: anterior a la migración 20260729_0001).
_PREFIJOS_DE_VENTA: Final = ("sale:", "manual_batch:")


@dataclass(frozen=True)
class Evento:
    """Un hecho de la historia del producto, ya clasificado.

    ``unidades`` es la cantidad en unidades base, siempre positiva: la clase dice
    si entra o sale. ``costo`` es por unidad de venta y sólo cuenta en las
    clases que traen mercadería con precio (apertura, compra, ancla).
    """

    fecha: datetime
    clase: Clase
    unidades: int
    costo: Decimal | None
    creado: datetime
    id: UUID
    movement_id: UUID | None = None

    def __post_init__(self) -> None:
        # Las compras llegan rotuladas UTC y las ventas naive: se comparan sin
        # zona (las dos son hora argentina) para que ``sorted`` no mezcle tipos.
        if self.fecha.tzinfo is not None:
            object.__setattr__(self, "fecha", self.fecha.replace(tzinfo=None))
        if self.creado.tzinfo is not None:
            object.__setattr__(self, "creado", self.creado.replace(tzinfo=None))

    def clave(self) -> tuple[date, int, datetime, datetime, str]:
        """Día de negocio → clase → hora → carga → id. La clase va ANTES que la
        hora: dentro del día los créditos se procesan antes que los débitos aunque
        la venta sea de la mañana y la compra de la tarde (muchos imports traen
        sólo el día). ``fecha`` llega en hora argentina: el llamador normaliza
        (las compras vienen rotuladas UTC y las ventas naive)."""
        return (self.fecha.date(), PRIORIDAD[self.clase], self.fecha, self.creado, str(self.id))


@dataclass(frozen=True)
class EstadoCosto:
    """El estado del producto después de un evento."""

    u_conocidas: Decimal
    u_sin_costo: Decimal
    promedio: Decimal | None
    estado: Estado
    motivo: str | None
    #: ``False`` cuando ya no se sabe cuántas unidades hay (``sin_medicion``).
    stock_conocido: bool = True

    @property
    def stock(self) -> Decimal:
        return self.u_conocidas + self.u_sin_costo

    @property
    def fraccion_sin_costo(self) -> Decimal | None:
        if self.stock <= 0:
            return None
        return self.u_sin_costo / self.stock


@dataclass(frozen=True)
class Registro:
    """Lo que se materializa: el estado justo después de una compra o un ancla."""

    evento: Evento
    estado: EstadoCosto


INICIAL: Final = EstadoCosto(CERO, CERO, None, "sin_medicion", MOTIVO_SIN_COMPRAS)


def clasificar(
    movement_type: str,
    source_type: str | None,
    qty: int,
    *,
    source_event_id: str | None = None,
    catalogo_reparado: bool = False,
) -> Clase | None:
    """La clase de un movimiento del ledger; ``None`` = no cuenta.

    - Un ``sale`` que descuenta una venta registrada (``source_event_id`` con
      prefijo ``sale:``, incluido el replay histórico) se ignora: la venta ya
      entra desde ``sales_entries`` y contarla otra vez la duplicaría. Cualquier
      otro ``sale`` (el «saqué N» del chat, UPDATE_STOCK) no tiene venta: es una
      salida.
    - Un ``purchase`` de catálogo cuyo gasto anuló M·R (``catalogo_reparado``) es
      una foto del stock que el negocio ya tenía: apertura, no compra.
    - Un ``return`` o un ajuste sin procedencia no dicen qué pasó con el stock:
      ``complejo``.
    """
    if qty == 0:
        return None
    if movement_type == "sale":
        if source_event_id is not None and source_event_id.startswith(_PREFIJOS_DE_VENTA):
            return None
        return "salida" if qty < 0 else "complejo"
    if movement_type == "purchase":
        if source_type == _CATALOGO and catalogo_reparado and qty > 0:
            return "apertura"
        return "compra" if qty > 0 else "salida"
    if movement_type == "loss":
        return "salida"
    if movement_type == "adjustment":
        if source_type == _CATALOGO:
            return "apertura" if qty > 0 else "salida"
        if source_type in _AJUSTES_CON_PROCEDENCIA:
            return "entrada" if qty > 0 else "salida"
    return "complejo"


def _estado(u_c: Decimal, u_s: Decimal, prom: Decimal | None, motivo: str | None) -> EstadoCosto:
    if prom is None:
        sin_compras = MOTIVO_SIN_COSTO if u_s > 0 else MOTIVO_SIN_COMPRAS
        return EstadoCosto(u_c, u_s, None, "sin_medicion", motivo or sin_compras)
    if u_s > 0:
        return EstadoCosto(u_c, u_s, prom, "incompleto", MOTIVO_SIN_COSTO)
    if motivo == MOTIVO_SIN_COSTO:
        motivo = None
    return EstadoCosto(u_c, u_s, prom, "medido", motivo)


def _todo_sin_costo(qty: Decimal) -> EstadoCosto:
    """Todo el stock es de unidades sin costo: no hay promedio que mostrar (el
    anterior valía para unidades que ya no están)."""
    return EstadoCosto(CERO, qty, None, "sin_medicion", MOTIVO_SIN_COSTO)


def aplicar(estado: EstadoCosto, ev: Evento) -> EstadoCosto:
    """El estado después de ``ev``."""
    qty = Decimal(ev.unidades)
    if ev.clase == "complejo":
        return EstadoCosto(CERO, CERO, None, "sin_medicion", MOTIVO_NO_RECONSTRUIBLE, False)
    if ev.clase == "ancla":
        if ev.costo is None:
            return _todo_sin_costo(qty)
        return _estado(qty, CERO, ev.costo, None)
    if not estado.stock_conocido:
        # Sin un ancla no se sabe cuántas unidades hay: nada lo recupera.
        return estado
    u_c, u_s, prom = estado.u_conocidas, estado.u_sin_costo, estado.promedio

    if ev.clase == "salida":
        stock = u_c + u_s
        if stock > 0 and u_s > 0:
            consumo = min(qty, stock)
            restante = stock - consumo
            # Así la suma queda exacta: con ``u_c -= consumo × u_c / stock`` el
            # redondeo de Decimal deja residuos (1E-28) que nunca llegan a 0.
            u_s = u_s * restante / stock if restante > 0 else CERO
            u_c = restante - u_s - (qty - consumo)
        else:
            u_c -= qty
        if u_c + u_s <= 0:
            # Agotado: lo que falta es stock negativo de unidades fungibles; ya no
            # queda nada sin costo que contaminar el promedio.
            u_c, u_s = u_c + u_s, CERO
        motivo = estado.motivo if estado.motivo == MOTIVO_REINICIO else None
        return _estado(u_c, u_s, prom, motivo)

    # apertura, compra o entrada: suman unidades. Un costo 0 no es un costo
    # conocido (igual que ``costo_de_referencia``): promediarlo bajaría el costo.
    con_costo = ev.costo is not None and ev.costo > 0 and ev.clase != "entrada"
    stock = u_c + u_s
    if stock <= 0:
        # El promedio se reinicia con la compra, pero la cantidad NO se perdona:
        # las unidades vendidas de más se cubren con ésta (stock + qty).
        reinicia = prom is not None
        if con_costo:
            return _estado(stock + qty, CERO, ev.costo, MOTIVO_REINICIO if reinicia else None)
        if stock + qty <= 0:
            return _estado(stock + qty, CERO, prom, None)
        return _todo_sin_costo(stock + qty)
    if not con_costo:
        return _estado(u_c, u_s + qty, prom, None)
    assert ev.costo is not None
    if prom is None or u_c <= 0:
        return _estado(u_c + qty, u_s, ev.costo, None)
    return _estado(u_c + qty, u_s, (u_c * prom + qty * ev.costo) / (u_c + qty), None)


def ordenar(eventos: Iterable[Evento]) -> list[Evento]:
    return sorted(eventos, key=Evento.clave)


def proyectar(
    eventos: Iterable[Evento],
    *,
    desde: EstadoCosto = INICIAL,
    stock_fuera_del_ledger: bool = False,
) -> list[tuple[Evento, EstadoCosto]]:
    """Cada evento, en orden, con el estado que deja.

    ``desde`` permite reconstruir a partir de un registro materializado (el
    checkpoint) sin releer toda la historia. ``stock_fuera_del_ledger`` lo
    detecta el llamador (el stock actual no cierra con el ledger): sin un ancla,
    no hay medición.
    """
    estado = desde
    if stock_fuera_del_ledger:
        estado = EstadoCosto(CERO, CERO, None, "sin_medicion", MOTIVO_FUERA_DEL_LEDGER, False)
    salida: list[tuple[Evento, EstadoCosto]] = []
    for ev in ordenar(eventos):
        estado = aplicar(estado, ev)
        salida.append((ev, estado))
    return salida


def registros(proyeccion: Sequence[tuple[Evento, EstadoCosto]]) -> list[Registro]:
    """Lo que se materializa: un registro por cada compra, apertura o ancla."""
    return [Registro(ev, est) for ev, est in proyeccion if ev.clase in CLASES_CON_REGISTRO]


def costo_a(
    proyeccion: Sequence[tuple[Evento, EstadoCosto]],
    corte: date,
    *,
    desde: EstadoCosto = INICIAL,
) -> EstadoCosto:
    """El estado del producto al cierre del día ``corte``.

    Es lo que vale para una venta de ese día: dentro del día los créditos se
    procesan antes que los débitos, así que una compra del día ya cuenta.
    """
    estado = desde
    for ev, est in proyeccion:
        if ev.fecha.date() > corte:
            break
        estado = est
    return estado
