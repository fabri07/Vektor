"""F-D — buffer de campos cross-sección: acumula durante el recorrido de
filas, se aplica UNA vez por entidad resuelta al final del confirm.

Por qué un buffer y no una escritura por fila: 1187 ventas del mismo cliente
dan 1187 valores posibles del mismo campo cross, y escribir en cada fila
hace ganar a la última fila del archivo — elegir un dato de negocio por un
detalle de implementación (el orden en que SQLAlchemy procesó las filas).
Acá gana la PRIMERA fila del archivo que aporta el dato para esa entidad, de
forma determinística (orden físico del archivo, no el de un dict/query), y
se escribe una sola vez por entidad al final.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Campos cross cuyo "vacío" es numérico (sólo `None` — `0` es un costo real,
#: no ausencia de dato). Todo lo demás en `CROSS_ENTITY_TARGETS` es texto.
NUMERIC_CROSS_FIELDS: frozenset[str] = frozenset({"unit_cost_ars"})


def _is_blank_text(value: object) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("", "nan")


def is_cross_value_blank(field_name: str, value: object) -> bool:
    """¿Este valor cuenta como "sin dato" para `field_name`?

    Numérico (`NUMERIC_CROSS_FIELDS`): sólo `None` vacía — `0`/`0.0` son
    costos reales, tratarlos como vacíos borraría un dato válido. Texto
    (todo lo demás): `None`, `""`, espacios, o el string `"nan"` importado.
    """
    if field_name in NUMERIC_CROSS_FIELDS:
        return value is None
    return _is_blank_text(value)


@dataclass(frozen=True)
class PendingCrossField:
    value: object
    source_row_ref: str | None


@dataclass
class CrossFieldBuffer:
    """Acumula `(kind, entity_id) -> {field: PendingCrossField}` durante el
    recorrido de filas de UN confirm entero (todas las hojas, no por hoja —
    dos hojas de ventas del mismo archivo apuntando al mismo cliente
    comparten el mismo buffer, y "primera fila" es del ARCHIVO, no de la
    hoja)."""

    _entries: dict[
        tuple[Literal["customer", "supplier", "product"], str], dict[str, PendingCrossField]
    ] = field(default_factory=dict)

    def add(
        self,
        kind: Literal["customer", "supplier", "product"],
        entity_id: str,
        field_name: str,
        value: object,
        *,
        source_row_ref: str | None,
    ) -> None:
        """Registra un valor candidato. Sin efecto si:
        - el valor es "vacío" para ese campo (`is_cross_value_blank`) — no
          hay nada que proponer;
        - la entidad YA tiene un valor pendiente para ESE campo — la primera
          fila que llegó (en orden de archivo) ya ganó, ésta se ignora.
        """
        if is_cross_value_blank(field_name, value):
            return
        key = (kind, entity_id)
        fields_for_entity = self._entries.setdefault(key, {})
        if field_name in fields_for_entity:
            return
        fields_for_entity[field_name] = PendingCrossField(
            value=value, source_row_ref=source_row_ref
        )

    def resolved(
        self,
    ) -> list[tuple[Literal["customer", "supplier", "product"], str, dict[str, PendingCrossField]]]:
        """Todo lo acumulado, listo para aplicarse — una entrada por entidad
        con TODOS los campos que le tocaron, no una por campo."""
        return [(kind, entity_id, fields) for (kind, entity_id), fields in self._entries.items()]

    def __len__(self) -> int:
        return len(self._entries)
