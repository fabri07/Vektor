"""Base comparable del costo según el régimen fiscal (M·1d).

Puro. Responde «¿contra qué costo por unidad se puede comparar el precio de
venta, y es un número confirmado?». Lo consume el margen (M·2); acá sólo vive
el contrato.

Reglas (``docs/plans/cierre-ingesta-capital-margen.md``, M·1, y mini-plan M·1d):

- **El régimen guía**, la operación confirma. La tasa y si el IVA es
  recuperable se confirman por COMPRA (contrato de M·1a/M·1b); el régimen sólo
  decide qué hacer con lo confirmado. Nunca 21% por defecto.
- **Responsable Inscripto con IVA recuperable confirmado:** se compara NETO
  contra neto. Un IVA ``incluido`` se extrae UNA vez, por importe confirmado o,
  si no, por base y tasa confirmadas.
- **IVA no recuperable confirmado** (o régimen que no lo recupera: monotributo,
  informal): integra el costo. Si ya está ``incluido`` no se suma de nuevo; si es
  ``adicional`` se suma por importe o por tasa confirmados.
- ``no_aplica`` ≠ ``adicional``: no hay impuesto que tratar.
- **Envío:** la base es comparable sólo si el costo lo incluye (``incluido``) o
  no hubo (``no_aplica``). Con ``adicional`` el costo no trae el envío (el rótulo
  ``sin_flete`` de M·1c) y con ``desconocido`` no se sabe.
- **Sin base comparable** se devuelve el costo igual, como ESTIMACIÓN, con su
  motivo, y queda afuera del margen confirmado.
- El alias legacy ``registered`` (monotributo y RI juntos) NO habilita un
  cálculo fiscal confirmado: no dice si el IVA se recupera.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.fiscal_condition import (
    RESPONSABLE_INSCRIPTO,
    es_registrado_legacy,
    normalize_fiscal_condition,
)

CENTAVO: Final = Decimal("0.01")

RegimenParaBase = Literal["ri", "no_recupera"]

MOTIVO_SIN_COSTO: Final = "sin costo de compra"
MOTIVO_ESTIMACION: Final = (
    "el costo no sale de una compra (stock inicial, catálogo o carga manual): es una estimación"
)
MOTIVO_SIN_REGIMEN: Final = "régimen fiscal no configurado"
MOTIVO_REGIMEN_LEGACY: Final = (
    "régimen «registrado» sin distinguir monotributo de responsable inscripto"
)
MOTIVO_ENVIO_ADICIONAL: Final = "el costo no incluye el envío"
MOTIVO_ENVIO_DESCONOCIDO: Final = "no se sabe si el costo incluye el envío"
MOTIVO_IMPUESTO_DESCONOCIDO: Final = "no se sabe si el costo incluye impuestos"
MOTIVO_RECUPERABLE_SIN_CONFIRMAR: Final = "no se confirmó si el IVA de la compra es recuperable"
MOTIVO_SIN_IMPORTE_NI_TASA: Final = (
    "el IVA de la compra no tiene importe confirmado (ni tasa aplicable sin envío)"
)
MOTIVO_IVA_MAYOR_QUE_COSTO: Final = "el IVA confirmado no es menor que el costo"
MOTIVO_RECUPERABLE_SIN_RI: Final = (
    "la compra dice IVA recuperable pero el régimen no lo recupera"
)


@dataclass(frozen=True)
class BaseComparable:
    #: Costo por unidad contra el que se compara el precio. Con ``confirmada``
    #: en False es una ESTIMACIÓN (o ``None`` si no hay costo).
    costo: Decimal | None
    confirmada: bool
    motivo: str | None = None


def regimen_para_base(fiscal_condition_crudo: str | None) -> RegimenParaBase | str:
    """Qué hace el régimen con el IVA, o el MOTIVO por el que no se sabe.

    Recibe el valor CRUDO de ``business_profiles.fiscal_condition``: el legacy
    ``registered`` se normaliza a monotributo en el resto de la app, pero acá no
    alcanza (podría ser un RI) y se detecta antes de normalizar.
    """
    if es_registrado_legacy(fiscal_condition_crudo):
        return MOTIVO_REGIMEN_LEGACY
    canonico = normalize_fiscal_condition(fiscal_condition_crudo)
    if canonico is None:
        return MOTIVO_SIN_REGIMEN
    return "ri" if canonico == RESPONSABLE_INSCRIPTO else "no_recupera"


def _impuesto_unitario(costo: Decimal, ref: CostoDeReferencia, *, incluido: bool) -> Decimal | None:
    """El IVA por unidad: por importe confirmado o, si no, por base y tasa.

    Por TASA sólo si no hubo envío (``no_aplica``): con el envío dentro del
    costo, aplicar la alícuota a todo gravaría también el flete, que puede no
    llevar IVA o llevar otra. Ahí hace falta el importe.
    """
    if ref.tax_amount is not None:
        return ref.tax_amount
    if ref.tax_rate is None or ref.envio != "no_aplica":
        return None
    if incluido:
        # costo = base × (1 + tasa) → impuesto = costo − costo / (1 + tasa).
        return costo - (costo / (1 + ref.tax_rate)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    return (costo * ref.tax_rate).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def base_comparable(
    referencia: CostoDeReferencia, fiscal_condition_crudo: str | None
) -> BaseComparable:
    """La base comparable de un costo de referencia (M·1a) para el régimen dado."""
    costo = referencia.costo_unitario
    if costo is None:
        return BaseComparable(None, False, referencia.motivo or MOTIVO_SIN_COSTO)

    def estimacion(motivo: str) -> BaseComparable:
        return BaseComparable(costo, False, motivo)

    # Sólo una COMPRA confirma. El stock inicial valoriza la apertura con el
    # costo que declaró el catálogo (decisión 3: estimación, no compra).
    if referencia.origen != "compra":
        return estimacion(MOTIVO_ESTIMACION)
    regimen = regimen_para_base(fiscal_condition_crudo)
    if regimen not in ("ri", "no_recupera"):
        return estimacion(regimen)
    if referencia.envio == "adicional":
        return estimacion(MOTIVO_ENVIO_ADICIONAL)
    if referencia.envio not in ("incluido", "no_aplica"):
        return estimacion(MOTIVO_ENVIO_DESCONOCIDO)

    impuesto = referencia.impuesto
    if impuesto == "no_aplica":
        return BaseComparable(costo, True)
    if impuesto not in ("incluido", "adicional"):
        return estimacion(MOTIVO_IMPUESTO_DESCONOCIDO)
    incluido = impuesto == "incluido"

    if regimen == "ri":
        if referencia.tax_recoverable is None:
            return estimacion(MOTIVO_RECUPERABLE_SIN_CONFIRMAR)
        recupera = referencia.tax_recoverable
    else:
        if referencia.tax_recoverable is True:
            return estimacion(MOTIVO_RECUPERABLE_SIN_RI)
        recupera = False

    if recupera:
        # Neto contra neto: el IVA incluido se extrae UNA vez; uno adicional ya
        # está afuera del costo.
        if not incluido:
            return BaseComparable(costo, True)
        iva = _impuesto_unitario(costo, referencia, incluido=True)
        if iva is None:
            return estimacion(MOTIVO_SIN_IMPORTE_NI_TASA)
        if iva >= costo:
            return estimacion(MOTIVO_IVA_MAYOR_QUE_COSTO)
        return BaseComparable((costo - iva).quantize(CENTAVO, rounding=ROUND_HALF_UP), True)

    # No recuperable: integra el costo. Incluido → ya está; adicional → se suma.
    if incluido:
        return BaseComparable(costo, True)
    iva = _impuesto_unitario(costo, referencia, incluido=False)
    if iva is None:
        return estimacion(MOTIVO_SIN_IMPORTE_NI_TASA)
    return BaseComparable((costo + iva).quantize(CENTAVO, rounding=ROUND_HALF_UP), True)
