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
El preview corre la MISMA reconciliación que el apply dentro de un SAVEPOINT
(``begin_nested``) y hace rollback al final: los contadores y el sample
antes/después coinciden EXACTAMENTE con lo que haría el apply (una sola fuente de
verdad, sin caminos paralelos que puedan divergir). Con ``dry_run=True`` el
``_reconcile`` evita escribir las filas de auditoría (``DataRepairItem``), que no
hacen falta en una corrida que se descarta — el resto del trabajo es idéntico.
Ya sin el N+1 de huellas, el insert+rollback es mucho más rápido que antes.

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

from app.application.services import ingestion_import_service as _iis
from app.application.services import maintenance_lock_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.application.services.stock_service import (
    unvoid_movement,
    void_movement,
)
from app.integrations.s3 import S3Client
from app.observability.logger import get_logger
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

# Helpers/constantes de parseo compartidos con el import — reusados en el estimado
# del preview para clasificar/anclar las filas IGUAL que ``insert_confirmed_data``.
# Son privados del módulo origen; el reuso cross-módulo es deliberado.
_FECHA_COLS = _iis._FECHA_COLS  # type: ignore[attr-defined]
_GASTO_AMOUNT_COLS = _iis._GASTO_AMOUNT_COLS
_VENTA_AMOUNT_COLS = _iis._VENTA_AMOUNT_COLS
_NOMBRE_COLS = _iis._NOMBRE_COLS
_SKU_COLS = _iis._SKU_COLS
_CANTIDAD_COLS = _iis._CANTIDAD_COLS
_parse_amount = _iis._parse_amount
_parse_date = _iis._parse_date
_parse_qty = _iis._parse_qty
_row_val = _iis._row_val
_resolve_product = _iis._resolve_product
_load_import_fingerprints = _iis._load_import_fingerprints
_load_product_index = _iis._load_product_index

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
    # Filas cuya huella ya está registrada (import previo): el reimport las saltea
    # → no son nuevas ni se duplican. Evita el "todo nuevo" engañoso.
    unchanged: int = 0
    # Impacto en el catálogo de productos (estimado): altas + reposiciones de stock.
    products_new: int = 0
    products_restock: int = 0
    legacy_fallback: bool = False
    sample_changes: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "to_update": self.to_update,
            "preserved": self.preserved,
            "new": self.new,
            "to_void": self.to_void,
            "unchanged": self.unchanged,
            "products_new": self.products_new,
            "products_restock": self.products_restock,
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
    # F7d: maestros (clientes/proveedores) reaplicados por esta relectura —
    # creados + actualizados combinados. Ver ``_reread_master_entities``: solo se
    # reaplica si el confirm original guardó ``master_column_mappings`` en el
    # summary (sin mapeo, no se adivina el shape — mismo criterio que el confirm).
    clientes: int = 0
    proveedores: int = 0


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


