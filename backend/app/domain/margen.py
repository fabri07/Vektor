"""Margen bruto de ventas: LA definición única (M·2b).

Puro. La comparten FactsService, el chat y el subscore de salud: nadie más
calcula un margen del negocio (``docs/plans/m2-margen-por-venta.md``).

Entra una línea por venta con su importe y el snapshot de costo que congeló
M·2a/M·2c (``sales_entries.cost_ars`` / ``cost_confirmed``). El universo lo
arma el llamador: sin anuladas, sin demo (salvo tenant demo), sin cobros de
fiado. Las ventas SIN producto entran igual: su importe es ingreso sin costo y
baja la cobertura —es la cobertura sobre todo lo vendido, no sólo sobre lo
vinculado—.

Reglas (decisiones del dueño, 2026-10-11 y 2026-10-12):

- ``ganancia = ingreso − costo``; ``margen % = ganancia / ingreso × 100``,
  ponderado por importe (nunca promedio simple de márgenes). El negativo vale.
- **Cobertura por importe:** ingreso con costo / ingreso total.
- **Estado:** con ≥ 50% del ingreso con costo CONFIRMADO, ``confirmado`` (el
  ratio sale de las líneas confirmadas). Si no, con ≥ 50% contando las
  estimaciones, ``estimado`` (ratio de confirmadas + estimadas). Si ni así,
  ``ausente``: no hay número, nunca 0 ni un neutral.
- **Ratio del cubierto:** el costo de lo vendido sin costo se proyecta con el
  ratio costo/ingreso de lo cubierto (``costo_vendido``). Contar esas ventas
  como costo 0 inflaba el margen (el 96,2% de ASTERIA).
- **Responsable Inscripto:** costo y precio se comparan SIN IVA. El costo ya
  llega neto (``base_comparable``, M·1d); el importe de la venta incluye IVA y
  la alícuota de la venta no se guarda, así que se supone el 21%. Un supuesto
  nunca confirma: el estado queda como mucho en ``estimado`` (si la cobertura
  confirmada alcanzaba, el ratio sigue saliendo de las confirmadas: lo que
  baja es el rótulo, no el dato de costo).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from app.domain.fiscal_condition import (
    RESPONSABLE_INSCRIPTO,
    es_registrado_legacy,
    normalize_fiscal_condition,
)

EstadoDeMargen = Literal["confirmado", "estimado", "ausente"]

#: Cobertura mínima (fracción del ingreso) para mostrar un margen. Aprobado por
#: el dueño el 2026-10-09; es cobertura de DATOS, no un piso del margen.
COBERTURA_MINIMA: Final = Decimal("0.5")

#: Alícuota supuesta del IVA de venta de un RI mientras no se guarde la real.
ALICUOTA_VENTA_SUPUESTA: Final = Decimal("0.21")

SUPUESTO_ALICUOTA_RI: Final = "alícuota de venta supuesta 21% (no se guarda el IVA de la venta)"
MOTIVO_SIN_INGRESO: Final = "sin ventas en el período"


def motivo_cobertura(cobertura: Decimal) -> str:
    """«cobertura de costo X%» (el motivo de un margen ausente)."""
    return f"cobertura de costo {_pct_txt(cobertura)}%"


def _pct_txt(fraccion: Decimal) -> str:
    return f"{float(fraccion * 100):.1f}".replace(".", ",")


@dataclass(frozen=True)
class LineaDeMargen:
    """Una venta del universo.

    ``costo`` es ``None`` si la venta no tiene costo (sin producto, sin costear,
    ``sin_costo``). ``confirmado`` sólo cuenta con costo.
    """

    importe: Decimal
    costo: Decimal | None
    confirmado: bool = False


@dataclass(frozen=True)
class MedicionDeMargen:
    estado: EstadoDeMargen
    #: Motivo de que no sea confirmado (``None`` si lo es).
    motivo: str | None
    #: Ingresos comparables (netos de IVA supuesto para un RI).
    ingreso_total: Decimal
    ingreso_confirmado: Decimal
    costo_confirmado: Decimal
    #: Confirmadas + estimadas.
    ingreso_cubierto: Decimal
    costo_cubierto: Decimal
    #: Fracciones 0..1 del ingreso total.
    cobertura_confirmada: Decimal
    cobertura_total: Decimal
    #: Del estado (``None`` si ausente).
    margen_pct: Decimal | None
    #: Costo de TODO lo vendido con el ratio del cubierto (``None`` si ausente).
    costo_vendido: Decimal | None
    ganancia: Decimal | None
    supuestos: tuple[str, ...] = ()

    @property
    def cobertura_del_estado(self) -> Decimal:
        """La cobertura que sostiene el número: confirmada si es confirmado."""
        if self.estado == "confirmado":
            return self.cobertura_confirmada
        return self.cobertura_total


def es_responsable_inscripto(fiscal_condition_crudo: str | None) -> bool:
    """RI declarado. El legacy ``registered`` no alcanza (podría no serlo)."""
    if es_registrado_legacy(fiscal_condition_crudo):
        return False
    return normalize_fiscal_condition(fiscal_condition_crudo) == RESPONSABLE_INSCRIPTO


def medir_margen(
    lineas: Iterable[LineaDeMargen], fiscal_condition_crudo: str | None
) -> MedicionDeMargen:
    """El margen bruto de un universo de ventas, con su cobertura y su estado."""
    ri = es_responsable_inscripto(fiscal_condition_crudo)
    divisor = 1 + ALICUOTA_VENTA_SUPUESTA if ri else Decimal(1)
    supuestos = (SUPUESTO_ALICUOTA_RI,) if ri else ()

    ingreso_total = ingreso_conf = costo_conf = ingreso_cub = costo_cub = Decimal(0)
    for linea in lineas:
        ingreso = linea.importe / divisor
        ingreso_total += ingreso
        if linea.costo is None:
            continue
        ingreso_cub += ingreso
        costo_cub += linea.costo
        if linea.confirmado:
            ingreso_conf += ingreso
            costo_conf += linea.costo

    if ingreso_total <= 0:
        cero = Decimal(0)
        return MedicionDeMargen(
            "ausente", MOTIVO_SIN_INGRESO, ingreso_total, ingreso_conf, costo_conf,
            ingreso_cub, costo_cub, cero, cero, None, None, None, supuestos,
        )

    cob_conf = ingreso_conf / ingreso_total
    cob_total = ingreso_cub / ingreso_total

    estado: EstadoDeMargen
    if cob_conf >= COBERTURA_MINIMA:
        estado, ingreso_base, costo_base = "confirmado", ingreso_conf, costo_conf
        motivo = None
    elif cob_total >= COBERTURA_MINIMA:
        estado, ingreso_base, costo_base = "estimado", ingreso_cub, costo_cub
        motivo = (
            f"cobertura confirmada {_pct_txt(cob_conf)}%: incluye costos estimados"
        )
    else:
        return MedicionDeMargen(
            "ausente", motivo_cobertura(cob_total), ingreso_total, ingreso_conf,
            costo_conf, ingreso_cub, costo_cub, cob_conf, cob_total, None, None, None,
            supuestos,
        )

    if ri and estado == "confirmado":
        estado, motivo = "estimado", SUPUESTO_ALICUOTA_RI

    ratio = costo_base / ingreso_base
    costo_vendido = ingreso_total * ratio
    return MedicionDeMargen(
        estado=estado,
        motivo=motivo,
        ingreso_total=ingreso_total,
        ingreso_confirmado=ingreso_conf,
        costo_confirmado=costo_conf,
        ingreso_cubierto=ingreso_cub,
        costo_cubierto=costo_cub,
        cobertura_confirmada=cob_conf,
        cobertura_total=cob_total,
        margen_pct=(1 - ratio) * 100,
        costo_vendido=costo_vendido,
        ganancia=ingreso_total - costo_vendido,
        supuestos=supuestos,
    )
