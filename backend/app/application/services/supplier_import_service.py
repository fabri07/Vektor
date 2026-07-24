"""supplier_import_service — import masivo de proveedores en dos pasos (preview/confirm).

Espejo de ``customer_import_service``: mismo motor de identidad
(``identity_resolution``), misma estructura de dos pasos (preview puro / confirm con
upsert idempotente). Matchea por CUIL → email → teléfono; el nombre es señal débil.
Campos acotados a lo que persiste el modelo ``Supplier`` hoy (ver ``models/supplier.py``):
no hay doc_type/address/etc. El sentinela "No identificado" y las marcas colapsadas
(``_brand_collapsed``) nunca se crean ni se actualizan por import — quedan afuera del
índice de dedup (``SupplierRepository.list_for_dedup``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic_core import PydanticCustomError

from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    is_blank,
    record_keys,
    resolve_identity,
)
from app.persistence.models.supplier import Supplier
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.schemas._ar_fiscal import validate_cuit

# Campos del proveedor que el import puede setear/actualizar (subconjunto del modelo).
_IMPORTABLE_FIELDS = ("name", "last_name", "cuil", "payment_method", "email", "phone", "notes")

# Proveedor solo tiene CUIL como documento (sin DNI, a diferencia de cliente).
_DOC_FIELDS = ("cuil",)


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
    # F7d: desglose de `skipped` (siempre skipped == needs_review + invalid) — ver
    # el mismo campo en customer_import_service.ImportResult.
    needs_review: int = 0
    invalid: int = 0


def _record_keys(record: dict[str, Any]) -> list[IdentityKey]:
    """Claves de identidad del record — CUIL → email → teléfono."""
    return record_keys(record, doc_fields=_DOC_FIELDS)


def _validate_record(record: dict[str, Any]) -> list[str]:
    """Diagnóstico de una fila de import. Lista vacía = válida.

    Reglas: nombre/razón social obligatorio; si trae CUIL, tiene que ser válido. NO
    exige CUIL — una fila sin CUIL pero con email/teléfono es candidata a matchear por
    esas claves, y una fila sin ninguna clave fuerte cae en needs_review (no invalid).
    """
    issues: list[str] = []
    if not (record.get("name") or "").strip():
        issues.append("Falta nombre o razón social.")
    cuil = (record.get("cuil") or "").strip()
    if cuil:
        try:
            validate_cuit(record.get("cuil"))
        except PydanticCustomError as exc:
            issues.append(str(exc))
    return issues


def _supplier_record(sup: Supplier) -> dict[str, Any]:
    return {"cuil": sup.cuil, "email": sup.email, "phone": sup.phone}


def _existing_index(existing: list[Supplier]) -> dict[IdentityKey, Supplier]:
    """Índice ``IdentityKey → Supplier`` de los proveedores existentes (CUIL/email/tel)."""
    return build_existing_index(existing, to_record=_supplier_record, doc_fields=_DOC_FIELDS)


def build_import_preview(
    records: list[dict[str, Any]],
    existing: list[Supplier],
    *,
    parse_warnings: list[str] | None = None,
) -> ImportPreview:
    """Clasifica cada fila contra los existentes. Puro, sin tocar la DB."""
    existing_index = _existing_index(existing)
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
                        "Falta un dato fuerte (CUIL, email o teléfono) para "
                        "identificar al proveedor sin ambigüedad."
                    ],
                )
            )
        elif resolution.outcome == "conflict":
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="invalid",
                    fields=record,
                    issues=["El registro matchea contra más de un proveedor existente."],
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


async def apply_import(
    repo: SupplierRepository,
    tenant_id: UUID,
    records: list[dict[str, Any]],
) -> ImportResult:
    """Aplica el import: upsert idempotente por CUIL/email/teléfono. Crea o actualiza,
    no duplica.

    Re-valida cada fila y re-resuelve el match contra la DB actual. Las filas
    inválidas se saltean, igual que las ``needs_review`` (sin clave fuerte) y los
    conflictos de identidad — nunca crean ni actualizan. El sentinela "No identificado"
    y las marcas colapsadas nunca se crean/pisan (quedan fuera de ``list_for_dedup``).
    Devuelve los ids creados/actualizados y la cantidad salteada.
    """
    existing = await repo.list_for_dedup(tenant_id)
    index = _existing_index(existing)
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
            # Actualizar solo los campos provistos (no pisar con vacío): una
            # columna MAPEADA pero con la celda vacía en esta fila arma
            # {campo: None} (clave presente, valor vacío) — is_blank() evita que
            # eso borre un valor existente (edición manual u otra carga).
            for fname in _IMPORTABLE_FIELDS:
                if fname not in record or is_blank(record[fname]):
                    continue
                setattr(match, fname, record[fname])
            await repo.save(match)
            result.updated_ids.append(match.id)
        else:
            payload: dict[str, Any] = {
                fname: record[fname] for fname in _IMPORTABLE_FIELDS if fname in record
            }
            supplier = Supplier(tenant_id=tenant_id, **payload)
            saved = await repo.save(supplier)
            result.created_ids.append(saved.id)
            # Registrar las nuevas claves para no duplicar dentro del mismo batch.
            for k in keys:
                index[k] = saved

    return result