def _snapshot_movement(m: InventoryMovement) -> dict[str, Any]:
    """Snapshot de un ``InventoryMovement`` para auditar la reversa de inventario
    en el reread. ``kind='movement'`` distingue estas filas de las de venta/gasto
    en ``DataRepairItem`` (reusan las mismas actions ``REREAD_VOID``/``REREAD_INSERT``,
    ya permitidas por el CHECK, para no tocar el schema)."""
    return {
        "kind": "movement",
        "id": str(m.id),
        "product_id": str(m.product_id),
        "qty": m.qty,
        "movement_type": m.movement_type,
        "source_upload_id": str(m.source_upload_id) if m.source_upload_id else None,
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


async def _reread_master_entities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file: UploadedFile,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
) -> tuple[int, int]:
    """F7d — reread de hojas de maestro (clientes/proveedores).

    Reusa el MISMO motor que el confirm (``_import_master_entities``, F7c/F7d):
    upsert idempotente (crea o actualiza, solo setea los campos que el archivo
    provee — no pisa con vacío una edición manual posterior) y needs_review/
    conflicto de identidad se saltean siempre (nunca merge silencioso).

    A diferencia de ventas/gastos, clientes/proveedores NO tienen
    ``source_upload_id`` ni ciclo void+reimport — igual que los productos (ver
    el comentario ``products_limitation`` más abajo), se re-derivan
    idempotentemente por identidad (documento/email/teléfono), así que no hace
    falta reconciliar altas/bajas acá.

    Requiere que el confirm original haya guardado ``master_column_mappings``
    en el summary (ver ``api/v1/ingestion.py::confirm_file``) — sin mapeo
    explícito no se adivina el shape de la hoja (mismo criterio que el
    confirm), así que un archivo confirmado ANTES de F7d simplemente no
    reaplica sus maestros en la relectura (cobertura documentada, no un bug).

    Devuelve ``(clientes_creados_o_actualizados, proveedores_creados_o_actualizados)``.
    """
    stored = (file.parsed_summary_json or {}).get("master_column_mappings") or {}
    context_mappings = stored.get("context") or None
    flat_mapping = stored.get("flat") or None
    if not context_mappings and not flat_mapping:
        return 0, 0

    counts: dict[str, Any] = {"clientes": 0, "proveedores": 0}
    await _iis._import_master_entities(
        session,
        tenant_id,
        fresh,
        confirmed_fields,
        context_mappings,
        None,
        flat_mapping,
        counts,
    )
    return counts.get("clientes", 0), counts.get("proveedores", 0)


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
    dry_run: bool = False,
) -> RereadApplyResult:
    """Núcleo de la relectura. Asume estar dentro de una transacción que el
    caller commitea (apply) o rollbackea (preview).

    Con ``dry_run=True`` (preview) NO escribe las filas de auditoría
    (``DataRepairItem``) — innecesarias en una corrida que se descarta, y son la
    mitad de los inserts en archivos grandes. El resto (void en memoria, borrado
    de huellas, reimport vía ``insert_confirmed_data``, conteo exacto) es idéntico
    al apply, así los contadores y el sample coinciden."""
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

    # Refs de las filas EDITADAS (preservadas): el reimport las saltea, así que su
    # InventoryMovement NO debe voidearse (si lo voidáramos sin recrearlo, el stock
    # quedaría subestimado). Se preservan movimiento + registro juntos.
    preserved_refs: set[str] = {
        rec.source_row_ref
        for rec in all_existing
        if getattr(rec, "has_user_edits", False) and rec.source_row_ref
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
        if not dry_run:
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

    # ── Inventario: borrar la lectura ANTERIOR también del lado stock ──
    # El camino compra→stock es incremental (``_record_stock_movement`` suma). Si
    # no voideamos los ``InventoryMovement`` del import previo antes de reimportar,
    # cada relectura los DUPLICA e infla el stock (confirmado en prod: 8,5x).
    # ``void_movement`` revierte el efecto de cada movimiento sobre
    # ``product.stock_units`` + ``inventory_balances`` (qty con signo) y es
    # idempotente; el reimport los vuelve a sumar ⇒ reread ×N = mismo estado.
    prev_movements_res = await session.execute(
        select(InventoryMovement).where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.source_upload_id == file_id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    for mov in prev_movements_res.scalars().all():
        # No voidear el movimiento de una fila editada preservada: el reimport la saltea,
        # así que su stock debe quedar intacto (si no, se subestimaría).
        if mov.source_row_ref and mov.source_row_ref in preserved_refs:
            continue
        mov_snap = _snapshot_movement(mov)
        await void_movement(mov, session)
        if not dry_run:
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=file_id,
                    sale_entry_id=None,
                    action=ACTION_VOID,
                    before_json=mov_snap,
                    after_json=None,
                    confidence="HIGH",
                )
            )

    # Re-importar: re-crea no-editados corregidos + filas nuevas; saltea editados
    # (los fingerprints de las filas editadas siguen presentes → insert los omite).
    await session.flush()
    # Preservar la elección de tratamiento del stock (apertura vs compra) que el usuario
    # hizo en el confirm original: vive en el summary guardado, no en el crudo re-parseado.
    _stored_treatment = (file.parsed_summary_json or {}).get("stock_treatment")
    await insert_confirmed_data(
        session,
        tenant_id,
        fresh,
        confirmed_fields,
        source="reread",
        uploaded_file_id=file_id,
        stock_treatment=_stored_treatment,
    )
    await session.flush()

    # Auditar los movimientos de inventario recién insertados por el reimport
    # (tras el void anterior, cualquier movimiento vivo del archivo es nuevo). Se
    # audita como REREAD_INSERT (kind=movement) para poder revertirlo en el undo.
    if not dry_run:
        new_movements_res = await session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.source_upload_id == file_id,
                InventoryMovement.voided_at.is_(None),
            )
        )
        for mov in new_movements_res.scalars().all():
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=file_id,
                    sale_entry_id=None,
                    action=ACTION_INSERT,
                    before_json=None,
                    after_json=_snapshot_movement(mov),
                    confidence="HIGH",
                )
            )

    # Auditar inserciones: registros recién creados por este reimport
    # (source_upload_id=file, voided_at NULL, y NO estaban antes).
    before_ids = {rec.id for rec in all_existing}
    inserted_items, inserted = await _audit_inserts(
        session, tenant_id, file_id, run, before_ids=before_ids, dry_run=dry_run
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
    dry_run: bool = False,
) -> tuple[list[DataRepairItem], int]:
    """Detecta los registros insertados por el reimport (no estaban en before_ids)
    y crea un DataRepairItem por cada uno (after_json con su snapshot + id, base
    del undo). Con ``dry_run=True`` arma los items para el conteo/sample pero NO
    los persiste (``session.add`` salteado)."""
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
        if not dry_run:
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
        if not dry_run:
            session.add(item)
        items.append(item)
        inserted += 1
    return items, inserted


