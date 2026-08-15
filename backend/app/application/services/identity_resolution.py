"""identity_resolution — motor común de identidad para import de clientes y proveedores.

Funciones puras, sin DB. Normaliza las claves candidatas (documento, email, teléfono) de
un record y las resuelve contra un índice de entidades existentes que arma el caller.

**Claves fuertes vs débiles:** el documento (CUIT/DNI para cliente, CUIL para proveedor)
es la clave fuerte PRIMARIA; email y teléfono son claves fuertes SECUNDARIAS — alcanzan
para identificar solas, pero ceden ante el documento si ambas aparecen en el mismo match.
El **nombre es señal DÉBIL: nunca identifica solo** — un record con nombre pero sin
ninguna clave fuerte no matchea, cae en `needs_review`.

Reusado hoy por ``customer_import_service`` y ``supplier_import_service`` (import masivo,
sin DB). Diseñado para que F7c lo reuse también en la resolución de referencia por fila
(venta→cliente, compra→proveedor) sin duplicar la lógica de match/conflicto — ahí el
índice se arma desde la DB en vez de desde un `list_for_dedup`, pero `resolve_identity`
no lo necesita saber.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from app.domain.text_norm import normalize_external_code

T = TypeVar("T")

KeyType = Literal["code", "business_code", "doc", "email", "phone"]
Outcome = Literal["matched", "conflict", "needs_review", "none"]

# Prioridad de match cuando varias claves del mismo record matchean a LA MISMA
# entidad: código primero (F-ID — un código es una decisión explícita, nunca
# ambigua) — "code" (vektor_code, propio y single-valued) y "business_code"
# (externo, multi-valuado vía `entity_identifiers`) están AL MISMO nivel y
# TIPADOS DISTINTO a propósito: son índices separados en el dict de identidad,
# así que un `vektor_code` de una entidad y un `business_code` de OTRA nunca
# pueden compartir el mismo slot y taparse en silencio (`index.setdefault`) —
# si el valor de la fila matchea a una entidad por cada lado, es un
# `conflict` real, no "el primero que se indexó gana". Después documento,
# después email, después teléfono.
_KEY_PRIORITY: tuple[KeyType, ...] = ("code", "business_code", "doc", "email", "phone")


def normalize_digits(value: Any) -> str:
    """Solo dígitos — documento (CUIT/DNI/CUIL) o teléfono. ``None``/vacío → ''."""
    return re.sub(r"\D", "", str(value)) if value is not None else ""


def normalize_email(value: Any) -> str:
    """Email normalizado: lowercase + trim. ``None``/vacío → ''."""
    if value is None:
        return ""
    return str(value).strip().lower()


def normalize_name(value: Any) -> str:
    """Nombre normalizado (lower + espacios colapsados) — SOLO para diagnóstico/UI.

    Nunca se usa como clave de match acá: el nombre es señal débil, no identifica.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower())


_BLANK_STRINGS = {"", "nan", "none", "null", "n/a", "na"}


