"""Productos de NOMBRE PARECIDO: sólo SUGIERE, nunca fusiona.

Pruebas del dueño (2026-11-10): «Jean azul clásico», «Jean azul T40», «Jeans azules
T40» y «Jean azul 40» son el mismo artículo cargado cuatro veces. La identidad de un
producto es EXACTA a propósito (barcode → sku → alias → nombre+marca): dos nombres
que sólo se parecen no colapsan solos, porque «T40» y «40» son TALLES y plegarlos
fusionaría productos distintos («ante la duda, no hay clave»).

Este módulo hace la otra mitad: agrupa los nombres que se parecen (plural, orden de
las palabras, tildes, unidades y talles aparte) para que una PERSONA los vea juntos y
decida. No escribe nada: ni fusiona, ni crea alias, ni toca stock. Devuelve, por
grupo, los talles/números de cada miembro, porque ahí está la duda real: dos
miembros con talles DISTINTOS probablemente no son el mismo producto.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from app.application.services.ingestion_import_service import (
    _PRODUCT_TOKEN_STOPWORDS,
    _plegar_plural,
)
from app.domain.text_norm import normalize_product_name

_TIENE_NUMERO_RE = re.compile(r"\d")
# «t40», «talle 40», «40»: lo que dice el TAMAÑO, no el artículo.
_TALLE_PREFIJO_RE = re.compile(r"^(?:t|talle|tl)(\d+)$")


@dataclass(frozen=True)
class ProductoParaComparar:
    id: uuid.UUID
    name: str


@dataclass(frozen=True)
class MiembroParecido:
    id: uuid.UUID
    name: str
    #: Números/talles del nombre («t40» y «40» dan {"40"}). Distintos entre miembros
    #: = probablemente NO son el mismo producto.
    numeros: frozenset[str]


@dataclass(frozen=True)
class GrupoParecido:
    clave: tuple[str, ...]
    miembros: tuple[MiembroParecido, ...]
    #: ¿Todos los miembros comparten los mismos números/talles? Si no, es una duda
    #: para la persona, no una coincidencia.
    mismos_numeros: bool = field(default=True)


def _partir(name: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """(palabras que identifican el artículo, números/talles) de un nombre."""
    palabras: list[str] = []
    numeros: set[str] = set()
    for token in normalize_product_name(name).split():
        talle = _TALLE_PREFIJO_RE.match(token)
        if talle:
            numeros.add(talle.group(1))
            continue
        if _TIENE_NUMERO_RE.search(token):
            # «500ml» → 500; el número queda aparte, la unidad no identifica.
            numeros.update(re.findall(r"\d+(?:[.,]\d+)?", token))
            continue
        raiz = _plegar_plural(token)
        if raiz and raiz not in _PRODUCT_TOKEN_STOPWORDS:
            palabras.append(raiz)
    # Orden de las palabras irrelevante: «azul jean» == «jean azul».
    return tuple(sorted(set(palabras))), frozenset(numeros)


def clave_de_parecido(name: str) -> tuple[str, ...]:
    """Clave de agrupación: las palabras del artículo, sin plural, orden ni talle."""
    return _partir(name)[0]


def agrupar_parecidos(productos: list[ProductoParaComparar]) -> list[GrupoParecido]:
    """Grupos de 2+ productos de nombre parecido, los más grandes primero.

    Un nombre sin ninguna palabra identificatoria (sólo números/unidades) no forma
    grupo: no hay con qué compararlo.
    """
    por_clave: dict[tuple[str, ...], list[MiembroParecido]] = {}
    for p in productos:
        palabras, numeros = _partir(p.name)
        if not palabras:
            continue
        por_clave.setdefault(palabras, []).append(MiembroParecido(p.id, p.name, numeros))
    grupos = [
        GrupoParecido(
            clave=clave,
            miembros=tuple(sorted(miembros, key=lambda m: m.name.lower())),
            mismos_numeros=len({m.numeros for m in miembros}) == 1,
        )
        for clave, miembros in por_clave.items()
        if len(miembros) > 1
    ]
    return sorted(grupos, key=lambda g: (-len(g.miembros), g.clave))
