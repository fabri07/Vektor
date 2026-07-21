"""customer_import_service — import masivo de clientes en dos pasos (preview/confirm).

Reutiliza el parser determinístico de ``customer_extraction_service.parse_customer_records``
para leer la planilla, matchea cada fila contra los clientes existentes por documento
(DNI/CUIT) y arma un preview (a crear / a actualizar / inválidos / duplicados en el
archivo). El confirm aplica el upsert idempotente: matchea de nuevo por documento contra
la DB y crea o actualiza, sin duplicar. El sentinela "Local" nunca se crea por import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from pydantic_core import PydanticCustomError

from app.domain.date_parsing import BIRTHDAY_CENTURY_PIVOT, parse_business_date
from app.persistence.models.customer import Customer
from app.persistence.repositories.customer_repository import CustomerRepository
from app.schemas._ar_fiscal import validate_cuit, validate_dni

# Campos del cliente que el import puede setear/actualizar (subconjunto del modelo).
_IMPORTABLE_FIELDS = (
    "customer_type",
    "name",
    "last_name",
    "doc_type",
    "dni",
    "cuit",
    "iva_condition",
    "email",
    "phone",
    "address",
    "locality",
    "province",
    "postal_code",
    "birthday",
)


@dataclass
class PreviewItem:
    """Una fila del preview: clasificación + payload normalizado + diagnóstico."""

    row_index: int
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file"
    fields: dict[str, Any]
    existing_id: UUID | None = None
    existing_name: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    items: list[PreviewItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def to_create(self) -> int:
        return sum(1 for i in self.items if i.status == "create")

    @property
    def to_update(self) -> int:
        return sum(1 for i in self.items if i.status == "update")

    @property
    def invalid(self) -> int:
        return sum(1 for i in self.items if i.status == "invalid")

    @property
    def duplicates(self) -> int:
        return sum(1 for i in self.items if i.status == "duplicate_in_file")


@dataclass
class ImportResult:
    created_ids: list[UUID] = field(default_factory=list)
    updated_ids: list[UUID] = field(default_factory=list)
    skipped: int = 0


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value)) if value is not None else ""


def _doc_keys(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Claves de documento (tipo, dígitos) para matchear — CUIT primero."""
    keys: list[tuple[str, str]] = []
    cuit = _digits(record.get("cuit"))
    if cuit:
        keys.append(("cuit", cuit))
    dni = _digits(record.get("dni"))
    if dni:
        keys.append(("dni", dni))
    return keys


def _validate_record(record: dict[str, Any]) -> list[str]:
    """Diagnóstico de una fila de import. Lista vacía = válida.

    Reglas del import (más livianas que el alta manual, pero exigen identidad + un
    documento para poder deduplicar): nombre/razón social + (DNI o CUIT válido).
    """
    issues: list[str] = []
    if not (record.get("name") or "").strip():
        issues.append("Falta nombre o razón social.")
    has_dni = bool((record.get("dni") or "").strip())
    has_cuit = bool((record.get("cuit") or "").strip())
    if not has_dni and not has_cuit:
        issues.append("Falta un documento (DNI o CUIT).")
    if has_cuit:
        try:
            validate_cuit(record.get("cuit"))
        except PydanticCustomError as exc:
            issues.append(str(exc))
    if has_dni:
        try:
            validate_dni(record.get("dni"))
        except PydanticCustomError as exc:
            issues.append(str(exc))
    return issues


def _existing_doc_map(existing: list[Customer]) -> dict[tuple[str, str], Customer]:
    """Índice ``(tipo_doc, dígitos) → Customer`` de los clientes existentes."""
    index: dict[tuple[str, str], Customer] = {}
    for cust in existing:
        for key in _doc_keys({"cuit": cust.cuit, "dni": cust.dni}):
            index.setdefault(key, cust)
    return index


def build_import_preview(
    records: list[dict[str, Any]],
    existing: list[Customer],
    *,
    parse_warnings: list[str] | None = None,
) -> ImportPreview:
    """Clasifica cada fila contra los existentes. Puro, sin tocar la DB."""
    existing_index = _existing_doc_map(existing)
    seen_in_file: dict[tuple[str, str], int] = {}
    items: list[PreviewItem] = []

    for idx, record in enumerate(records):
        issues = _validate_record(record)
        if issues:
            items.append(
                PreviewItem(row_index=idx, status="invalid", fields=record, issues=issues)
            )
            continue

        keys = _doc_keys(record)
        # Duplicado dentro del MISMO archivo (otra fila ya trajo este documento).
        dup_of = next((seen_in_file[k] for k in keys if k in seen_in_file), None)
        if dup_of is not None:
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="duplicate_in_file",
                    fields=record,
                    issues=[f"Documento repetido en el archivo (fila {dup_of + 1})."],
                )
            )
            continue
        for k in keys:
            seen_in_file[k] = idx

        match = next((existing_index[k] for k in keys if k in existing_index), None)
        if match is not None:
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="update",
                    fields=record,
                    existing_id=match.id,
                    existing_name=match.name,
                )
            )
        else:
            items.append(PreviewItem(row_index=idx, status="create", fields=record))

    return ImportPreview(items=items, warnings=list(parse_warnings or []))


def _coerce_birthday(value: Any) -> date | None:
    # F6-C1: antes solo aceptaba ISO, así que un "12/03/1985" de una planilla se
    # perdía en silencio. Mismo parser y mismo pivote que la extracción por IA.
    return parse_business_date(value, century_pivot=BIRTHDAY_CENTURY_PIVOT)


async def apply_import(
    repo: CustomerRepository,
    tenant_id: UUID,
    records: list[dict[str, Any]],
) -> ImportResult:
    """Aplica el import: upsert idempotente por documento. Crea o actualiza, no duplica.

    Re-valida cada fila y re-resuelve el match contra la DB actual (no confía en la
    clasificación del cliente). Las filas inválidas se saltean. El sentinela nunca se
    crea (los records no llevan ``_sentinel`` y no lo seteamos). Devuelve los ids
    creados/actualizados y la cantidad salteada.
    """
    existing = await repo.list_for_dedup(tenant_id)
    index = _existing_doc_map(existing)
    result = ImportResult()

    for record in records:
        if _validate_record(record):
            result.skipped += 1
            continue
        keys = _doc_keys(record)
        match = next((index[k] for k in keys if k in index), None)

        if match is not None:
            # Actualizar solo los campos provistos (no pisar con vacío).
            for fname in _IMPORTABLE_FIELDS:
                if fname not in record:
                    continue
                value = (
                    _coerce_birthday(record[fname])
                    if fname == "birthday"
                    else record[fname]
                )
                setattr(match, fname, value)
            await repo.save(match)
            result.updated_ids.append(match.id)
        else:
            payload: dict[str, Any] = {}
            for fname in _IMPORTABLE_FIELDS:
                if fname not in record:
                    continue
                payload[fname] = (
                    _coerce_birthday(record[fname])
                    if fname == "birthday"
                    else record[fname]
                )
            customer = Customer(tenant_id=tenant_id, **payload)
            saved = await repo.save(customer)
            result.created_ids.append(saved.id)
            # Registrar el nuevo doc para no duplicar dentro del mismo batch.
            for k in keys:
                index[k] = saved

    return result
