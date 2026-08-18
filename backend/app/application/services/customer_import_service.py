"""customer_import_service — import masivo de clientes en dos pasos (preview/confirm).

Reutiliza el parser determinístico de ``customer_extraction_service.parse_customer_records``
para leer la planilla, y el motor común ``identity_resolution`` para matchear cada fila
contra los clientes existentes (código externo → documento → email → teléfono; el nombre
es señal débil). Arma un preview (a crear / a actualizar / inválido / duplicado en el
archivo / needs_review — sin clave fuerte, no matchea ni crea). El confirm aplica el
upsert idempotente: re-resuelve contra la DB y crea o actualiza, sin duplicar; needs_review
y conflictos de identidad se saltean siempre. El sentinela "Local" nunca se crea por import.

F-I(B): una fila que repite CUALQUIER clave (documento/email/teléfono/``business_code``)
ya vista en ESTE archivo no fusiona sola con la entidad de la fila anterior — va a la
bandeja "Otros" para revisión humana. Antes, ``apply_import`` no trackeaba duplicados
dentro del batch (sólo ``build_import_preview`` lo hacía, sólo para el preview): la fila 2
con el mismo documento matcheaba a la entidad recién creada por la fila 1 y la actualizaba
en silencio — un merge secuencial que el usuario nunca veía venir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._master_import_shared import (
    classify_duplicate_in_file,
    persist_business_code,
    register_seen_keys,
)
from app.application.services.identity_resolution import (
    IdentityKey,
    build_existing_index,
    is_blank,
    record_keys,
    resolve_identity,
)
from app.domain.date_parsing import BIRTHDAY_CENTURY_PIVOT, parse_business_date
from app.domain.name_split import NameSplitProposal, propose_name_split
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
    # F-N: PROPUESTA de split nombre/apellido, nunca aplicada sola — el
    # usuario la ve en el preview y decide. Sólo se calcula para "create" (un
    # "update" ya tiene su propio last_name en la ficha existente, y no es
    # este servicio el que decide pisarlo) y sólo cuando el archivo trajo
    # `name` sin `last_name` — si ya vino con las dos columnas separadas, no
    # hay nada que proponer.
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
    # F7d: desglose de `skipped` (siempre skipped == needs_review + invalid) — la
    # taxonomía reconciliada de contadores de maestro necesita distinguir "sin
    # clave fuerte para identificar sin ambigüedad" de "dato inválido/conflicto",
    # sin romper `skipped` (usado hoy en la respuesta pública del import manual).
    needs_review: int = 0
    invalid: int = 0
    # F-I(B): filas con una clave (documento/email/teléfono/business_code) que
    # otra fila de ESTE archivo ya usó — van a "Otros", no se pierden ni se
    # fusionan solas. Aparte de `skipped`: la fila queda trazada, no salteada
    # en silencio.
    sent_to_others: int = 0
    # F-I(B), hallazgo del code review: la fila SÍ crea/actualiza la entidad
    # con sus otros campos, pero el `business_code` que traía ya pertenece a
    # OTRA entidad — antes se descartaba en silencio (sin log ni contador),
    # a diferencia del mismo caso en F-I(A) (`*_referencia_conflictiva`).
    business_code_conflictivo: int = 0


def _record_keys(record: dict[str, Any]) -> list[IdentityKey]:
    """Claves de identidad del record — business_code → CUIT → DNI → email → teléfono.

    ``code_key_types=("code", "business_code")``: mismo criterio que F-ID.7
    (``_classify_row_reference``) — el archivo no sabe de antemano si su
    columna de código va a matchear el ``vektor_code`` propio de una entidad
    (re-importación del propio catálogo exportado) o un ``business_code``
    externo de otra. Probar los dos tiers es lo que deja a ``resolve_identity``
    detectar un ``conflict`` real en vez de taparlo en silencio.
    """
    return record_keys(
        record,
        doc_fields=_DOC_FIELDS,
        code_field="business_code",
        code_key_types=("code", "business_code"),
    )


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


def _customer_record(cust: Customer) -> dict[str, Any]:
    return {
        "cuit": cust.cuit,
        "dni": cust.dni,
        "email": cust.email,
        "phone": cust.phone,
        "code": cust.vektor_code,
    }


async def build_existing_index_with_codes(
    session: AsyncSession, tenant_id: UUID, existing: list[Customer]
) -> dict[IdentityKey, Any]:
    """Índice ``IdentityKey → Customer`` de los existentes: documento/email/tel +
    ``vektor_code`` propio (tier "code") + ``business_code`` externo (F-I(B),
    tier "business_code", desde ``entity_identifiers``).

    F-I(B): reusa ``_augment_index_with_business_codes`` de
    ``ingestion_import_service`` — ya hace exactamente lo que hace falta (F-ID.7).
    Import LOCAL (no a nivel de módulo): ``ingestion_import_service`` importa
    ``customer_import_service`` en la dirección opuesta, también diferido, para
    no crear un ciclo entre los dos módulos.
    """
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _augment_index_with_business_codes,
    )

    index = build_existing_index(
        existing, to_record=_customer_record, doc_fields=_DOC_FIELDS, code_field="code"
    )
    await _augment_index_with_business_codes(
        session, tenant_id, "customer", index, {c.id: c for c in existing}
    )
    return index


def _maybe_name_split_suggestion(record: dict[str, Any]) -> NameSplitProposal | None:
    """`None` si el archivo ya trajo `last_name` por su cuenta (columna
    separada mapeada explícitamente) — F-N sólo propone cuando hay algo que
    proponer, nunca reemplaza un dato que ya llegó separado."""
    if not is_blank(record.get("name")) and is_blank(record.get("last_name")):
        return propose_name_split(
            record["name"],
            customer_type=record.get("customer_type"),
            doc_type=record.get("doc_type"),
        )
    return None


def build_import_preview(
    records: list[dict[str, Any]],
    existing_index: dict[IdentityKey, Any],
    *,
    parse_warnings: list[str] | None = None,
) -> ImportPreview:
    """Clasifica cada fila contra los existentes. Puro, sin tocar la DB.

    ``existing_index`` ya viene construido por el caller (F-I(B):
    ``build_existing_index_with_codes``, que necesita el ``session`` para leer
    ``entity_identifiers`` — por eso esta función deja de armarlo internamente
    y sigue siendo síncrona/pura).
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
        dup_of = classify_duplicate_in_file(keys, seen_in_file)
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
            continue
        if resolution.outcome == "conflict":
            items.append(
                PreviewItem(
                    row_index=idx,
                    status="invalid",
                    fields=record,
                    issues=["El registro matchea contra más de un cliente existente."],
                )
            )
            continue

        # Hallazgo del code review de F-I(B): registrar la clave ACÁ, recién
        # cuando se sabe que la fila va a crear/actualizar algo — nunca antes.
        # Una fila `needs_review`/`conflict` no toca ninguna entidad y no
        # puede "contaminar" la detección de duplicados de una fila posterior
        # que sí es válida por su cuenta.
        register_seen_keys(keys, seen_in_file, idx)

        if resolution.outcome == "matched":
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


