"""supplier_import_service — import masivo de proveedores en dos pasos (preview/confirm).

Espejo de ``customer_import_service``: mismo motor de identidad
(``identity_resolution``), misma estructura de dos pasos (preview puro / confirm con
upsert idempotente). Matchea por código externo → CUIL → email → teléfono; el nombre
es señal débil. Campos acotados a lo que persiste el modelo ``Supplier`` hoy (ver
``models/supplier.py``): no hay doc_type/address/etc. El sentinela "No identificado" y
las marcas colapsadas (``_brand_collapsed``) nunca se crean ni se actualizan por import —
quedan afuera del índice de dedup (``SupplierRepository.list_for_dedup``).

F-I(B): una fila que repite CUALQUIER clave (CUIL/email/teléfono/``business_code``) ya
vista en ESTE archivo va a la bandeja "Otros" para revisión humana — ver la nota
equivalente en ``customer_import_service``.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.entity_code_service import (
    EntityIdentifierConflictError,
    record_identifier,
)
from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    is_blank,
    record_keys,
    resolve_identity,
)
from app.domain.name_split import NameSplitProposal, propose_supplier_name_split
from app.persistence.models.supplier import Supplier
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.schemas._ar_fiscal import validate_cuit

# Campos del proveedor que el import puede setear/actualizar (subconjunto del modelo).
_IMPORTABLE_FIELDS = (
    "name", "last_name", "cuil", "cuit", "iva_condition", "payment_method",
    "email", "phone", "notes",
)

# Documentos del proveedor: CUIT (empresa) y CUIL (persona física). No tiene DNI,
# a diferencia de cliente. `cuit` va PRIMERO porque es el caso mayoritario en un
# padrón de proveedores y el orden fija la prioridad de la clave de identidad.
_DOC_FIELDS = ("cuit", "cuil")


@dataclass
class PreviewItem:
    """Una fila del preview: clasificación + payload normalizado + diagnóstico."""

    row_index: int
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review"
    fields: dict[str, Any]
    existing_id: UUID | None = None
    existing_name: str | None = None
    issues: list[str] = field(default_factory=list)
    # F-N: ver el campo equivalente en customer_import_service.PreviewItem.
    name_split_suggestion: NameSplitProposal | None = None


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
    # F-I(B): ver el mismo campo en customer_import_service.ImportResult.
    sent_to_others: int = 0


def _record_keys(record: dict[str, Any]) -> list[IdentityKey]:
    """Claves de identidad del record — business_code → CUIL → email → teléfono.

    Ver la nota de ``code_key_types`` en ``customer_import_service._record_keys``.
    """
    return record_keys(
        record,
        doc_fields=_DOC_FIELDS,
        code_field="business_code",
        code_key_types=("code", "business_code"),
    )


def _validate_record(record: dict[str, Any]) -> list[str]:
    """Diagnóstico de una fila de import. Lista vacía = válida.

    Reglas: nombre/razón social obligatorio; si trae CUIT o CUIL, tiene que ser
    válido. NO exige documento — una fila sin él pero con email/teléfono es
    candidata a matchear por esas claves, y una fila sin ninguna clave fuerte cae
    en needs_review (no invalid).
    """
    issues: list[str] = []
    if not (record.get("name") or "").strip():
        issues.append("Falta nombre o razón social.")
    # CUIT y CUIL comparten formato y dígito verificador (módulo 11): el mismo
    # validador sirve para los dos.
    for campo in _DOC_FIELDS:
        if (record.get(campo) or "").strip():
            try:
                validate_cuit(record.get(campo))
            except PydanticCustomError as exc:
                issues.append(str(exc))
    return issues


def _supplier_record(sup: Supplier) -> dict[str, Any]:
    return {
        "cuit": sup.cuit,
        "cuil": sup.cuil,
        "email": sup.email,
        "phone": sup.phone,
        "code": sup.vektor_code,
    }


def _maybe_name_split_suggestion(record: dict[str, Any]) -> NameSplitProposal | None:
    """`None` si el archivo ya trajo `last_name` por su cuenta. `Supplier` no
    tiene columna de tipo (a diferencia de `customer_type`) — ver la regla
    más conservadora en `propose_supplier_name_split`."""
    if not is_blank(record.get("name")) and is_blank(record.get("last_name")):
        return propose_supplier_name_split(record["name"])
    return None


async def build_existing_index_with_codes(
    session: AsyncSession, tenant_id: UUID, existing: list[Supplier]
) -> dict[IdentityKey, Any]:
    """Índice ``IdentityKey → Supplier`` de los existentes: CUIL/email/tel +
    ``vektor_code`` propio (tier "code") + ``business_code`` externo (F-I(B),
    tier "business_code", desde ``entity_identifiers``).

    Ver la nota equivalente en ``customer_import_service.build_existing_index_with_codes``
    (mismo motivo del import local diferido).
    """
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _augment_index_with_business_codes,
    )

    index = build_existing_index(
        existing, to_record=_supplier_record, doc_fields=_DOC_FIELDS, code_field="code"
    )
    await _augment_index_with_business_codes(
        session, tenant_id, "supplier", index, {s.id: s for s in existing}
    )
    return index


def build_import_preview(
    records: list[dict[str, Any]],
    existing_index: dict[IdentityKey, Any],
    *,
    parse_warnings: list[str] | None = None,
) -> ImportPreview:
    """Clasifica cada fila contra los existentes. Puro, sin tocar la DB.

    ``existing_index`` ya viene construido por el caller — ver la nota
    equivalente en ``customer_import_service.build_import_preview``.
    """
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
        # Duplicado dentro del MISMO archivo (otra fila ya trajo esta clave) —
        # F-I(B): el confirm manda esta fila a "Otros", nunca la fusiona sola.
        dup_of = next((seen_in_file[k] for k in keys if k in seen_in_file), None)
        if dup_of is not None:
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="duplicate_in_file",
                    fields=record,
                    issues=[
                        f"Documento/código/contacto repetido en el archivo (fila "
                        f"{dup_of + 1}) — va a la bandeja Otros para revisión."
                    ],
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
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="create",
                    fields=record,
                    name_split_suggestion=_maybe_name_split_suggestion(record),
                )
            )

    return ImportPreview(items=items, warnings=list(parse_warnings or []))


async def _persist_business_code(
    session: AsyncSession,
    tenant_id: UUID,
    supplier_id: UUID,
    record: dict[str, Any],
    uploaded_file_id: UUID | None,
) -> None:
    """F-I(B): ver la nota equivalente en
    ``customer_import_service._persist_business_code``."""
    raw_code = record.get("business_code")
    if raw_code is None or not str(raw_code).strip():
        return
    with suppress(EntityIdentifierConflictError):
        await record_identifier(
            session,
            tenant_id,
            "supplier",
            supplier_id,
            identifier_type="business_code",
            namespace="business",
            raw_value=str(raw_code),
            origin="business",
            source_upload_id=uploaded_file_id,
        )


async def apply_import(
    repo: SupplierRepository,
    tenant_id: UUID,
    records: list[dict[str, Any]],
    *,
    session: AsyncSession,
    uploaded_file_id: UUID | None,
    source: str = "ingestion",
) -> ImportResult:
    """Aplica el import: upsert idempotente por CUIL/email/teléfono/código. Crea
    o actualiza, no duplica.

    Re-valida cada fila y re-resuelve el match contra la DB actual. Las filas
    inválidas se saltean, igual que las ``needs_review`` (sin clave fuerte) y los
    conflictos de identidad — nunca crean ni actualizan. El sentinela "No identificado"
    y las marcas colapsadas nunca se crean/pisan (quedan fuera de ``list_for_dedup``).

    F-I(B): ver la nota equivalente en ``customer_import_service.apply_import``
    — una fila cuya clave ya apareció en OTRA fila de este archivo va a "Otros",
    nunca fusiona sola.
    """
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _capture_unclassified,
    )

    existing = await repo.list_for_dedup(tenant_id)
    index = await build_existing_index_with_codes(session, tenant_id, existing)
    seen_in_file: dict[IdentityKey, int] = {}
    result = ImportResult()

    for idx, record in enumerate(records):
        if _validate_record(record):
            result.skipped += 1
            result.invalid += 1
            continue
        keys = _record_keys(record)

        # F-I(B): duplicado dentro del MISMO archivo — a "Otros", nunca fusiona.
        dup_of = next((seen_in_file[k] for k in keys if k in seen_in_file), None)
        if dup_of is not None:
            capture = _capture_unclassified(
                session,
                tenant_id,
                rows=[record],
                headers=None,
                source=source,
                uploaded_file_id=uploaded_file_id,
                context_label=(
                    "Documento/código/contacto repetido en el archivo "
                    f"(fila {dup_of + 1}) — requiere revisión manual antes de "
                    "fusionar con esa ficha."
                ),
                suggested_entity="supplier",
            )
            result.sent_to_others += capture.captured
            continue
        for k in keys:
            seen_in_file[k] = idx

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
            await _persist_business_code(
                session, tenant_id, match.id, record, uploaded_file_id
            )
            result.updated_ids.append(match.id)
        else:
            payload: dict[str, Any] = {
                fname: record[fname] for fname in _IMPORTABLE_FIELDS if fname in record
            }
            supplier = Supplier(tenant_id=tenant_id, **payload)
            saved = await repo.save(supplier)
            await _persist_business_code(
                session, tenant_id, saved.id, record, uploaded_file_id
            )
            result.created_ids.append(saved.id)
            # Registrar las nuevas claves para no duplicar dentro del mismo batch.
            for k in keys:
                index[k] = saved

    return result
