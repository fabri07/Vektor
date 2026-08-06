"""F-H3.b — qué le PASARÍA al stock si se aplicara la historia de un archivo.

Calcula, no aplica. Es la misma cuenta que va a hacer el replay cuando el
usuario lo elija (F-H3.d) y la que alimenta el preview (F-H3.c): tenerla en un
solo lugar es lo que evita que lo que se muestra y lo que se aplica se
desincronicen.

Dos decisiones que no son obvias:

**El saldo de apertura es el stock PREVIO al archivo, no el actual.** Si se
usara el stock de hoy, las compras que el import ya aplicó estarían contadas dos
veces: una en el saldo y otra como evento. Por eso el caller captura el saldo la
PRIMERA vez que toca un producto, antes de mutarlo.

**Un catálogo declara un absoluto, no un movimiento.** ``current_snapshot`` es
una foto: pisa el saldo de apertura en vez de sumarse a él. Tratarlo como un
evento más convertiría "tengo 10 en góndola" en "entraron 10", que sobre un
producto que ya existía es inventar una compra.

El orden intra-día y el desempate crédito-antes-que-débito los pone
``replay_timeline``, que ya existe y es la única implementación del replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from app.application.services.inventory_temporal_service import (
    TimelineEvent,
    replay_timeline,
)

#: Mismos rangos que `inventory_temporal_service` — NO se re-inventan acá.
RANK_PURCHASE = 0
RANK_ADJUSTMENT = 1
RANK_LOSS = 2
RANK_SALE = 3


@dataclass
class ProductProjection:
    """Los movimientos que un archivo declara sobre UN producto."""

    product_id: UUID
    product_name: str
    #: Stock del producto ANTES de que este archivo lo tocara.
    saldo_previo: int = 0
    #: Absoluto declarado por una hoja de catálogo (`current_snapshot`). `None` =
    #: el archivo no declara saldo, así que se arranca del previo.
    saldo_declarado: int | None = None
    eventos: list[TimelineEvent] = field(default_factory=list)
    #: Unidades compradas / vendidas que el archivo declara (para el resumen).
    compradas: int = 0
    vendidas: int = 0

    @property
    def apertura(self) -> int:
        """Desde dónde arranca el replay: lo declarado gana sobre lo previo."""
        return self.saldo_previo if self.saldo_declarado is None else self.saldo_declarado

    def agregar_compra(self, day: date, qty: int) -> None:
        if qty <= 0:
            return
        self.compradas += qty
        self.eventos.append(TimelineEvent(day, qty, kind_rank=RANK_PURCHASE))

    def agregar_venta(self, day: date, qty: int) -> None:
        if qty <= 0:
            return
        self.vendidas += qty
        self.eventos.append(TimelineEvent(day, -qty, kind_rank=RANK_SALE))


@dataclass(frozen=True)
class ProductImpact:
    """El resultado de reproducir lo que el archivo declara sobre un producto."""

    product_id: str
    product_name: str
    saldo_inicial: int
    saldo_final: int
    compradas: int
    vendidas: int
    minimo: int
    minimo_en: date | None
    primer_negativo_en: date | None

    @property
    def queda_negativo(self) -> bool:
        return self.saldo_final < 0

    @property
    def toca_negativo(self) -> bool:
        """Pasó por debajo de cero en algún momento, aunque termine en positivo.

        No es lo mismo que terminar negativo y no se colapsan: un saldo final
        sano con un pozo en el medio significa que faltan compras viejas, no que
        el inventario de hoy esté mal.
        """
        return self.primer_negativo_en is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "saldo_inicial": self.saldo_inicial,
            "saldo_final": self.saldo_final,
            "compradas": self.compradas,
            "vendidas": self.vendidas,
            "minimo": self.minimo,
            "minimo_en": self.minimo_en.isoformat() if self.minimo_en else None,
            "primer_negativo_en": (
                self.primer_negativo_en.isoformat() if self.primer_negativo_en else None
            ),
        }


@dataclass(frozen=True)
class ImportImpact:
    """El impacto del archivo entero. Lo que el preview muestra y el resumen avisa."""

    productos: list[ProductImpact]

    @property
    def negativos(self) -> list[ProductImpact]:
        """Productos cuyo saldo se va abajo de cero en algún punto de la historia."""
        return [p for p in self.productos if p.toca_negativo]

    @property
    def unidades_vendidas(self) -> int:
        return sum(p.vendidas for p in self.productos)

    @property
    def unidades_compradas(self) -> int:
        return sum(p.compradas for p in self.productos)


def project_import_impact(proyecciones: dict[UUID, ProductProjection]) -> ImportImpact:
    """Reproduce cada producto por fecha y devuelve el impacto, sin tocar nada.

    Los productos sin ningún evento quedan afuera: el archivo los nombró pero no
    declara movimiento sobre ellos, y listarlos con "saldo final = saldo actual"
    llenaría el preview de filas que no dicen nada.
    """
    impactos: list[ProductImpact] = []
    for proyeccion in proyecciones.values():
        if not proyeccion.eventos:
            continue
        resultado = replay_timeline(
            opening_anchor_qty=proyeccion.apertura, events=proyeccion.eventos
        )
        impactos.append(
            ProductImpact(
                product_id=str(proyeccion.product_id),
                product_name=proyeccion.product_name,
                saldo_inicial=proyeccion.apertura,
                saldo_final=resultado.ending_balance,
                compradas=proyeccion.compradas,
                vendidas=proyeccion.vendidas,
                minimo=resultado.min_balance,
                minimo_en=resultado.min_balance_at,
                primer_negativo_en=resultado.first_negative_at,
            )
        )
    # Orden estable y con el problema arriba: primero lo que se va a negativo, y
    # dentro de cada grupo por nombre. Un orden por dict de Python haría que el
    # mismo archivo muestre el preview distinto entre corridas.
    impactos.sort(key=lambda p: (not p.toca_negativo, p.product_name.lower()))
    return ImportImpact(productos=impactos)