# ── preview: estimación en memoria, sin escribir en la DB ──────────────────────
#
# El apply REAL inserta todas las filas del archivo (en archivos grandes tarda
# minutos); correrlo en un savepoint solo para el preview lo hacía inusable (499
# por timeout del cliente). El preview ESTIMA: reusa los mismos helpers de parseo
# y el mismo anclado (``sha256`` == ``source_row_ref``) que ``insert_confirmed_data``
# para clasificar cada fila fresca en memoria — sub-segundo sin importar el tamaño.
# ``to_void``/``preserved`` son exactos; ``new``/``to_update`` son una estimación
# muy cercana. El apply (``apply_reread``) sigue siendo la fuente de verdad exacta.

_SAMPLE_PER_KIND = 6


def _row_amount(row: dict[str, Any], kind: str) -> Any:
    """Monto parseable de la fila según el tipo (None si no hay). Usa ``is None``
    explícito para NO tratar un monto 0 (válido) como ausente."""
    if kind == "expense":
        return _parse_amount(_row_val(row, _GASTO_AMOUNT_COLS))
    if kind == "sale":
        return _parse_amount(_row_val(row, _VENTA_AMOUNT_COLS))
    v = _parse_amount(_row_val(row, _VENTA_AMOUNT_COLS))
    if v is None:
        v = _parse_amount(_row_val(row, _GASTO_AMOUNT_COLS))
    return v


def _fresh_row_snapshot(row: dict[str, Any], kind: str) -> dict[str, Any]:
    """Snapshot legible de una fila fresca para el diff antes/después (respeta el
    ``kind`` para tomar la columna de monto correcta)."""
    amount = _row_amount(row, kind)
    raw_date = _row_val(row, _FECHA_COLS)
    tx_date = _parse_date(raw_date) if raw_date is not None else None
    descr = _row_val(row, _NOMBRE_COLS)
    descr_str = str(descr).strip() if descr is not None else ""
    return {
        "kind": kind,
        "amount": str(amount) if amount is not None else None,
        "transaction_date": tx_date.date().isoformat() if tx_date else None,
        "description": descr_str if descr_str.lower() not in {"", "nan", "none"} else None,
    }


