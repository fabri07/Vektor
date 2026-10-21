"""B·6 (F-D) — campos de otra sección: venta → cliente y compra → proveedor.

Una hoja de ventas puede traer el domicilio o la condición de IVA del cliente, y
una de compras la forma de pago del proveedor. Esos datos no son de la venta ni
de la compra: completan la FICHA a la que la fila apunta. Este módulo es la parte
pura de ese camino — qué valor se propone para cada ficha y cuándo un valor es
"vacío" o inválido —; la escritura, el ledger y la reversa viven en el importador
y en ``file_deletion_service``.

Por qué un buffer y no una escritura por fila: 1187 ventas del mismo cliente dan
1187 valores posibles del mismo campo, y escribir en cada fila haría ganar a la
ÚLTIMA fila del archivo — elegir un dato de negocio por el orden en que el
importador recorrió las hojas. Acá gana la PRIMERA fila del archivo que trae un
valor válido para esa ficha, y la ficha se escribe una sola vez, al final.

``product:*`` queda afuera a propósito: toca el motor de costo (F-H6) y la
sugerencia de categoría, y un segundo camino de escritura ahí es la clase de
riesgo que esos incidentes ya dejaron cara.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.text_norm import normalize_text

CrossKind = Literal["customer", "supplier"]

#: Largo máximo de cada campo (las columnas del modelo). ``None`` = sin tope
#: (``Text``). Un valor más largo NO se recorta: recortarlo guardaría un dato que
#: el archivo no dice, así que se rechaza y se cuenta como inválido.
_MAX_LEN: dict[tuple[CrossKind, str], int | None] = {
    ("customer", "last_name"): 200,
    ("customer", "address"): None,
    ("customer", "locality"): 120,
    ("customer", "province"): 120,
    ("customer", "postal_code"): 12,
    ("customer", "customer_type"): 10,
    ("customer", "iva_condition"): 25,
    ("supplier", "last_name"): 200,
    ("supplier", "payment_method"): 30,
}

#: Campos de catálogo cerrado: el texto de la celda se traduce al valor canónico
#: (``schemas/customer.CUSTOMER_TYPES``, ``schemas/_ar_fiscal.IVA_CONDITIONS``).
#: Lo que no se reconoce es inválido, nunca se guarda tal cual: la ficha manual
#: rechazaría ese mismo valor con un 422.
_ALIASES: dict[str, dict[str, str]] = {
    "customer_type": {
        "person": "person",
        "persona": "person",
        "persona fisica": "person",
        "fisica": "person",
        "company": "company",
        "empresa": "company",
        "persona juridica": "company",
        "juridica": "company",
        "sociedad": "company",
    },
    "iva_condition": {
        "consumidor final": "consumidor_final",
        "cf": "consumidor_final",
        "monotributo": "monotributo",
        "monotributista": "monotributo",
        "responsable inscripto": "responsable_inscripto",
        "resp inscripto": "responsable_inscripto",
        "ri": "responsable_inscripto",
        "exento": "exento",
        "iva exento": "exento",
    },
}

#: Campos cuyo valor guardado no es el texto de la celda (se traduce al
#: catálogo). El comprobante los marca "transformado", no "guardado".
CROSS_FIELDS_TRANSFORMADOS: frozenset[str] = frozenset(_ALIASES)


def _clave_de_alias(texto: str) -> str:
    return normalize_text(texto.replace(".", "").replace("_", " ").replace("-", " "))


def _como_texto(raw: object) -> str | None:
    """La celda como texto, o ``None`` si está vacía.

    Un número entero que el Excel guardó como ``1425.0`` vuelve a ser ``1425``:
    un código postal no tiene decimales, y guardar el ``.0`` sería guardar un
    artefacto del formato y no lo que la persona escribió.
    """
    if raw is None:
        return None
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        if raw.is_integer():
            return str(int(raw))
    texto = str(raw).strip()
    if not texto or texto.lower() in ("nan", "none", "null"):
        return None
    return texto


def is_ficha_value_blank(value: object) -> bool:
    """¿La ficha está vacía en este campo? Sólo un campo vacío se completa."""
    return _como_texto(value) is None


@dataclass(frozen=True)
class CrossValue:
    """Resultado de normalizar una celda para un campo de la ficha.

    ``value is None and valid`` = celda vacía (no propone nada).
    ``not valid`` = la celda trae algo que la ficha no puede guardar.
    """

    value: str | None
    valid: bool


def normalize_cross_value(kind: CrossKind, field_name: str, raw: object) -> CrossValue:
    """Normaliza una celda para ``{kind}:{field_name}``.

    Un campo que no está en la lista (``_MAX_LEN``) es inválido: el importador
    sólo debería llamar con los destinos que ``CROSS_ENTITY_TARGETS_IMPLEMENTED``
    habilita, y si no, mejor no escribir que escribir un campo desconocido.
    """
    if (kind, field_name) not in _MAX_LEN:
        return CrossValue(None, False)
    texto = _como_texto(raw)
    if texto is None:
        return CrossValue(None, True)
    aliases = _ALIASES.get(field_name)
    if aliases is not None:
        canonico = aliases.get(_clave_de_alias(texto))
        return CrossValue(canonico, canonico is not None)
    tope = _MAX_LEN[(kind, field_name)]
    if tope is not None and len(texto) > tope:
        return CrossValue(None, False)
    return CrossValue(texto, True)


@dataclass(frozen=True)
class PendingCrossField:
    value: str
    source_row_ref: str | None


@dataclass
class CrossFieldBuffer:
    """Acumula ``(kind, entity_id) -> {campo: PendingCrossField}`` durante UN
    import entero (todas las hojas): dos hojas de ventas del mismo archivo que
    apuntan al mismo cliente comparten el buffer, y "primera fila" es del
    ARCHIVO, no de la hoja que se procesó primero."""

    _entries: dict[tuple[CrossKind, str], dict[str, PendingCrossField]] = field(
        default_factory=dict
    )
    #: Celdas con un valor que la ficha no puede guardar (catálogo cerrado
    #: desconocido, texto más largo que la columna). Se informan.
    invalid: int = 0

    def add(
        self,
        kind: CrossKind,
        entity_id: str,
        field_name: str,
        raw: object,
        *,
        source_row_ref: str | None,
    ) -> None:
        """Propone el valor de esta fila. Sin efecto si la celda está vacía o si
        una fila anterior del archivo ya propuso un valor válido para ese campo
        de esa ficha (la primera gana)."""
        normalizado = normalize_cross_value(kind, field_name, raw)
        if not normalizado.valid:
            self.invalid += 1
            return
        if normalizado.value is None:
            return
        campos = self._entries.setdefault((kind, entity_id), {})
        if field_name in campos:
            return
        campos[field_name] = PendingCrossField(normalizado.value, source_row_ref)

    def resolved(self) -> list[tuple[CrossKind, str, dict[str, PendingCrossField]]]:
        """Todo lo acumulado: una entrada por ficha con todos sus campos."""
        return [(kind, eid, campos) for (kind, eid), campos in self._entries.items()]

    def __len__(self) -> int:
        return len(self._entries)
