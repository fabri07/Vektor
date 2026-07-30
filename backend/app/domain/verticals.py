"""Verticales (rubros) de negocio soportados por Véktor — fuente única de verdad.

Hoy conviven dos códigos históricos para el mismo rubro: `business_profiles.
vertical_code` guarda el código corto `"kiosco"` mientras que los archivos de
datos (heurísticas, campos de vertical, categorías de producto) usan el código
largo `"kiosco_almacen"`. Este módulo se queda con el código LARGO como único
valor canónico persistible (`Vertical`) y **no aliasea nada**: `parse_vertical`
falla ruidosamente ante cualquier código que no sea exactamente uno de los tres
valores canónicos, incluido el código corto legado. Una migración de datos
posterior (fuera de este módulo) reescribe los registros viejos.

Sin dependencias de infraestructura (ni SQLAlchemy, ni settings, ni repos).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Vertical(StrEnum):
    """Verticales operativos: los únicos valores válidos para un negocio ya dado
    de alta. Cualquier consumidor que calcule heurísticas, categorías de
    producto o benchmarks de margen recibe uno de estos valores.

    Todos son de REVENTA: el negocio compra un producto y lo vende. Los rubros
    que transforman materia prima —carnicería (desposte de media res), pollería
    (despiece + elaborados), panadería, rotisería— NO entran acá: el motor asume
    `compra → producto → venta` y no tiene concepto de rendimiento ni de receta.
    Un kilo de media res no es un producto: son doce cortes con precios
    distintos."""

    KIOSCO_ALMACEN = "kiosco_almacen"
    DECORACION_HOGAR = "decoracion_hogar"
    LIMPIEZA = "limpieza"
    LIBRERIA_PAPELERIA = "libreria_papeleria"
    INDUMENTARIA = "indumentaria"
    VERDULERIA_FRUTERIA = "verduleria_fruteria"


class RequestedVertical(StrEnum):
    """Verticales que se pueden elegir en el formulario de solicitud de acceso.

    Superconjunto de `Vertical`: agrega `OTROS` para el negocio que no encaja
    en ninguno de los rubros soportados hoy. `OTROS` NUNCA es un `Vertical`
    operativo — no tiene heurísticas ni categorías propias."""

    KIOSCO_ALMACEN = "kiosco_almacen"
    DECORACION_HOGAR = "decoracion_hogar"
    LIMPIEZA = "limpieza"
    LIBRERIA_PAPELERIA = "libreria_papeleria"
    INDUMENTARIA = "indumentaria"
    VERDULERIA_FRUTERIA = "verduleria_fruteria"
    OTROS = "otros"


#: Conjunto cerrado de los valores operativos (los mismos que `Vertical`, como
#: `frozenset[str]` para chequeos de pertenencia sin importar el enum).
OPERATIONAL_VERTICALS: Final[frozenset[str]] = frozenset(v.value for v in Vertical)

#: Regex de validación para los schemas Pydantic que reciben un `vertical_code`
#: crudo. Derivado del enum a propósito: escrito a mano se desincroniza (así fue
#: como `auth.py`/`onboarding.py` siguieron aceptando SOLO el código corto).
VERTICAL_CODE_PATTERN: Final[str] = "^(" + "|".join(v.value for v in Vertical) + ")$"

#: Nombre visible por vertical, para UI y narrativas.
VERTICAL_LABELS: Final[dict[Vertical, str]] = {
    Vertical.KIOSCO_ALMACEN: "Kiosco / Almacén",
    Vertical.DECORACION_HOGAR: "Decoración del hogar",
    Vertical.LIMPIEZA: "Limpieza",
    Vertical.LIBRERIA_PAPELERIA: "Librería y papelería",
    Vertical.INDUMENTARIA: "Indumentaria",
    Vertical.VERDULERIA_FRUTERIA: "Verdulería y frutería",
}


class UnknownVerticalError(ValueError):
    """Un código de vertical no coincide con ninguno de los valores canónicos.

    Se levanta en vez de convertirse en un fallback silencioso — un vertical
    desconocido es un bug de datos o de deploy, no un caso a tapar."""


def parse_vertical(raw: str | None) -> Vertical:
    """Parsea un código de vertical al valor canónico exacto.

    No aliasea nada: ni el código corto legado (`"kiosco"`), ni `"otros"`, ni
    variantes de mayúsculas/minúsculas o con espacios. Solo el valor exacto en
    minúsculas de uno de los tres verticales operativos es válido. Levanta
    `UnknownVerticalError` para cualquier otro caso, incluido `None`.
    """
    if raw is not None:
        for vertical in Vertical:
            if raw == vertical.value:
                return vertical
    validos = ", ".join(v.value for v in Vertical)
    raise UnknownVerticalError(
        f"Vertical desconocido: {raw!r}. Valores válidos: {validos}."
    )


def try_parse_vertical(raw: str | None) -> Vertical | None:
    """Como `parse_vertical`, pero devuelve `None` en vez de levantar.

    Para callers que legítimamente pueden no tener un negocio asociado (o
    todavía no haber configurado el vertical) y necesitan degradar sin
    excepción — nunca para tapar un código inválido en silencio."""
    try:
        return parse_vertical(raw)
    except UnknownVerticalError:
        return None


def heuristic_profile_version(vertical: Vertical) -> str:
    """Versión del perfil de heurísticas vigente para el vertical.

    Reemplaza el dict `_HEURISTIC_PROFILE_BY_VERTICAL` de
    `onboarding_service.py` (que hoy accede con `[]` y tira `KeyError` ante un
    vertical no contemplado) por una función total sobre `Vertical`."""
    return f"{vertical.value}:v1"
