"""Relectura de archivos: re-leer un archivo ya subido y re-importar corregido.

Re-descarga el archivo crudo de S3, lo re-parsea y reconcilia los registros
importados contra la versión fresca **sin obligar a resubir** y **preservando las
ediciones manuales**. Todo es auditado y reversible vía ``DataRepairRun`` +
``DataRepairItem`` (mismo patrón que ``data_repair_service``).

Insight central (verificado en ``ingestion_import_service``)
------------------------------------------------------------
``source_row_ref`` de un registro importado == el ``fingerprint`` de su fila en
``operation_fingerprints`` (ambos = ``sha256(anchor)`` donde
``anchor = "{tenant}:IMPORT_ROW:{file_id}:{ctx}:{row_index}"``). Eso permite
mapear registro ↔ fingerprint **exactamente** por ese valor:

  - ``_source_row_ref(anchor) == hashlib.sha256(anchor).hexdigest()``
  - ``_register_import_row_fingerprint`` / ``_import_row_seen`` usan el mismo hash.

Estrategia (reusar ``insert_confirmed_data``)
---------------------------------------------
1. Preservar los registros editados a mano (``has_user_edits=True``): NO se tocan
   y se **conserva** su fingerprint → la re-importación los saltea.
2. Para los no editados: void (soft delete) + borrar su fingerprint → la
   re-importación los re-crea corregidos.
3. ``insert_confirmed_data(..., source="reread")`` re-importa no-editados
   corregidos + filas nuevas; saltea editados (fingerprint presente).

dry_run / preview
-----------------
El preview corre la reconciliación **dentro de un SAVEPOINT** (``begin_nested``) y
hace rollback al final: garantiza que los contadores coinciden exactamente con lo
que haría el apply, sin escribir nada.

Undo
----
``undo_reread`` revierte el último run aplicado del archivo: des-anula los
registros voldados por ese run y borra los insertados por ese run (identificables
porque cada inserción quedó auditada en un ``DataRepairItem`` con su
``sale_entry_id`` en ``after_json``).

Limitación conocida — productos
-------------------------------
La reconciliación se enfoca en ventas y gastos (``source_upload_id`` los ata al
archivo). Los ``Product`` creados desde el archivo NO se vinculan vía
``source_upload_id`` (no existe esa columna en ``products``), así que la relectura
**no los anula ni recrea**; ``insert_confirmed_data`` igual los re-deriva de forma
idempotente (upsert por SKU/nombre). Se reporta en ``details_json``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.integrations.s3 import S3Client
from app.observability.logger import get_logger
from app.persistence.models.file import UploadedFile
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

REPAIR_TYPE_REREAD = "REREAD_FILE"
VOID_REASON_REREAD = "REREAD_REIMPORT"

# DataRepairItem.action — valores específicos de la relectura (additive en
# 20260720_0002). REREAD_VOID/REREAD_INSERT cubren ventas y gastos por igual.
ACTION_VOID = "REREAD_VOID"
ACTION_INSERT = "REREAD_INSERT"

_IMPORT_ROW_ACTION = "IMPORT_ROW"


# ── helpers de hashing (espejo de ingestion_import_service) ───────────────────


def _hash_anchor(anchor: str) -> str:
    return hashlib.sha256(anchor.encode()).hexdigest()


def _row_anchor(
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    context_id: str | None,
    row_index: int,
) -> str:
    return f"{tenant_id}:{_IMPORT_ROW_ACTION}:{file_id}:{context_id or ''}:{row_index}"


# ── dataclasses de resultado ──────────────────────────────────────────────────


@dataclass
class RereadPreview:
    file_id: uuid.UUID
    to_update: int = 0
    preserved: int = 0
    new: int = 0
    to_void: int = 0
    legacy_fallback: bool = False
    sample_changes: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "to_update": self.to_update,
            "preserved": self.preserved,
            "new": self.new,
            "to_void": self.to_void,
        }


@dataclass
class RereadApplyResult:
    run_id: uuid.UUID
    dry_run: bool
    file_id: uuid.UUID
    to_update: int = 0
    preserved: int = 0
    new: int = 0
    voided: int = 0
    inserted: int = 0
    legacy_fallback: bool = False
    items: list[dict[str, Any]] = field(default_factory=list)


# ── snapshots para auditoría ───────────────────────────────────────────────────


def _snapshot_sale(s: SaleEntry) -> dict[str, Any]:
    return {
        "kind": "sale",
        "id": str(s.id),
        "amount": str(s.amount),
        "quantity": s.quantity,
        "transaction_date": s.transaction_date.isoformat() if s.transaction_date else None,
        "payment_method": s.payment_method,
        "product_id": str(s.product_id) if s.product_id else None,
        "notes": s.notes,
        "source_row_ref": s.source_row_ref,
        "has_user_edits": s.has_user_edits,
    }


def _snapshot_expense(e: ExpenseEntry) -> dict[str, Any]:
    return {
        "kind": "expense",
        "id": str(e.id),
        "amount": str(e.amount),
        "category": e.category,
        "expense_type": e.expense_type,
        "transaction_date": e.transaction_date.isoformat() if e.transaction_date else None,
        "payment_method": e.payment_method,
        "supplier_name": e.supplier_name,
        "supplier_id": str(e.supplier_id) if e.supplier_id else None,
        "product_id": str(e.product_id) if e.product_id else None,
        "description": e.description,
        "source_row_ref": e.source_row_ref,
        "has_user_edits": e.has_user_edits,
    }


# ── carga de estado ────────────────────────────────────────────────────────────


async def _load_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> UploadedFile | None:
    result = await session.execute(
        select(UploadedFile).where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def _load_existing_records(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[list[SaleEntry], list[ExpenseEntry]]:
    """Ventas + gastos NO anulados que provienen de este archivo."""
    sales_res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.source_upload_id == file_id,
            SaleEntry.voided_at.is_(None),
        )
    )
    expenses_res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    return list(sales_res.scalars().all()), list(expenses_res.scalars().all())


async def _fresh_summary(file: UploadedFile, s3: S3Client) -> dict[str, Any]:
    """Re-descarga el crudo de S3 y lo re-parsea."""
    content = await s3.download(file.s3_key)
    return parse_uploaded_content(content, file.content_type, file.original_filename)


def _confirmed_fields_for(file: UploadedFile, fresh: dict[str, Any]) -> dict[str, bool]:
    """Campos a importar en la relectura.

    UNIÓN de lo confirmado antes con lo que el re-parseo ACTUAL detecta: la
    relectura re-interpreta el archivo, así que un tipo que ahora se detecta
    (ej. productos, cuando una hoja de catálogo dejó de rutearse como gasto) debe
    confirmarse aunque la confirmación vieja no lo incluyera. Nunca importa menos
    que antes (no se pierde lo ya confirmado) y suma lo nuevo.
    """
    fresh_defaults = default_confirmed_fields(fresh)
    stored = (file.parsed_summary_json or {}).get("confirmed_fields")
    if isinstance(stored, dict) and stored:
        keys = set(stored) | set(fresh_defaults)
        return {k: bool(stored.get(k)) or bool(fresh_defaults.get(k)) for k in keys}
    return fresh_defaults


# ── reconciliación común ───────────────────────────────────────────────────────


@dataclass
class _Reconciliation:
    edited_refs: set[str]
    non_edited: list[SaleEntry | ExpenseEntry]
    non_edited_with_ref: list[SaleEntry | ExpenseEntry]
    legacy_records: list[SaleEntry | ExpenseEntry]
    preserved_count: int
    legacy_fallback: bool


def _split_records(
    sales: list[SaleEntry], expenses: list[ExpenseEntry]
) -> _Reconciliation:
    """Parte los registros en editados (preservar) / no editados (void+reimport).

    Distingue además los que tienen ``source_row_ref`` (camino exacto) de los
    legacy sin ref (fallback best-effort).
    """
    all_records: list[SaleEntry | ExpenseEntry] = [*sales, *expenses]
    edited_refs: set[str] = set()
    non_edited: list[SaleEntry | ExpenseEntry] = []
    non_edited_with_ref: list[SaleEntry | ExpenseEntry] = []
    legacy_records: list[SaleEntry | ExpenseEntry] = []
    preserved = 0
    legacy_fallback = False

    for rec in all_records:
        ref = rec.source_row_ref
        if rec.has_user_edits:
            preserved += 1
            if ref:
                edited_refs.add(ref)
            else:
                legacy_fallback = True
            continue
        if ref:
            non_edited.append(rec)
            non_edited_with_ref.append(rec)
        else:
            non_edited.append(rec)
            legacy_records.append(rec)
            legacy_fallback = True

    return _Reconciliation(
        edited_refs=edited_refs,
        non_edited=non_edited,
        non_edited_with_ref=non_edited_with_ref,
        legacy_records=legacy_records,
        preserved_count=preserved,
        legacy_fallback=legacy_fallback,
    )


async def _delete_fingerprints(
    session: AsyncSession, tenant_id: uuid.UUID, fingerprints: set[str]
) -> None:
    """Borra fingerprints de operation_fingerprints (para que el reimport re-cree
    esas filas). Nunca borra los de filas editadas (no se pasan acá)."""
    if not fingerprints:
        return
    await session.execute(
        delete(OperationFingerprint).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.action_type == _IMPORT_ROW_ACTION,
            OperationFingerprint.fingerprint.in_(fingerprints),
        )
    )


def _content_key(rec: SaleEntry | ExpenseEntry) -> tuple[str, str, str]:
    """Clave de contenido (best-effort) para matchear filas legacy contra
    registros editados: (amount, fecha-día, descripción/notas)."""
    amount = str(rec.amount)
    day = rec.transaction_date.date().isoformat() if rec.transaction_date else ""
    if isinstance(rec, SaleEntry):
        descr = (rec.notes or "").strip().lower()
    else:
        descr = (rec.description or "").strip().lower()
    return (amount, day, descr)


async def _reconcile(
    session: AsyncSession,
    file: UploadedFile,
    tenant_id: uuid.UUID,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    run: DataRepairRun,
) -> RereadApplyResult:
    """Núcleo de la relectura. Asume estar dentro de una transacción que el
    caller commitea (apply) o rollbackea (preview)."""
    file_id = file.id
    sales, expenses = await _load_existing_records(session, file_id, tenant_id)
    all_existing: list[SaleEntry | ExpenseEntry] = [*sales, *expenses]
    recon = _split_records(sales, expenses)

    # Snapshot del estado pre-void de los no-editados (para auditoría + undo).
    pre_void_snapshots: dict[uuid.UUID, dict[str, Any]] = {}
    for rec in recon.non_edited:
        snap = _snapshot_sale(rec) if isinstance(rec, SaleEntry) else _snapshot_expense(rec)
        pre_void_snapshots[rec.id] = snap

    # ── Camino exacto: fingerprints a borrar = refs de no-editados con ref ──
    fingerprints_to_delete: set[str] = {
        rec.source_row_ref
        for rec in recon.non_edited_with_ref
        if rec.source_row_ref
    }

    # ── Fallback legacy: recomputar anchors del archivo y borrar los que NO
    # matcheen por contenido a un registro editado. (Best-effort; la primera
    # relectura migra todo a source_row_ref exacto.) ──
    if recon.legacy_records:
        edited_content = {
            _content_key(rec)
            for rec in all_existing
            if rec.has_user_edits
        }
        legacy_fps = _legacy_fingerprints_to_delete(
            tenant_id, file_id, fresh, edited_content, recon
        )
        fingerprints_to_delete |= legacy_fps

    # Void de todos los no-editados (soft delete auditado).
    voided = 0
    now = datetime.now(UTC)
    for rec in recon.non_edited:
        rec.voided_at = now
        rec.void_reason = VOID_REASON_REREAD
        rec.voided_by_repair_run_id = run.id
        voided += 1
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                sale_entry_id=rec.id if isinstance(rec, SaleEntry) else None,
                action=ACTION_VOID,
                before_json=pre_void_snapshots[rec.id],
                after_json=None,
                confidence="HIGH",
            )
        )

    # Borrar fingerprints de los no-editados (preservando los editados).
    await _delete_fingerprints(session, tenant_id, fingerprints_to_delete)

    # Re-importar: re-crea no-editados corregidos + filas nuevas; saltea editados
    # (los fingerprints de las filas editadas siguen presentes → insert los omite).
    await session.flush()
    await insert_confirmed_data(
        session,
        tenant_id,
        fresh,
        confirmed_fields,
        source="reread",
        uploaded_file_id=file_id,
    )
    await session.flush()

    # Auditar inserciones: registros recién creados por este reimport
    # (source_upload_id=file, voided_at NULL, y NO estaban antes).
    before_ids = {rec.id for rec in all_existing}
    inserted_items, inserted = await _audit_inserts(
        session, tenant_id, file_id, run, before_ids=before_ids
    )

    # ``new`` = inserciones que no corresponden a un registro voldado (su ref no
    # estaba entre los no-editados con ref). ``to_update`` = inserciones que sí.
    voided_refs = {
        rec.source_row_ref for rec in recon.non_edited_with_ref if rec.source_row_ref
    }
    new_count = 0
    update_count = 0
    for item in inserted_items:
        ref = (item.after_json or {}).get("source_row_ref")
        if ref and ref in voided_refs:
            update_count += 1
        else:
            new_count += 1

    items_payload = [
        {
            "action": it.action,
            "before": it.before_json,
            "after": it.after_json,
        }
        for it in inserted_items
    ]
    # incluir también los voids en el payload de items
    void_items_payload = [
        {"action": ACTION_VOID, "before": pre_void_snapshots[rec.id], "after": None}
        for rec in recon.non_edited
    ]

    return RereadApplyResult(
        run_id=run.id,
        dry_run=run.dry_run,
        file_id=file_id,
        to_update=update_count,
        preserved=recon.preserved_count,
        new=new_count,
        voided=voided,
        inserted=inserted,
        legacy_fallback=recon.legacy_fallback,
        items=void_items_payload + items_payload,
    )


def _legacy_fingerprints_to_delete(
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    fresh: dict[str, Any],
    edited_content: set[tuple[str, str, str]],
    recon: _Reconciliation,
) -> set[str]:
    """Best-effort: recomputa anchors de todas las filas re-parseadas y devuelve
    los fingerprints a borrar (todos salvo los que matcheen por contenido a un
    registro editado). Cubre los registros legacy sin source_row_ref."""
    fps: set[str] = set()
    contexts = _iter_fresh_rows(fresh)
    for context_id, idx, row in contexts:
        anchor = _row_anchor(tenant_id, file_id, context_id, idx)
        fp = _hash_anchor(anchor)
        if _row_matches_edited(row, edited_content):
            continue
        fps.add(fp)
    return fps


def _iter_fresh_rows(
    fresh: dict[str, Any],
) -> list[tuple[str | None, int, dict[str, Any]]]:
    """Itera las filas del summary fresco aproximando el orden/contexto que usa
    ``insert_confirmed_data`` para anclar (single-sheet: context_id=None).

    Best-effort: para multi-hoja usa los context_id de ``mapping_contexts`` si
    están; si no, cae a None. Suficiente para el fallback legacy."""
    out: list[tuple[str | None, int, dict[str, Any]]] = []
    inferred = fresh.get("inferred_type", "general")
    if inferred == "stock":
        rows = fresh.get("stock_detectado", []) or []
    else:
        rows = (
            fresh.get("ventas_detectadas", [])
            or fresh.get("gastos_detectados", [])
            or fresh.get("otros_detectados", [])
            or []
        )
    for idx, row in enumerate(rows):
        if isinstance(row, dict):
            out.append((None, idx, row))
    return out


def _row_matches_edited(
    row: dict[str, Any], edited_content: set[tuple[str, str, str]]
) -> bool:
    """Heurística laxa: si algún valor de la fila contiene un monto/descr que
    matchea un registro editado, la consideramos editada y NO borramos su fp."""
    if not edited_content:
        return False
    values = " ".join(str(v).strip().lower() for v in row.values() if v is not None)
    for amount, _day, descr in edited_content:
        if descr and descr in values:
            return True
        if amount and amount.split(".")[0] in values:
            return True
    return False


async def _audit_inserts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    run: DataRepairRun,
    before_ids: set[uuid.UUID],
) -> tuple[list[DataRepairItem], int]:
    """Detecta los registros insertados por el reimport (no estaban en before_ids)
    y crea un DataRepairItem por cada uno (after_json con su snapshot + id, base
    del undo)."""
    sales_res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.source_upload_id == file_id,
            SaleEntry.voided_at.is_(None),
        )
    )
    expenses_res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    items: list[DataRepairItem] = []
    inserted = 0
    for s in sales_res.scalars().all():
        if s.id in before_ids:
            continue
        item = DataRepairItem(
            run_id=run.id,
            tenant_id=tenant_id,
            source_file_id=file_id,
            sale_entry_id=s.id,
            action=ACTION_INSERT,
            before_json=None,
            after_json=_snapshot_sale(s),
            confidence="HIGH",
        )
        session.add(item)
        items.append(item)
        inserted += 1
    for e in expenses_res.scalars().all():
        if e.id in before_ids:
            continue
        item = DataRepairItem(
            run_id=run.id,
            tenant_id=tenant_id,
            source_file_id=file_id,
            sale_entry_id=None,
            action=ACTION_INSERT,
            before_json=None,
            # expense_id no tiene columna propia en DataRepairItem; guardamos el id
            # en after_json para el undo.
            after_json=_snapshot_expense(e),
            confidence="HIGH",
        )
        session.add(item)
        items.append(item)
        inserted += 1
    return items, inserted


# ── API pública ────────────────────────────────────────────────────────────────


async def preview_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
) -> RereadPreview:
    """dry_run: proyecta la relectura dentro de un SAVEPOINT y hace rollback.

    Garantiza que los contadores coinciden con ``apply_reread`` porque corre la
    misma reconciliación; no persiste nada."""
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = await _fresh_summary(file, s3)
    confirmed_fields = _confirmed_fields_for(file, fresh)

    # Run efímero NO persistido: lo usamos solo como portador de id/dry_run dentro
    # del savepoint; el rollback lo descarta.
    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_REREAD,
        status="RUNNING",
        dry_run=True,
    )

    savepoint = await session.begin_nested()
    try:
        session.add(run)
        await session.flush()
        result = await _reconcile(session, file, tenant_id, fresh, confirmed_fields, run)
        sample = list(result.items)[:10]
        preview = RereadPreview(
            file_id=file_id,
            to_update=result.to_update,
            preserved=result.preserved,
            new=result.new,
            to_void=result.voided,
            legacy_fallback=result.legacy_fallback,
            sample_changes=sample,
        )
        return preview
    finally:
        await savepoint.rollback()


async def apply_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
) -> RereadApplyResult:
    """Aplica la relectura: void no-editados + reimport corregido, auditado y
    reversible. El commit lo hace el caller (get_db_session)."""
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = await _fresh_summary(file, s3)
    confirmed_fields = _confirmed_fields_for(file, fresh)

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_REREAD,
        status="RUNNING",
        dry_run=False,
        details_json={"file_id": str(file_id)},
    )
    session.add(run)
    await session.flush()

    result = await _reconcile(session, file, tenant_id, fresh, confirmed_fields, run)

    run.status = "APPLIED"
    run.completed_at = datetime.now(UTC)
    run.sales_detected = result.to_update + result.new
    run.sales_voided = result.voided
    run.details_json = {
        "file_id": str(file_id),
        "to_update": result.to_update,
        "preserved": result.preserved,
        "new": result.new,
        "voided": result.voided,
        "inserted": result.inserted,
        "legacy_fallback": result.legacy_fallback,
        "products_limitation": (
            "Products no se vinculan por source_upload_id; insert_confirmed_data "
            "los re-deriva idempotentemente (upsert por SKU/nombre)."
        ),
    }
    await session.flush()

    _trigger_score(tenant_id)
    return result


async def undo_reread(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, Any]:
    """Revierte un run de relectura aplicado: des-anula los registros voldados por
    ese run y borra los insertados por ese run. El commit lo hace el caller."""
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.tenant_id != tenant_id or run.repair_type != REPAIR_TYPE_REREAD:
        raise FileNotFoundError(run_id)
    if run.dry_run:
        raise ValueError("No se puede revertir un run dry_run.")
    if run.status == "REVERTED":
        raise ValueError("Este run ya fue revertido.")

    items_res = await session.execute(
        select(DataRepairItem).where(DataRepairItem.run_id == run_id)
    )
    items = list(items_res.scalars().all())

    restored = 0
    removed = 0

    # 1. Des-anular voids: registros marcados con voided_by_repair_run_id == run_id.
    sales_voided = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_by_repair_run_id == run_id,
        )
    )
    for s in sales_voided.scalars().all():
        s.voided_at = None
        s.void_reason = None
        s.voided_by_repair_run_id = None
        restored += 1
    expenses_voided = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_by_repair_run_id == run_id,
        )
    )
    for e in expenses_voided.scalars().all():
        e.voided_at = None
        e.void_reason = None
        e.voided_by_repair_run_id = None
        restored += 1

    # 2. Borrar (hard delete) los registros insertados por el run. Sus ids están en
    # DataRepairItem.after_json (action == REREAD_INSERT).
    insert_sale_ids: set[uuid.UUID] = set()
    insert_expense_ids: set[uuid.UUID] = set()
    for it in items:
        if it.action != ACTION_INSERT or not it.after_json:
            continue
        kind = it.after_json.get("kind")
        raw_id = it.after_json.get("id")
        if not raw_id:
            continue
        rid = uuid.UUID(raw_id)
        if kind == "sale":
            insert_sale_ids.add(rid)
        elif kind == "expense":
            insert_expense_ids.add(rid)

    if insert_sale_ids:
        await session.execute(
            delete(SaleEntry).where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.id.in_(insert_sale_ids),
            )
        )
        removed += len(insert_sale_ids)
    if insert_expense_ids:
        await session.execute(
            delete(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.id.in_(insert_expense_ids),
            )
        )
        removed += len(insert_expense_ids)

    # 3. Restaurar fingerprints de los registros des-anulados (consistencia: la
    # próxima relectura debe volver a saltearlos si quedan editados). Re-derivamos
    # el fp desde su source_row_ref (== fp). Solo para los que tengan ref.
    restored_refs: set[str] = set()
    for it in items:
        if it.action == ACTION_VOID and it.before_json:
            ref = it.before_json.get("source_row_ref")
            if ref:
                restored_refs.add(ref)
    await _restore_fingerprints(session, tenant_id, restored_refs)

    run.status = "REVERTED"
    run.completed_at = datetime.now(UTC)
    await session.flush()

    _trigger_score(tenant_id)
    return {
        "run_id": str(run_id),
        "restored": restored,
        "removed": removed,
        "status": "REVERTED",
    }


async def _restore_fingerprints(
    session: AsyncSession, tenant_id: uuid.UUID, fingerprints: set[str]
) -> None:
    """Re-inserta fingerprints (idempotente) para filas des-anuladas."""
    if not fingerprints:
        return
    existing_res = await session.execute(
        select(OperationFingerprint.fingerprint).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.fingerprint.in_(fingerprints),
        )
    )
    existing = {row[0] for row in existing_res.all()}
    for fp in fingerprints - existing:
        session.add(
            OperationFingerprint(
                tenant_id=tenant_id,
                fingerprint=fp,
                action_type=_IMPORT_ROW_ACTION,
            )
        )


async def latest_applied_run_for_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> DataRepairRun | None:
    """Último run de relectura APLICADO (no revertido) de un archivo.

    El filtro por ``file_id`` (guardado en ``details_json``) se hace en Python para
    ser portable a SQLite (los tests no soportan operadores JSONB ``.astext``)."""
    result = await session.execute(
        select(DataRepairRun)
        .where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.dry_run.is_(False),
            DataRepairRun.status == "APPLIED",
        )
        .order_by(DataRepairRun.created_at.desc())
    )
    target = str(file_id)
    for run in result.scalars().all():
        if (run.details_json or {}).get("file_id") == target:
            return run
    return None


def _trigger_score(tenant_id: uuid.UUID) -> None:
    from app.application.services.score_trigger_service import (  # noqa: PLC0415
        trigger_score_recalculation,
    )

    try:
        trigger_score_recalculation.delay(str(tenant_id), "reread_file")
    except Exception:  # noqa: BLE001
        logger.warning("reread.score_trigger_failed", tenant_id=str(tenant_id))


# Re-export para tests
__all__ = [
    "ACTION_INSERT",
    "ACTION_VOID",
    "REPAIR_TYPE_REREAD",
    "VOID_REASON_REREAD",
    "RereadApplyResult",
    "RereadPreview",
    "apply_reread",
    "latest_applied_run_for_file",
    "preview_reread",
    "undo_reread",
]
