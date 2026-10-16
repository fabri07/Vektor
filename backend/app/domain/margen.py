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

Al final, M·P4-2: ``comparar_promedio_y_reposicion``, el margen con costo
promedio al lado del de reposición (un dato, no un segundo margen).
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
    #: De qué líneas sale el ratio: sólo las confirmadas, o confirmadas +
    #: estimadas. No se deduce del estado: un RI confirmado por cobertura queda
    #: rotulado «estimado» por el supuesto del IVA, pero su ratio sigue saliendo
    #: de las confirmadas.
    base_del_ratio: Literal["confirmada", "total"] | None = None

    @property
    def cobertura_del_estado(self) -> Decimal:
        """La cobertura de las líneas de las que sale el número."""
        if self.base_del_ratio == "confirmada":
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
    base: Literal["confirmada", "total"]
    if cob_conf >= COBERTURA_MINIMA:
        estado, base, motivo = "confirmado", "confirmada", None
        ingreso_base, costo_base = ingreso_conf, costo_conf
    elif cob_total >= COBERTURA_MINIMA:
        estado, base = "estimado", "total"
        ingreso_base, costo_base = ingreso_cub, costo_cub
        motivo = (
            f"cobertura confirmada {_pct_txt(cob_conf)}%: incluye costos estimados"
        )
    else:
        return MedicionDeMargen(
            "ausente", motivo_cobertura(cob_total), ingreso_total, ingreso_conf,
            costo_conf, ingreso_cub, costo_cub, cob_conf, cob_total, None, None, None,
            supuestos,
        )

    if ri:
        # El supuesto del IVA de venta siempre se dice, también si ya era
        # estimado por cobertura.
        estado = "estimado"
        motivo = f"{motivo}; {SUPUESTO_ALICUOTA_RI}" if motivo else SUPUESTO_ALICUOTA_RI

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
        base_del_ratio=base,
    )


# ─────────────────────────────────────────────────────────────────────────────
# M·P4-2 — margen con costo PROMEDIO vs. margen con costo de REPOSICIÓN
# ─────────────────────────────────────────────────────────────────────────────
#
# Es un DATO al lado del margen principal, nunca un segundo margen: el margen del
# negocio sigue siendo el de ``medir_margen`` (reposición). La brecha entre los
# dos es el efecto inflación sobre el stock (ganancia por tenencia): lo que el
# promedio arrastra de compras viejas y el negocio vuelve a pagar al reponer.
#
# Las dos cifras salen de la MISMA intersección (venta con costo de reposición y
# con costo promedio ``medido``) y el mismo denominador; si no, la brecha mezcla
# universos. ``incompleto`` y ``sin_medicion`` quedan fuera.

ESTADO_PROMEDIO_MEDIDO: Final = "medido"

#: RI (decisión del dueño, 2026-10-15): el promedio promedia el costo CRUDO
#: (``costo_promedio_repository``) y la reposición de la venta pasa por
#: ``base_comparable`` (neto). Compararlos daría una brecha falsa del tamaño del
#: IVA. Pasar el promedio a neto es M·P4b.
MOTIVO_PROMEDIO_SIN_NETO_IVA: Final = "costo promedio todavía sin neto de IVA"

# Fuera del RI la comparación supone que ``cost_ars`` y el promedio están en la
# MISMA base cruda. Vale hoy porque ningún escritor de compras emite un impuesto
# ``adicional`` (``contrato_de_costo`` sólo escribe ``incluido``/``desconocido``):
# con uno, ``base_comparable`` le sumaría el IVA a la reposición de un
# monotributista y no al promedio, y la brecha mostraría ~21 puntos de
# «inflación» falsa. Si M·1d habilita ``adicional``, esto entra en M·P4b.


@dataclass(frozen=True)
class LineaDeComparacion:
    """Una venta del universo con sus dos costos (``None`` = no tiene)."""

    importe: Decimal
    costo_reposicion: Decimal | None
    costo_promedio: Decimal | None
    estado_promedio: str | None


@dataclass(frozen=True)
class ComparacionDeMargenes:
    #: ``None`` si el dato está presente; si no, por qué falta.
    motivo_ausente: str | None
    ingreso_total: Decimal
    ingreso_comun: Decimal
    #: Fracción 0..1 del ingreso total que cae en la intersección.
    cobertura_conjunta: Decimal
    margen_reposicion_pct: Decimal | None = None
    margen_promedio_pct: Decimal | None = None
    #: margen promedio − margen reposición. Positiva con precios que suben.
    brecha_pp: Decimal | None = None
    #: costo reposición − costo promedio de la intersección (en pesos).
    brecha_ars: Decimal | None = None

    @property
    def ausente(self) -> bool:
        return self.motivo_ausente is not None


def comparar_promedio_y_reposicion(
    lineas: Iterable[LineaDeComparacion], fiscal_condition_crudo: str | None
) -> ComparacionDeMargenes:
    """Margen con costo promedio y con costo de reposición sobre la intersección."""
    ingreso_total = ingreso_comun = costo_rep = costo_prom = Decimal(0)
    for linea in lineas:
        ingreso_total += linea.importe
        if (
            linea.costo_reposicion is None
            or linea.costo_promedio is None
            or linea.estado_promedio != ESTADO_PROMEDIO_MEDIDO
        ):
            continue
        ingreso_comun += linea.importe
        costo_rep += linea.costo_reposicion
        costo_prom += linea.costo_promedio

    cero = Decimal(0)
    if ingreso_total <= 0:
        return ComparacionDeMargenes(MOTIVO_SIN_INGRESO, ingreso_total, ingreso_comun, cero)
    cobertura = ingreso_comun / ingreso_total
    if es_responsable_inscripto(fiscal_condition_crudo):
        return ComparacionDeMargenes(
            MOTIVO_PROMEDIO_SIN_NETO_IVA, ingreso_total, ingreso_comun, cobertura
        )
    if cobertura < COBERTURA_MINIMA:
        return ComparacionDeMargenes(
            motivo_cobertura(cobertura), ingreso_total, ingreso_comun, cobertura
        )
    margen_rep = (1 - costo_rep / ingreso_comun) * 100
    margen_prom = (1 - costo_prom / ingreso_comun) * 100
    return ComparacionDeMargenes(
        motivo_ausente=None,
        ingreso_total=ingreso_total,
        ingreso_comun=ingreso_comun,
        cobertura_conjunta=cobertura,
        margen_reposicion_pct=margen_rep,
        margen_promedio_pct=margen_prom,
        brecha_pp=margen_prom - margen_rep,
        brecha_ars=costo_rep - costo_prom,
    )