def _iter_importable_fresh_rows(
    fresh: dict[str, Any], confirmed_fields: dict[str, bool]
) -> list[tuple[str | None, int, dict[str, Any], str]]:
    """Filas frescas que producirían venta/gasto, con su ``(context_id, índice)``
    — replicando el anclado de ``insert_confirmed_data`` para reconciliar por
    ``source_row_ref``. Gatea por ``confirmed_fields`` (no cuenta tipos no
    confirmados; las filas 'otros' solo si ventas/gastos están confirmados). Los
    productos NO entran en los contadores de la relectura.
    """
    want_v = bool(confirmed_fields.get("ventas"))
    want_g = bool(confirmed_fields.get("gastos"))

    def _kind_ok(kind: str) -> bool:
        if kind == "sale":
            return want_v
        if kind == "expense":
            return want_g
        return want_v or want_g  # 'unknown' (bucket otros)

    out: list[tuple[str | None, int, dict[str, Any], str]] = []
    multi = fresh.get("inferred_type") == "mixed" or bool(fresh.get("multi_sheet"))

    if not multi:
        # Single-sheet: insert usa el PRIMER bucket no vacío, con context_id=None.
        ventas = fresh.get("ventas_detectadas") or []
        gastos = fresh.get("gastos_detectados") or []
        otros = fresh.get("otros_detectados") or []
        if ventas:
            rows, kind = ventas, "sale"
        elif gastos:
            rows, kind = gastos, "expense"
        else:
            rows, kind = otros, "unknown"
        if not _kind_ok(kind):
            return out
        for idx, row in enumerate(rows):
            if isinstance(row, dict) and _row_amount(row, kind) is not None:
                out.append((None, idx, row, kind))
        return out

    # Multi-hoja: el anclado depende de si hay ``mapping_contexts`` (mismo criterio
    # que ``_insert_multisheet_data``).
    if fresh.get("mapping_contexts"):
        # Con mapping_contexts: por bucket, agrupar por __context__ y enumerar
        # dentro de cada contexto (un contexto vive en un solo bucket).
        for bucket_key, kind in (
            ("ventas_detectadas", "sale"),
            ("gastos_detectados", "expense"),
            ("otros_detectados", "unknown"),
        ):
            if not _kind_ok(kind):
                continue
            bucket = fresh.get(bucket_key) or []
            per_ctx: dict[str | None, int] = {}
            for row in bucket:
                if not isinstance(row, dict):
                    continue
                ctx = row.get("__context__")
                idx = per_ctx.get(ctx, 0)
                per_ctx[ctx] = idx + 1
                if _row_amount(row, kind) is not None:
                    out.append((ctx, idx, row, kind))
    else:
        # Legacy (sin mapping_contexts): insert ancla con context_id LITERAL
        # 'ventas'/'gastos' e índice sobre el bucket completo.
        for bucket_key, ctx_literal, kind in (
            ("ventas_detectadas", "ventas", "sale"),
            ("gastos_detectados", "gastos", "expense"),
        ):
            if not _kind_ok(kind):
                continue
            for idx, row in enumerate(fresh.get(bucket_key) or []):
                if isinstance(row, dict) and _row_amount(row, kind) is not None:
                    out.append((ctx_literal, idx, row, kind))
    return out