def _coerce_birthday(value: Any) -> date | None:
    # F6-C1: antes solo aceptaba ISO, así que un "12/03/1985" de una planilla se
    # perdía en silencio. Mismo parser y mismo pivote que la extracción por IA.
    return parse_business_date(value, century_pivot=BIRTHDAY_CENTURY_PIVOT)


async def apply_import(
    repo: CustomerRepository,
    tenant_id: UUID,
    records: list[dict[str, Any]],
    *,
    session: AsyncSession,
    uploaded_file_id: UUID | None,
    source: str = "ingestion",
) -> ImportResult:
    """Aplica el import: upsert idempotente por documento/código. Crea o
    actualiza, no duplica.

    Re-valida cada fila y re-resuelve el match contra la DB actual (no confía en la
    clasificación del cliente). Las filas inválidas se saltean, igual que las
    ``needs_review`` (sin clave fuerte) y los conflictos de identidad — nunca crean ni
    actualizan. El sentinela nunca se crea (los records no llevan ``_sentinel`` y no lo
    seteamos).

    F-I(B): una fila cuya clave (documento/email/teléfono/``business_code``) ya
    apareció en OTRA fila de este mismo archivo NO fusiona con esa entidad —
    va a "Otros" para revisión humana. Antes, sin este chequeo, la fila 2
    matcheaba a la entidad recién creada/actualizada por la fila 1 y la volvía
    a actualizar en silencio (merge secuencial sin que el usuario lo viera).
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
        dup_of = classify_duplicate_in_file(keys, seen_in_file)
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
                suggested_entity="customer",
            )
            result.sent_to_others += capture.captured
            continue

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

        # Hallazgo del code review de F-I(B): registrar la clave ACÁ, recién
        # cuando se sabe que la fila va a crear/actualizar algo — ver el
        # docstring de `register_seen_keys`.
        register_seen_keys(keys, seen_in_file, idx)

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
                value = (
                    _coerce_birthday(record[fname])
                    if fname == "birthday"
                    else record[fname]
                )
                setattr(match, fname, value)
            await repo.save(match)
            if (
                await persist_business_code(
                    session, tenant_id, "customer", match.id, record, uploaded_file_id
                )
                is False
            ):
                result.business_code_conflictivo += 1
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
            if (
                await persist_business_code(
                    session, tenant_id, "customer", saved.id, record, uploaded_file_id
                )
                is False
            ):
                result.business_code_conflictivo += 1
            result.created_ids.append(saved.id)
            # Registrar las nuevas claves para no duplicar dentro del mismo batch.
            for k in keys:
                index[k] = saved

    return result
