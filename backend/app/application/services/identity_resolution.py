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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from app.domain.external_code import clave_de_codigo_externo

T = TypeVar("T")

KeyType = Literal["code", "doc", "email", "phone"]
Outcome = Literal["matched", "conflict", "needs_review", "none"]

# Prioridad de match cuando varias claves del mismo record matchean a LA MISMA
# entidad: el CÓDIGO EXTERNO gana, después documento, email y teléfono.
#
# El código va primero porque es la identidad que el NEGOCIO le asigna a la
# entidad dentro de su propio sistema, y por lo tanto la más específica de este
# tenant: el CUIT lo asigna el Estado y puede repetirse entre una persona y su
# monotributo, el email y el teléfono se comparten entre parientes y sucursales.
# Sólo importa para elegir la clave GANADORA de un match ya resuelto — cuando dos
# claves apuntan a entidades DISTINTAS eso es `conflict` y no lo desempata nadie.
_KEY_PRIORITY: tuple[KeyType, ...] = ("code", "doc", "email", "phone")


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


#: Lo que una planilla escribe en la celda de un contacto que no tiene: no es
#: un dato, y como clave de identidad juntaba a todos los que lo comparten
#: (I6-1: el «-» de Juan pisaba la ficha de Ana, que también tenía «-»).
_RELLENO_DE_CONTACTO = frozenset(
    {
        "-", "--", "---", ".", "..", "x", "no", "no tiene", "sin dato", "sin datos",
        "s/d", "sd", "sin email", "sin mail", "sin telefono", "sin teléfono",
    }
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
#: Menos dígitos que esto no es un teléfono que identifique a alguien (un
#: interno, un «0», un «123»): el número local más corto del país tiene 6.
_MIN_DIGITOS_TELEFONO = 6


def _digitos_identifican(digits: str) -> bool:
    """Un teléfono con todos los dígitos iguales («0», «000», «1111111») es
    relleno, no un número."""
    return len(set(digits)) > 1


def _documento_identifica(digits: str) -> bool:
    """Un documento de puros ceros («0», «00000000») es relleno. Uno con todos
    los dígitos iguales pero distinto de cero (DNI 22222222) existe: la
    numeración es secuencial, así que se respeta."""
    return bool(digits) and set(digits) != {"0"}


def email_identifica(value: Any) -> bool:
    """True si ``value`` es un email con forma de email (algo@algo). El relleno
    («-», «sin email», «no tiene») no identifica a nadie."""
    email = normalize_email(value)
    return bool(email) and email not in _RELLENO_DE_CONTACTO and bool(_EMAIL_RE.match(email))


def telefono_identifica(value: Any) -> bool:
    """True si ``value`` tiene dígitos suficientes y no repetidos para ser un
    teléfono: «0», «000» o «1111111» son relleno."""
    digits = normalize_digits(value)
    return len(digits) >= _MIN_DIGITOS_TELEFONO and _digitos_identifican(digits)


def contacto_sin_dato(campo: str, value: Any) -> bool:
    """True si el ``email``/``phone`` de un import es relleno y no se escribe en
    la ficha. Escribirlo era lo que sembraba la clave basura para el próximo
    import. Más laxo que la clave: un teléfono corto de verdad (un interno) se
    guarda, aunque no identifique."""
    if is_blank(value):
        return True
    if str(value).strip().lower() in _RELLENO_DE_CONTACTO:
        return True
    if campo == "email":
        # Sin «@» no es un email. Uno real mal escrito («a@b.com; c@d.com»,
        # «Juan <juan@x.com>») se guarda aunque no sirva de clave.
        return "@" not in str(value)
    if campo == "phone":
        return not _digitos_identifican(normalize_digits(value))
    return False


def _documentos(fuente: Any, doc_fields: tuple[str, ...]) -> dict[str, str]:
    """Documentos (sólo dígitos) que identifican, por campo, de un record (dict)
    o de una entidad (atributos con el mismo nombre)."""
    if isinstance(fuente, FilaDelArchivo):
        fuente = fuente.record
    out: dict[str, str] = {}
    for f in doc_fields:
        raw = fuente.get(f) if isinstance(fuente, Mapping) else getattr(fuente, f, None)
        digits = normalize_digits(raw)
        if _documento_identifica(digits):
            out[f] = digits
    return out


def documentos_se_contradicen(a: Any, b: Any, doc_fields: tuple[str, ...]) -> bool:
    """True si ``a`` y ``b`` traen el MISMO campo de documento (DNI con DNI,
    CUIT con CUIT) con valores distintos: son dos personas. Un campo que falta
    en uno de los dos no contradice — un CUIT y un DNI no se comparan."""
    docs_a = _documentos(a, doc_fields)
    docs_b = _documentos(b, doc_fields)
    return any(f in docs_b and docs_b[f] != v for f, v in docs_a.items())


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
    code_field: str = "external_code",
    source_field: str = "external_source",
) -> list[IdentityKey]:
    """Arma las claves candidatas de un record, EN ORDEN DE PRIORIDAD de match.

    ``doc_fields`` son los campos de documento del record en orden de prioridad
    (p. ej. ``("cuit", "dni")`` para cliente, ``("cuil",)`` para proveedor).
    Vacías y de relleno (``-``, ``0``, ``sin email``, un documento de puros
    ceros, un teléfono con todos los dígitos iguales o de menos de 6 dígitos) se
    descartan — no entran como clave.

    E6a: el CÓDIGO EXTERNO entra como clave fuerte y va primero. La normalización
    la hace ``domain/external_code`` —la MISMA que llena la columna indexada—, no
    ``normalize_digits``: un código no es un número y recortarle los ceros
    fusionaría ``007`` con ``7``.
    """
    keys: list[IdentityKey] = []
    codigo = clave_de_codigo_externo(record.get(code_field), record.get(source_field))
    if codigo:
        keys.append(IdentityKey("code", codigo))
    for f in doc_fields:
        digits = normalize_digits(record.get(f))
        if _documento_identifica(digits):
            keys.append(IdentityKey("doc", digits))
    # I6-1: el relleno («-», «0», «sin email») no es clave: juntaba a todos los
    # que lo compartían en una sola persona.
    if email_identifica(record.get(email_field)):
        keys.append(IdentityKey("email", normalize_email(record.get(email_field))))
    if telefono_identifica(record.get(phone_field)):
        keys.append(IdentityKey("phone", normalize_digits(record.get(phone_field))))
    return keys