def is_blank(value: Any) -> bool:
    """True si ``value`` representa "sin dato": ``None``, NaN (float), o un string
    vacío/whitespace/placeholder de nulo (``"nan"``, ``"none"``, ``"null"``, etc.
    — mismo criterio laxo que ``file_parsing._NULL_STRINGS``, sin acoplar los dos
    módulos).

    Usado por ``apply_import`` (F7d review) para que un update NUNCA pise un
    campo existente con un valor vacío: una columna MAPEADA pero con la celda
    en blanco en esta fila puntual arma ``{campo: None}`` (clave presente, valor
    vacío) — sin este chequeo, ese `None` se `setattr`-ea igual y borra una
    edición manual o un dato cargado por otra vía.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip().lower() in _BLANK_STRINGS


@dataclass(frozen=True)
class IdentityKey:
    """Una clave candidata normalizada: tipo + valor. Hashable — sirve de dict key."""

    type: KeyType
    value: str


def record_keys(
    record: dict[str, Any],
    *,
    doc_fields: tuple[str, ...],
    email_field: str = "email",
    phone_field: str = "phone",
    code_field: str | None = None,
    code_key_types: tuple[KeyType, ...] = ("code",),
) -> list[IdentityKey]:
    """Arma las claves candidatas de un record, EN ORDEN DE PRIORIDAD de match.

    ``doc_fields`` son los campos de documento del record en orden de prioridad
    (p. ej. ``("cuit", "dni")`` para cliente, ``("cuil",)`` para proveedor).
    Vacías (sin dígitos / sin email) se descartan — no entran como clave.

    ``code_field`` (F-ID) — campo con el código externo/Véktor de la entidad, si
    el record lo trae. Normaliza igual que ``sku``/``external_code``
    (``normalize_external_code``): no es dígitos-solamente como documento/
    teléfono, un código puede ser alfanumérico.

    ``code_key_types`` — bajo qué tipo(s) de ``IdentityKey`` probar ese valor.
    Default ``("code",)``: para armar el índice del PROPIO código de una
    entidad (``build_existing_index``), un solo tier. El caller que clasifica
    la referencia de una FILA (F-ID.7) pasa ``("code", "business_code")``: el
    valor de la fila no sabe de antemano si va a matchear el ``vektor_code``
    propio de una entidad o un ``business_code`` externo de otra — probar los
    dos tiers es lo que permite a ``resolve_identity`` detectar un
    ``conflict`` real si cada uno matchea una entidad DISTINTA, en vez de que
    el índice ya haya tapado uno en silencio al construirse.
    """
    keys: list[IdentityKey] = []
    if code_field is not None:
        code = normalize_external_code(record.get(code_field))
        if code:
            keys.extend(IdentityKey(kt, code) for kt in code_key_types)
    for f in doc_fields:
        digits = normalize_digits(record.get(f))
        if digits:
            keys.append(IdentityKey("doc", digits))
    email = normalize_email(record.get(email_field))
    if email:
        keys.append(IdentityKey("email", email))
    phone = normalize_digits(record.get(phone_field))
    if phone:
        keys.append(IdentityKey("phone", phone))
    return keys


def build_existing_index(
    entities: Iterable[T],
    *,
    to_record: Callable[[T], dict[str, Any]],
    doc_fields: tuple[str, ...],
    email_field: str = "email",
    phone_field: str = "phone",
    code_field: str | None = None,
) -> dict[IdentityKey, T]:
    """Índice ``IdentityKey → entidad`` a partir de una lista de entidades existentes.

    ``to_record`` convierte cada entidad (ORM u otro) en un dict ``{campo: valor}`` para
    reusar ``record_keys``. El primer registro en ocupar una clave gana (``setdefault``).

    ``code_field`` cubre el código PROPIO de cada entidad (p. ej.
    ``vektor_code``, single-valued) — un código EXTERNO adicional que una
    entidad puede acumular de varias fuentes (``entity_identifiers``,
    multi-valuado) no cabe en este patrón de un dict por entidad; el caller lo
    agrega aparte con ``index.setdefault(IdentityKey("code", ...), entity)``.
    """
    index: dict[IdentityKey, T] = {}
    for entity in entities:
        for key in record_keys(
            to_record(entity),
            doc_fields=doc_fields,
            email_field=email_field,
            phone_field=phone_field,
            code_field=code_field,
        ):
            index.setdefault(key, entity)
    return index


@dataclass
class ResolutionResult:
    """Resultado tipado de ``resolve_identity``.

    - ``matched``: una única entidad matchea — vía ``entity`` + la clave ganadora en
      ``matched_key`` (prioridad documento > email > teléfono).
    - ``conflict``: dos o más claves DISTINTAS del record matchean a entidades
      DISTINTAS (p. ej. el documento apunta a A pero el email apunta a B) —
      ``conflicting_entities`` lista las involucradas.
    - ``needs_review``: el record no trae ninguna clave fuerte (solo señal débil de
      nombre) — no identifica, no matchea, no crea.
    - ``none``: hay clave(s) fuerte(s) pero ninguna matchea en el índice → candidato
      a crear.
    """

    outcome: Outcome
    entity: Any | None = None
    matched_key: IdentityKey | None = None
    conflicting_entities: list[Any] = field(default_factory=list)


def resolve_identity(
    keys: list[IdentityKey],
    existing_index: dict[IdentityKey, Any],
) -> ResolutionResult:
    """Resuelve un record (ya reducido a sus claves) contra el índice de existentes.

    Puro: no toca DB. El caller decide qué hacer con cada outcome (crear, actualizar,
    marcar needs_review, reportar conflicto).
    """
    if not keys:
        return ResolutionResult(outcome="needs_review")

    matches: dict[IdentityKey, Any] = {}
    for k in keys:
        entity = existing_index.get(k)
        if entity is not None:
            matches[k] = entity

    if not matches:
        return ResolutionResult(outcome="none")

    # Identidad de objeto (no igualdad estructural): dos claves pueden apuntar al
    # MISMO registro existente (ej. cuit y email de la misma fila ya cargada) — eso
    # NO es conflicto. Conflicto es cuando apuntan a registros DISTINTOS.
    distinct_entities = {id(e): e for e in matches.values()}
    if len(distinct_entities) > 1:
        return ResolutionResult(
            outcome="conflict",
            conflicting_entities=list(distinct_entities.values()),
        )

    for priority in _KEY_PRIORITY:
        for k, entity in matches.items():
            if k.type == priority:
                return ResolutionResult(outcome="matched", entity=entity, matched_key=k)
    # Inalcanzable (matches no vacío y todo KeyType está en _KEY_PRIORITY), pero
    # mypy no lo sabe sin esto.
    only_key, only_entity = next(iter(matches.items()))
    return ResolutionResult(outcome="matched", entity=only_entity, matched_key=only_key)
