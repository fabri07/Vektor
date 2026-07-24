"""customer_import_service — import masivo de clientes en dos pasos (preview/confirm).

Reutiliza el parser determinístico de ``customer_extraction_service.parse_customer_records``
para leer la planilla, y el motor común ``identity_resolution`` para matchear cada fila
contra los clientes existentes (documento → email → teléfono; el nombre es señal débil).
Arma un preview (a crear / a actualizar / inválido / duplicado en el archivo / needs_review
— sin clave fuerte, no matchea ni crea). El confirm aplica el upsert idempotente: re-resuelve
contra la DB y crea o actualiza, sin duplicar; needs_review y conflictos de identidad se
saltean siempre. El sentinela "Local" nunca se crea por import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from pydantic_core import PydanticCustomError

from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    record_keys,
    resolve_identity,
)
from app.domain.date_parsing import BIRTHDAY_CENTURY_PIVOT, parse_business_date
from app.persistence.models.customer import Customer
from app.persistence.repositories.customer_repository import CustomerRepository
from app.schemas._ar_fiscal import validate_cuit, validate_dni

# Documento: CUIT antes que DNI (prioridad de match del motor de identidad).
_DOC_FIELDS = ("cuit", "dni")

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
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review"
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

    @property
    def needs_review(self) -> int:
        return sum(1 for i in self.items if i.status == "needs_review")


@dataclass
class ImportResult:
    created_ids: list[UUID] = field(default_factory=list)
    updated_ids: list[UUID] = field(default_factory=list)
    skipped: int = 0
    # F7d: desglose de `skipped` (siempre skipped == needs_review + invalid) — la
    # taxonomía reconciliada de contadores de maestro necesita distinguir "sin
    # clave fuerte para identificar sin ambigüedad" de "dato inválido/conflicto",
    # sin romper `skipped` (usado hoy en la respuesta pública del import manual).
    needs_review: int = 0
    invalid: int = 0


def _record_keys(record: dict[str, Any]) -> list[IdentityKey]:
    """Claves de identidad del record — CUIT → DNI → email → teléfono."""
    return record_keys(record, doc_fields=_DOC_FIELDS)


def _validate_record(record: dict[str, Any]) -> list[str]:
    """Diagnóstico de una fila de import. Lista vacía = válida (puede seguir siendo
    ``needs_review`` más adelante si no trae ninguna clave fuerte — eso lo resuelve
    el motor de identidad, no esta función).

    Reglas: nombre/razón social obligatorio; si trae DNI o CUIT, tiene que ser válido.
    NO exige documento — una fila sin documento pero con email/teléfono es candidata a
    matchear por esas claves, y una fila sin ninguna clave fuerte cae en needs_review
    (no en invalid).
    """
    issues: list[str] = []
    if not (record.get("name") or "").strip():
        issues.append("Falta nombre o razón social.")
    has_cuit = bool((record.get("cuit") or "").strip())
    has_dni = bool((record.get("dni") or "").strip())
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


def _existing_doc_map(existing: list[Customer]) -> dict[IdentityKey, Customer]:
    """Índice ``IdentityKey → Customer`` de los clientes existentes (documento/email/tel)."""
    return build_existing_index(existing, to_record=_customer_record, doc_fields=_DOC_FIELDS)


def _customer_record(cust: Customer) -> dict[str, Any]:
    return {"cuit": cust.cuit, "dni": cust.dni, "email": cust.email, "phone": cust.phone}


def build_import_preview(
    records: list[dict[str, Any]],
    existing: list[Customer],
    *,
    parse_warnings: list[str] | None = None,
) -> ImportPreview:
    """Clasifica cada fila contra los existentes. Puro, sin tocar la DB."""
    existing_index = _existing_doc_map(existing)
    seen_in_file: dict[IdentityKey, int] = {}
    items: list[PreviewItem] = []

    for idx, record in enumerate(records):
        issues = _validate_record(record)
        if issues:
            items.append(
                PreviewItem(row_index=idx, status="invalid", fields=record, issues=issues)
            )
            continue

        keys = _record_keys(record)
        # Duplicado dentro del MISMO archivo (otra fila ya trajo esta clave).
        dup_of = next((seen_in_file[k] for k in keys if k in seen_in_file), None)
        if dup_of is not None:
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="duplicate_in_file",
                    fields=record,
                    issues=[f"Documento/contacto repetido en el archivo (fila {dup_of + 1})."],
                )
            )
            continue
        for k in keys:
            seen_in_file[k] = idx

        resolution = resolve_identity(keys, existing_index)
        if resolution.outcome == "needs_review":
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="needs_review",
                    fields=record,
                    issues=[
                        "Falta un dato fuerte (DNI, CUIT, email o teléfono) para "
                        "identificar al cliente sin ambigüedad."
                    ],
                )
            )
        elif resolution.outcome == "conflict":
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="invalid",
                    fields=record,
                    issues=["El registro matchea contra más de un cliente existente."],
                )
            )
        elif resolution.outcome == "matched":
            match = resolution.entity
            assert match is not None  # invariante de "matched": siempre trae entity
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
    clasificación del cliente). Las filas inválidas se saltean, igual que las
    ``needs_review`` (sin clave fuerte) y los conflictos de identidad — nunca crean ni
    actualizan. El sentinela nunca se crea (los records no llevan ``_sentinel`` y no lo
    seteamos). Devuelve los ids creados/actualizados y la cantidad salteada.
    """
    existing = await repo.list_for_dedup(tenant_id)
    index = _existing_doc_map(existing)
    result = ImportResult()

    for record in records:
        if _validate_record(record):
            result.skipped += 1
            result.invalid += 1
            continue
        keys = _record_keys(record)
        resolution = resolve_identity(keys, index)
        # F7d: mismo mapeo que build_import_preview — needs_review (sin clave
        # fuerte) es distinto de conflict (ambiguo, tratado como invalid).
        if resolution.outcome == "needs_review":
            result.skipped += 1
            result.needs_review += 1
            continue
        if resolution.outcome == "conflict":
            result.skipped += 1
            result.invalid += 1
            continue

        if resolution.outcome == "matched":
            match = resolution.entity
            assert match is not None  # invariante de "matched": siempre trae entity
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
