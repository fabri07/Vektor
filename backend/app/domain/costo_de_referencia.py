"""Costo de referencia de un producto a una fecha (M·1a, fase 2).

Puro: sin sesión, sin ORM y sin tenant (el repositorio ya filtró tenant,
producto, anulados y tipo de movimiento). Responde «¿cuál es el costo de
referencia de este producto a la fecha F, y de dónde sale?» sin inventar nada.

Reglas (``docs/plans/m1-contrato-costo-compra.md``):

- **Última compra** = la última compra VIVA por fecha de NEGOCIO
  (``COALESCE(occurred_at, created_at)``, invariante 2d), no la última fila
  procesada. Una compra con fecha posterior al corte no es elegible, aunque se
  haya cargado antes. Desempate estable: fecha de negocio → ``created_at`` →
  ``id``, todo descendente.
- Una compra sin costo (NULL o 0) no es fuente: se sigue con la anterior.
- **Precedencia:** compra (todo ``purchase`` vivo, incluida la mercadería que un
  catálogo declaró como compra) → stock inicial (el saldo de APERTURA valorizado:
  ``adjustment`` + ``catalog_initial_stock``) → el costo cargado en el producto,
  que no tiene fecha ni comprobante y por eso es sólo una estimación → sin costo.
  Siempre se devuelve el ORIGEN.
- **Fecha:** se compara ``fecha_negocio.date()`` del valor en UTC. La ingesta
  guarda ``occurred_at`` como hora AR rotulada UTC, pero ``stock_service`` (compra
  manual, Otros, chat) guarda ``datetime.now(UTC)`` real, y el fallback a
  ``created_at`` también es UTC real: una compra de esas cargada entre las 21 y
  las 24 AR cae en el día siguiente. Unificar a los escritores es M·1b, antes de
  que M·1c use esto para fijar ``unit_cost_ars``.
- El costo de una candidata es ``unit_cost_final`` si existe; si no, el
  ``unit_cost`` facturado de un movimiento anterior al contrato, que queda con
  envío e impuesto ``desconocido``.

M·1a no cambia ningún número que se muestre: esto todavía no lo consume nadie.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

CONTRATO_VERSION: Final = 1

Tratamiento = Literal["incluido", "adicional", "no_aplica", "desconocido"]
TRATAMIENTOS: Final[frozenset[str]] = frozenset(
    {"incluido", "adicional", "no_aplica", "desconocido"}
)

Origen = Literal["compra", "stock_inicial", "costo_cargado", "sin_costo"]

MOTIVO_SIN_COMPRA_NI_COSTO: Final = "sin compra ni costo cargado"
MOTIVO_COSTO_CARGADO_CERO: Final = "costo cargado en 0: queda pendiente, no es un costo conocido"


@dataclass(frozen=True)
class CompraCandidata:
    """Un movimiento ``purchase`` vivo del producto, con su contrato."""

    movement_id: UUID
    fecha_negocio: datetime
    cargada_en: datetime
    costo_facturado: Decimal | None
    costo_final: Decimal | None
    es_stock_inicial: bool
    shipping_treatment: str | None = None
    tax_treatment: str | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    tax_recoverable: bool | None = None
    document_ref: str | None = None
    contract_version: int | None = None

    @property
    def costo(self) -> Decimal | None:
        return self.costo_final if self.costo_final is not None else self.costo_facturado


@dataclass(frozen=True)
class CostoDeReferencia:
    origen: Origen
    costo_unitario: Decimal | None
    fecha: datetime | None = None
    movement_id: UUID | None = None
    envio: Tratamiento = "desconocido"
    impuesto: Tratamiento = "desconocido"
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    tax_recoverable: bool | None = None
    document_ref: str | None = None
    # True sólo si el movimiento se escribió con el contrato (versión ≥ 1).
    # Un movimiento viejo NO se considera confirmado aunque tenga costo.
    contrato_confirmado: bool = False
    motivo: str | None = None
    #: Cuándo se REGISTRÓ la compra elegida (``created_at``, UTC real). M·1c lo
    #: usa para desempatar dentro del mismo día de negocio contra una edición.
    cargada_en: datetime | None = None

    @property
    def es_estimacion(self) -> bool:
        """El costo cargado no tiene fecha ni comprobante: nunca es «realizado»."""
        return self.origen == "costo_cargado"


def _tratamiento(valor: str | None) -> Tratamiento:
    if valor in TRATAMIENTOS:
        return valor  # type: ignore[return-value]
    return "desconocido"


def _orden(c: CompraCandidata) -> tuple[datetime, datetime, str]:
    return (c.fecha_negocio, c.cargada_en, str(c.movement_id))


def elegir_ultima_compra(
    candidatas: Iterable[CompraCandidata], corte: date
) -> CompraCandidata | None:
    """La última candidata con costo > 0 y fecha de negocio ≤ ``corte``."""
    elegibles = [
        c
        for c in candidatas
        if c.fecha_negocio.date() <= corte and c.costo is not None and c.costo > 0
    ]
    if not elegibles:
        return None
    return max(elegibles, key=_orden)


def _desde_compra(c: CompraCandidata, origen: Origen) -> CostoDeReferencia:
    return CostoDeReferencia(
        origen=origen,
        costo_unitario=c.costo,
        fecha=c.fecha_negocio,
        movement_id=c.movement_id,
        envio=_tratamiento(c.shipping_treatment),
        impuesto=_tratamiento(c.tax_treatment),
        tax_rate=c.tax_rate,
        tax_amount=c.tax_amount,
        tax_recoverable=c.tax_recoverable,
        document_ref=c.document_ref,
        contrato_confirmado=c.contract_version is not None and c.contract_version >= 1,
        cargada_en=c.cargada_en,
    )


def costo_de_referencia(
    candidatas: Iterable[CompraCandidata],
    corte: date,
    costo_cargado: Decimal | None,
) -> CostoDeReferencia:
    """Costo de referencia a ``corte`` con su origen (ver precedencia arriba)."""
    todas = list(candidatas)
    compra = elegir_ultima_compra((c for c in todas if not c.es_stock_inicial), corte)
    if compra is not None:
        return _desde_compra(compra, "compra")
    inicial = elegir_ultima_compra((c for c in todas if c.es_stock_inicial), corte)
    if inicial is not None:
        return _desde_compra(inicial, "stock_inicial")
    if costo_cargado is not None and costo_cargado > 0:
        return CostoDeReferencia(origen="costo_cargado", costo_unitario=costo_cargado)
    motivo = MOTIVO_COSTO_CARGADO_CERO if costo_cargado == 0 else MOTIVO_SIN_COMPRA_NI_COSTO
    return CostoDeReferencia(origen="sin_costo", costo_unitario=None, motivo=motivo)