def build_existing_index(
    entities: Iterable[T],
    *,
    to_record: Callable[[T], dict[str, Any]],
    doc_fields: tuple[str, ...],
    email_field: str = "email",
    phone_field: str = "phone",
    code_field: str = "external_code",
    source_field: str = "external_source",
) -> dict[IdentityKey, T]:
    """Índice ``IdentityKey → entidad`` a partir de una lista de entidades existentes.

    ``to_record`` convierte cada entidad (ORM u otro) en un dict ``{campo: valor}`` para
    reusar ``record_keys``. El primer registro en ocupar una clave gana (``setdefault``).
    """
    index: dict[IdentityKey, T] = {}
    for entity in entities:
        for key in record_keys(
            to_record(entity),
            doc_fields=doc_fields,
            email_field=email_field,
            phone_field=phone_field,
            code_field=code_field,
            source_field=source_field,
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
    #: I6-1: el ``conflict`` no es por dos fichas sino porque el documento de la
    #: fila contradice al de la única ficha que matcheó por documento.
    documento_contradice: bool = False


@dataclass(frozen=True, eq=False)
class FilaDelArchivo:
    """Una fila anterior del MISMO archivo que se va a crear: ocupa sus claves
    en el índice del preview igual que la ficha recién creada las ocupa en el
    índice de ``apply_import``. Sus documentos salen de ``record``."""

    idx: int
    record: Mapping[str, Any]


class ResolutorDelArchivo:
    """Resuelve las filas de un preview en orden, con el MISMO índice que va a
    ver el confirm (I6-1). Antes el preview marcaba «repetida en el archivo» a
    toda fila que compartía una clave con otra anterior, mientras el confirm
    resolvía contra la base: con un email de familia, la fila de Ana salía como
    repetida de la de Juan y su dato no llegaba nunca.

    ``resolver`` devuelve la resolución y, si la fila repite a una anterior, el
    índice de esa fila: coincide con una fila que se va a crear, o actualiza la
    misma ficha existente que una fila anterior con la que comparte una clave
    (dos filas de la misma ficha con claves distintas —una por DNI, otra por
    teléfono— se aplican las dos, como antes).
    """

    def __init__(
        self, existing_index: dict[IdentityKey, Any], doc_fields: tuple[str, ...]
    ) -> None:
        self._index: dict[IdentityKey, Any] = dict(existing_index)
        self._doc_fields = doc_fields
        #: Ficha existente → filas que ya la actualizan, con sus claves.
        self._actualizadas: dict[int, list[tuple[int, set[IdentityKey]]]] = {}

    def resolver(
        self, idx: int, record: Mapping[str, Any], keys: list[IdentityKey]
    ) -> tuple[ResolutionResult, int | None]:
        res = resolve_identity(keys, self._index, record=record, doc_fields=self._doc_fields)
        if res.outcome == "matched":
            entity = res.entity
            if isinstance(entity, FilaDelArchivo):
                return res, entity.idx
            previas = self._actualizadas.setdefault(id(entity), [])
            repetida = next((i for i, ks in previas if ks & set(keys)), None)
            if repetida is None:
                previas.append((idx, set(keys)))
            return res, repetida
        if res.outcome == "conflict":
            filas = [e for e in res.conflicting_entities if isinstance(e, FilaDelArchivo)]
            return res, (filas[0].idx if filas else None)
        if res.outcome == "none":
            fila = FilaDelArchivo(idx, record)
            for k in keys:
                self._index.setdefault(k, fila)
        return res, None


def resolve_identity(
    keys: list[IdentityKey],
    existing_index: dict[IdentityKey, Any],
    *,
    record: Mapping[str, Any] | None = None,
    doc_fields: tuple[str, ...] = (),
) -> ResolutionResult:
    """Resuelve un record (ya reducido a sus claves) contra el índice de existentes.

    Puro: no toca DB. El caller decide qué hacer con cada outcome (crear, actualizar,
    marcar needs_review, reportar conflicto).

    I6-1 — con ``record`` y ``doc_fields``, el documento manda sobre el contacto:
    si la ficha encontrada por EMAIL o TELÉFONO tiene en el mismo campo un
    documento distinto del de la fila, son dos personas que comparten el
    contacto (una familia, una sucursal) y esa coincidencia se descarta; si no
    queda otra, el resultado es ``none`` (otra persona, se crea). Si la que
    contradice es una coincidencia por documento (mismo CUIT, otro DNI), el dato
    es contradictorio y el resultado es ``conflict`` con
    ``documento_contradice``: nunca se pisa el documento de una ficha con el de
    otra persona. El CÓDIGO externo no entra en esa regla: es la identidad que
    le da el propio negocio y le gana al documento, como en la vinculación de
    ventas (B·7) — un DNI corregido en el sistema de origen entra por reimport.
    """
    if not keys:
        return ResolutionResult(outcome="needs_review")

    matches: dict[IdentityKey, Any] = {}
    for k in keys:
        entity = existing_index.get(k)
        if entity is not None:
            matches[k] = entity

    if record is not None and doc_fields:
        contradice = {
            id(e): e
            for e in matches.values()
            if documentos_se_contradicen(record, e, doc_fields)
        }
        if contradice:
            matches = {
                k: e
                for k, e in matches.items()
                if not (id(e) in contradice and k.type in ("email", "phone"))
            }
            por_documento = [
                e for k, e in matches.items() if id(e) in contradice and k.type == "doc"
            ]
            por_codigo = any(k.type == "code" for k in matches)
            if por_documento and not por_codigo:
                return ResolutionResult(
                    outcome="conflict",
                    conflicting_entities=por_documento[:1],
                    documento_contradice=True,
                )

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