def _estimate_products(
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    catalog: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Estima el impacto en el catálogo de productos: altas (nombre/SKU que NO
    está en el catálogo) y reposiciones (sí está). Cubre catálogos de stock y
    libros de compras (gasto con nombre de producto + cantidad). Dedup por SKU/
    nombre: un mismo producto en N filas = un solo producto."""
    by_sku, by_name, by_token = catalog
    is_stock = fresh.get("inferred_type") == "stock"
    rows: list[dict[str, Any]] = []
    require_qty: bool
    if is_stock:
        rows = [r for r in (fresh.get("stock_detectado") or []) if isinstance(r, dict)]
        require_qty = False
    elif confirmed_fields.get("gastos") or confirmed_fields.get("ventas"):
        # Libro de compras: filas de gasto/otros con nombre de producto + cantidad.
        for bk in ("gastos_detectados", "otros_detectados"):
            rows += [r for r in (fresh.get(bk) or []) if isinstance(r, dict)]
        require_qty = True
    else:
        return 0, 0, []

    new = 0
    restock = 0
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = _row_val(row, _NOMBRE_COLS)
        clean_name = str(name).strip() if name is not None else ""
        if not clean_name or clean_name.lower() in {"nan", "none"}:
            continue
        if require_qty and _parse_qty(_row_val(row, _CANTIDAD_COLS)) <= 0:
            continue
        sku = _row_val(row, _SKU_COLS)
        sku_str = str(sku).strip() if sku is not None else ""
        key = sku_str.lower() or clean_name.lower()
        if key in seen:
            continue
        seen.add(key)
        pid = _resolve_product(by_sku, by_name, clean_name, sku_str or None, by_token)
        after = {"kind": "product", "name": clean_name[:120], "sku": sku_str[:60] or None}
        if pid is not None:
            restock += 1
            if len([s for s in samples if s["action"] == "restock"]) < _SAMPLE_PER_KIND:
                samples.append({"action": "restock", "before": None, "after": after})
        else:
            new += 1
            if len([s for s in samples if s["action"] == "product_new"]) < _SAMPLE_PER_KIND:
                samples.append({"action": "product_new", "before": None, "after": after})
    return new, restock, samples


def _estimate_reread(
    file: UploadedFile,
    tenant_id: uuid.UUID,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    sales: list[SaleEntry],
    expenses: list[ExpenseEntry],
    fingerprints: set[str],
    catalog: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> RereadPreview:
    """Proyecta la relectura **en memoria, sin tocar la DB** (sub-segundo)."""
    file_id = file.id
    recon = _split_records(sales, expenses)
    all_recs: list[SaleEntry | ExpenseEntry] = [*sales, *expenses]
    by_ref: dict[str, SaleEntry | ExpenseEntry] = {
        rec.source_row_ref: rec for rec in all_recs if rec.source_row_ref
    }
    voided_refs = {
        rec.source_row_ref for rec in recon.non_edited_with_ref if rec.source_row_ref
    }
    edited_refs = recon.edited_refs

    update_count = 0
    new_count = 0
    unchanged_count = 0
    update_samples: list[dict[str, Any]] = []
    new_samples: list[dict[str, Any]] = []

    for ctx, idx, row, kind in _iter_importable_fresh_rows(fresh, confirmed_fields):
        fp = _hash_anchor(_row_anchor(tenant_id, file_id, ctx, idx))
        if fp in edited_refs:
            continue  # editado → insert lo saltea (preservado)
        if fp in voided_refs:
            # no-editado de ESTE archivo → se anula y re-importa corregido (update).
            update_count += 1
            if len(update_samples) < _SAMPLE_PER_KIND:
                existing = by_ref.get(fp)
                before: dict[str, Any] | None = None
                if isinstance(existing, SaleEntry):
                    before = _snapshot_sale(existing)
                elif isinstance(existing, ExpenseEntry):
                    before = _snapshot_expense(existing)
                update_samples.append(
                    {"action": "update", "before": before, "after": _fresh_row_snapshot(row, kind)}
                )
        elif fp in fingerprints:
            # Huella ya registrada (import previo) y NO se va a anular → el reimport
            # la saltea: ni nueva ni duplicada. Esto evita el "todo nuevo" engañoso.
            unchanged_count += 1
        else:
            new_count += 1
            if len(new_samples) < _SAMPLE_PER_KIND:
                new_samples.append(
                    {"action": "new", "before": None, "after": _fresh_row_snapshot(row, kind)}
                )

    # Legacy: los no-editados SIN source_row_ref (importados antes del feature) se
    # anulan y re-crean, pero no matchean por ref → caen como 'new'. Reasignar esa
    # porción de new→update (aprox), cubriendo también archivos parcialmente legacy.
    legacy_void_n = sum(1 for r in recon.non_edited if not r.source_row_ref)
    if legacy_void_n:
        move = min(legacy_void_n, new_count)
        update_count += move
        new_count -= move

    products_new, products_restock, product_samples = _estimate_products(
        fresh, confirmed_fields, catalog
    )

    void_samples: list[dict[str, Any]] = []
    for rec in recon.non_edited[:_SAMPLE_PER_KIND]:
        snap = _snapshot_sale(rec) if isinstance(rec, SaleEntry) else _snapshot_expense(rec)
        void_samples.append({"action": "void", "before": snap, "after": None})

    return RereadPreview(
        file_id=file_id,
        to_update=update_count,
        preserved=recon.preserved_count,
        new=new_count,
        to_void=len(recon.non_edited),
        unchanged=unchanged_count,
        products_new=products_new,
        products_restock=products_restock,
        legacy_fallback=recon.legacy_fallback,
        sample_changes=void_samples + update_samples + new_samples + product_samples,
    )


# ── API pública ────────────────────────────────────────────────────────────────


async def preview_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
) -> RereadPreview:
    """Preview RÁPIDO de la relectura: re-descarga + re-parsea el archivo y estima
    los cambios en memoria, **sin escribir en la DB** (sub-segundo incluso en
    archivos grandes). ``to_void``/``preserved`` exactos; ``new``/``to_update``
    estimados — el apply (``apply_reread``) es la fuente de verdad exacta. Devuelve
    un sample antes/después real para ver qué va a cambiar antes de aplicar."""
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = await _fresh_summary(file, s3)
    confirmed_fields = _confirmed_fields_for(file, fresh)
    sales, expenses = await _load_existing_records(session, file_id, tenant_id)
    # Huellas de import (lo que el apply usa para deduplicar) + catálogo de productos
    # (para estimar altas/reposiciones). Dos queries, en memoria.
    fingerprints = await _load_import_fingerprints(session, tenant_id)
    catalog = await _load_product_index(session, tenant_id)
    return _estimate_reread(
        file, tenant_id, fresh, confirmed_fields, sales, expenses, fingerprints, catalog
    )


async def apply_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
    run: DataRepairRun | None = None,
) -> RereadApplyResult:
    """Aplica la relectura: void no-editados + reimport corregido, auditado y
    reversible. El commit lo hace el caller (get_db_session o el worker).

    Si se pasa ``run`` (creado por ``start_background_apply`` y ejecutado por el
    worker), se reusa; si no, se crea uno (camino síncrono / tests)."""
    # F3-T3: la relectura crea/void productos+stock. Shared lock ANTES de mutar.
    # No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = await _fresh_summary(file, s3)
    confirmed_fields = _confirmed_fields_for(file, fresh)

    if run is None:
        run = DataRepairRun(
            tenant_id=tenant_id,
            repair_type=REPAIR_TYPE_REREAD,
            status="RUNNING",
            dry_run=False,
            details_json={"file_id": str(file_id)},
        )
        session.add(run)
        await session.flush()

    # F7d: maestros (clientes/proveedores) ANTES que la reconciliación
    # transaccional — mismo orden que el confirm (F7c), y por la misma razón: una
    # venta/gasto de este archivo puede referenciar un cliente/proveedor recién
    # actualizado. No-op si el confirm original no guardó mapeo de columnas.
    clientes_count, proveedores_count = await _reread_master_entities(
        session, tenant_id, file, fresh, confirmed_fields
    )

    result = await _reconcile(session, file, tenant_id, fresh, confirmed_fields, run)
    result.clientes = clientes_count
    result.proveedores = proveedores_count

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
        "clientes": result.clientes,
        "proveedores": result.proveedores,
        # Sample para el diff antes/después en el frontend (limitado para no inflar).
        "sample_changes": list(result.items)[:24],
        "products_limitation": (
            "Products no se vinculan por source_upload_id; insert_confirmed_data "
            "los re-deriva idempotentemente (upsert por SKU/nombre)."
        ),
    }
    await session.flush()

    _trigger_score(tenant_id)
    return result


# ── Apply en background (Celery) ───────────────────────────────────────────────

# Una relectura RUNNING más vieja que esto se considera colgada (worker caído) y
# NO bloquea una nueva — evita que un run zombie trabe el archivo para siempre.
_STALE_RUNNING_AFTER_SECONDS = 15 * 60


async def start_background_apply(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> DataRepairRun:
    """Crea el ``DataRepairRun`` (status RUNNING) para un apply en background y lo
    devuelve. Guard anti-duplicado: si ya hay una relectura RUNNING reciente del
    tenant, levanta ``ValueError`` (el caller responde 409). El caller commitea y
    encola la task. Evita el ciclo timeout→reintento→duplicados."""
    existing = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status == "RUNNING",
        )
    )
    now = datetime.now(UTC)
    for r in existing.scalars().all():
        created = r.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (now - created).total_seconds() if created is not None else 0.0
        if age < _STALE_RUNNING_AFTER_SECONDS:
            raise ValueError(
                "Ya hay una relectura en curso. Esperá a que termine antes de "
                "aplicar otra."
            )

    # Validar que el archivo exista/pertenezca antes de encolar.
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_REREAD,
        status="RUNNING",
        dry_run=False,
        details_json={"file_id": str(file_id), "phase": "queued"},
    )
    session.add(run)
    await session.flush()
    return run


async def get_reread_run(
    session: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID
) -> DataRepairRun | None:
    """Devuelve el run de relectura (para el polling de estado), validando tenant."""
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.tenant_id != tenant_id or run.repair_type != REPAIR_TYPE_REREAD:
        return None
    return run


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

    # 4. Inventario: revertir el efecto del reread sobre stock. Los movimientos que
    # el reread voideó se auditaron como REREAD_VOID (kind=movement) y los que
    # insertó como REREAD_INSERT (kind=movement). Deshacer = un-void los primeros
    # (vuelven a aplicar su qty) + void los segundos (revierten su qty). La reversa
    # es INCREMENTAL vía void_movement/unvoid_movement: NO se recomputa desde el
    # ledger porque stock_units tiene base no-ledger (alta manual, chat, seed, y el
    # catálogo que setea stock absoluto y registra solo el delta) que un recompute
    # destruiría.
    for it in items:
        after = it.after_json or {}
        before = it.before_json or {}
        if it.action == ACTION_INSERT and after.get("kind") == "movement":
            snap, do_unvoid = after, False
        elif it.action == ACTION_VOID and before.get("kind") == "movement":
            snap, do_unvoid = before, True
        else:
            continue
        raw_mid = snap.get("id")
        if not raw_mid:
            continue
        mov = await session.get(InventoryMovement, uuid.UUID(raw_mid))
        if mov is None or mov.tenant_id != tenant_id:
            continue
        if do_unvoid:
            await unvoid_movement(mov, session)
        else:
            await void_movement(mov, session)

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
