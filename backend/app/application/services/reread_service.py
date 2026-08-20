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

F9b (Task 6): lo anterior sigue siendo cierto (no hay void+reimport de productos),
PERO desde acá los productos creados/actualizados por el upsert SÍ quedan
auditados con before/after — ``insert_confirmed_data(..., return_details=True)``
devuelve ``product_details`` (antes solo lo consumía la vía in-process, nunca la
relectura) y este módulo los materializa como ``DataRepairItem`` con
``action="CREATE_PRODUCT"``/``"UPDATE_PRODUCT"``. Habilita el touched-since check
del undo de productos (Task 7) sin necesitar el void+reimport que sí tienen
ventas/gastos.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTasks

from app.application.services import ingestion_import_service as _iis
from app.application.services import maintenance_lock_service
from app.application.services._ledger_restore import (
    MASTER_SNAPSHOT_FIELDS,
    entity_changed_since_ledger,
    restore_from_before,
    snapshot_master,
)
from app.application.services.column_mapping_service import (
    missing_required_fields,
    parse_target,
)
from app.application.services.column_risk import (
    AppliedColumnRisk,
    apply_column_risk_decisions,
    build_contextual_column_risk,
    context_is_included,
    derive_context_mapping_entries,
    resolve_contexts,
    split_derivable_decisions,
)
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.application.services.inventory_replay_service import run_inventory_replay
from app.application.services.stock_service import (
    sale_source_event_id,
    unvoid_movement,
    void_movement,
)
from app.domain.expense_categories import classify_expense_with_vertical
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.inventory_effect import (
    SheetInventoryProfile,
    replay_scope,
    resolve_inventory_effects,
)
from app.integrations.s3 import S3Client
from app.observability.logger import get_logger
from app.persistence.models.file import (
    REREAD_STATUS_APPLIED,
    REREAD_STATUS_AUTO_APPLIED,
    REREAD_STATUS_NEEDS_REVIEW,
    UploadedFile,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_ROW_REF_PREFIX,
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
    is_unclassified_row_ref,
)
from app.schemas.ingestion import ColumnMapping, ColumnRiskDecision

# F9a: outcome explícito de la resolución de riesgo de columnas en la relectura.
# Reemplaza el booleano implícito de ``_apply_risk_decisions`` (None/no-None) por
# un resultado que distingue "reaplicado tal cual" (mapeo REAL, F8b+) de un mapeo
# RE-DERIVADO (guess) para archivos pre-F8 — ver ``ResolvedRisk``.
#
# F-RR Fase 6: ``USER_REVIEWED`` es un quinto outcome, MÁS autoritativo que
# ``REAPPLIED`` — el usuario corrigió el mapeo/decisiones EN VIVO durante esta
# sesión de relectura (borrador persistido en ``run.details_json["draft"]``),
# no el mapeo guardado en el confirm original (posiblemente el mal resuelto que
# causó la relectura, ver el bug de ASTERIA). Igual que ``REAPPLIED``, siempre
# trae ``applied`` no-``None``.
RiskOutcome = Literal[
    "USER_REVIEWED", "REAPPLIED", "NO_RISK_FOUND", "FORCED_UNVERIFIED", "AMBIGUOUS"
]

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
# F-RR (Fase 4, reconciliación): MISMAS primitivas que resuelven identidad de
# producto y categoría en el import real — el estimador del impacto proyectado
# no puede tener su propia copia de este criterio, o diverge del apply (ver
# incidente ASTERIA: el preview decía "sin_producto: 0" contra 1.403/427
# reales porque nada de esto se contaba).
_CATEGORIA_COLS = _iis._CATEGORIA_COLS
_row_val_categoria = _iis._row_val_categoria
_load_tenant_vertical = _iis._load_tenant_vertical
_load_product_identity_indexes = _iis._load_product_identity_indexes
_resolve_link = _iis._resolve_link
_clean_str = _iis._clean_str
# F8b (Task 5): primitivas de captura/correlación de riesgo compartidas con el
# confirm (reuso deliberado, no se reimplementa la captura).
_capture_column_risk_rows = _iis._capture_column_risk_rows
_risk_row_anchor = _iis._risk_row_anchor
_RISK_REF_KEY = _iis.RISK_REF_KEY
_ROW_REF_KEY = _iis.ROW_REF_KEY

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
class UnlinkedProductsEstimate:
    """F-RR (Fase 4): impacto proyectado en el vínculo venta/compra↔producto,
    ANTES de aplicar — 5 categorías mutuamente excluyentes (ver docstring de
    ``estimate_unlinked_products``). Nace de un incidente real: el resumen de
    reread de la cuenta ASTERIA reportaba ``sin_producto: 0`` mientras la base
    tenía 1.403 ventas y 427 gastos/compras sin producto — nada en el código
    anterior contaba lo que se filtraba en silencio."""

    ventas_con_producto: int = 0
    ventas_sin_producto: int = 0
    ventas_sin_producto_samples: list[dict[str, Any]] = field(default_factory=list)
    compras_vinculadas: int = 0
    #: Producto nuevo que SE CREARÁ y quedará vinculado en el apply real —
    #: distinto de `compras_sin_producto` (ambiguo, NO se crea nada).
    compras_producto_nuevo: int = 0
    compras_sin_producto: int = 0
    compras_sin_producto_samples: list[dict[str, Any]] = field(default_factory=list)
    #: El bug real de ASTERIA: la fila ni siquiera INTENTA resolver producto
    #: porque falta la cantidad (o el nombre) — típicamente la hoja no tiene
    #: esa columna mapeada. Releer con el MISMO mapeo nunca lo arregla solo.
    compras_gate_bloqueado: int = 0
    compras_gate_bloqueado_samples: list[dict[str, Any]] = field(default_factory=list)
    #: Servicios/alquiler/etc: legítimamente no requieren producto — sin esta
    #: categoría se mezclarían con las filas realmente rotas.
    movimientos_sin_producto_esperado: int = 0


@dataclass
class RereadPreview:
    file_id: uuid.UUID
    to_update: int = 0
    preserved: int = 0
    #: F-O.1 — ver el campo homónimo de ``RereadApplyResult``.
    preserved_from_others: int = 0
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
    # F9a: resultado de la resolución de riesgo de columnas (ver ``ResolvedRisk``).
    # Default "NO_RISK_FOUND" — el valor conservador si nadie lo pisa explícitamente.
    column_risk_outcome: str = "NO_RISK_FOUND"
    column_risk_ambiguous: list[dict[str, Any]] = field(default_factory=list)
    column_risk_forced_unverified: list[dict[str, Any]] = field(default_factory=list)
    # F-RR (Fase 4): impacto proyectado en el vínculo venta/compra↔producto —
    # ver invariante de reconciliación en ``estimate_unlinked_products``.
    unlinked_products: UnlinkedProductsEstimate = field(
        default_factory=UnlinkedProductsEstimate
    )
    # F-RR Fase 8 (backend): revisión completa de interpretación — hoja/sección
    # efectiva, mapeo, riesgo. Best-effort (ver ``build_reread_sheets``): un
    # fallo acá nunca debe tumbar el preview completo.
    sheets: list[dict[str, Any]] = field(default_factory=list)
    mapping_contexts: list[dict[str, Any]] = field(default_factory=list)
    contextual_column_risk: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "to_update": self.to_update,
            "preserved": self.preserved,
            "preserved_from_others": self.preserved_from_others,
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
    #: F-O.1 — de los preservados, cuántos lo fueron por venir de "Otros" (una
    #: decisión de clasificación) y no por una edición manual. Son dos motivos
    #: distintos y el informe tiene que poder decir cuál.
    preserved_from_others: int = 0
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
    # F9a: resultado de la resolución de riesgo de columnas (ver ``ResolvedRisk``).
    column_risk_outcome: str = "NO_RISK_FOUND"
    column_risk_ambiguous: list[dict[str, Any]] = field(default_factory=list)
    column_risk_forced_unverified: list[dict[str, Any]] = field(default_factory=list)
    # F-RR (Fase 4): None si lo proyectado en el preview coincide con lo
    # efectivamente persistido; si no, el detalle del desvío — ver
    # ``_verify_unlinked_products_reconciliation``.
    reconciliation_warning: dict[str, Any] | None = None


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


# Definidos en `_ledger_restore`: los comparte el borrado de archivo, que revierte
# con las MISMAS reglas. Alias local para no tocar los usos de este módulo.
_MASTER_SNAPSHOT_FIELDS = MASTER_SNAPSHOT_FIELDS


# Definido en `_ledger_restore`: el confirm inicial captura el MISMO snapshot, y
# el borrado de archivo restaura desde él. Alias local para no tocar los usos.
_snapshot_master = snapshot_master


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


async def file_has_user_edits(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    """``True`` si algún registro derivado de este archivo tiene ediciones
    manuales (``has_user_edits=True``), NO anulado.

    F9a (Task 4): el batch de relectura usa esto para decidir si un archivo es
    seguro de reprocesar sin supervisión — un archivo con ediciones manuales NO
    debería auto-aplicarse (aunque el outcome de riesgo fuera REAPPLIED), porque
    la relectura reimporta corregido y podría pisar contexto que el humano ya
    ajustó a mano en otras filas del mismo archivo.

    Chequea tres fuentes, cualquiera en ``True`` alcanza:
    - ``SaleEntry`` de este archivo con ``has_user_edits=True`` (no anulada).
    - ``ExpenseEntry`` de este archivo con ``has_user_edits=True`` (no anulada).
    - ``Product`` con ``has_user_edits=True`` vinculado INDIRECTAMENTE: ``Product``
      no tiene ``source_upload_id`` propio (no existe esa columna en el modelo);
      el vínculo es vía ``ExpenseEntry.source_upload_id == file_id`` →
      ``ExpenseEntry.product_id == Product.id`` (compra de mercadería que creó/
      actualizó el producto), acotado a gastos no anulados.
    """
    sale_edit = await session.execute(
        select(SaleEntry.id)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.source_upload_id == file_id,
            SaleEntry.has_user_edits.is_(True),
            SaleEntry.voided_at.is_(None),
        )
        .limit(1)
    )
    if sale_edit.scalar_one_or_none() is not None:
        return True

    expense_edit = await session.execute(
        select(ExpenseEntry.id)
        .where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.has_user_edits.is_(True),
            ExpenseEntry.voided_at.is_(None),
        )
        .limit(1)
    )
    if expense_edit.scalar_one_or_none() is not None:
        return True

    product_edit = await session.execute(
        select(Product.id)
        .join(ExpenseEntry, ExpenseEntry.product_id == Product.id)
        .where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.voided_at.is_(None),
            Product.has_user_edits.is_(True),
        )
        .limit(1)
    )
    return product_edit.scalar_one_or_none() is not None


async def _fresh_summary(file: UploadedFile, s3: S3Client) -> dict[str, Any]:
    """Re-descarga el crudo de S3 y lo re-parsea."""
    content = await s3.download(file.s3_key)
    return parse_uploaded_content(content, file.content_type, file.original_filename)


def _confirmed_fields_for(
    file: UploadedFile, fresh: dict[str, Any], draft: dict[str, Any] | None = None
) -> dict[str, bool]:
    """Campos a importar en la relectura.

    F-RR Fase 6: si el borrador de la sesión trae ``confirmed_fields``
    explícito (el usuario revisó y corrigió qué importar durante ESTA
    relectura), se usa TAL CUAL — a diferencia de la unión de abajo, una
    corrección real puede querer DESMARCAR algo que estaba confirmado antes.

    Sin borrador (camino de siempre): UNIÓN de lo confirmado antes con lo que
    el re-parseo ACTUAL detecta: la relectura re-interpreta el archivo, así que
    un tipo que ahora se detecta (ej. productos, cuando una hoja de catálogo
    dejó de rutearse como gasto) debe confirmarse aunque la confirmación vieja
    no lo incluyera. Nunca importa menos que antes (no se pierde lo ya
    confirmado) y suma lo nuevo.
    """
    if draft is not None:
        draft_confirmed = draft.get("confirmed_fields")
        if isinstance(draft_confirmed, dict) and draft_confirmed:
            return {k: bool(v) for k, v in draft_confirmed.items()}
    fresh_defaults = default_confirmed_fields(fresh)
    stored = (file.parsed_summary_json or {}).get("confirmed_fields")
    if isinstance(stored, dict) and stored:
        keys = set(stored) | set(fresh_defaults)
        return {k: bool(stored.get(k)) or bool(fresh_defaults.get(k)) for k in keys}
    return fresh_defaults


async def load_reread_run_summary(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any]:
    """F-RR Fase 6: summary FRESCO cacheado de una sesión de relectura abierta —
    para que los endpoints de borrador (``column-mappings``/``column-risk``/
    ``inventory-effects``/``purchase-groups``) calculen sugerencias/riesgo
    contra lo que la relectura REALMENTE releyó, no contra
    ``record.parsed_summary_json`` (el confirm original — potencialmente el
    mapeo mal resuelto que motivó la relectura, caso ASTERIA)."""
    run = await session.get(DataRepairRun, run_id)
    if (
        run is None
        or run.tenant_id != tenant_id
        or run.repair_type != REPAIR_TYPE_REREAD
        or (run.details_json or {}).get("file_id") != str(file_id)
    ):
        raise FileNotFoundError(run_id)
    return (run.details_json or {}).get("fresh_summary") or {}


async def _reread_master_entities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file: UploadedFile,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    run_id: uuid.UUID,
    draft: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """F7d + F9b — reread de hojas de maestro (clientes/proveedores), ahora con
    auditoría before/after para poder revertirlos en ``undo_reread``.

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

    Como el motor de identidad decide QUÉ registro tocar recién adentro de
    ``apply_import`` (F7b), la forma más simple y segura de capturar el estado
    "antes" es un snapshot completo de todos los Customer/Supplier del tenant
    ANTES de llamar a ``_import_master_entities``, y comparar contra el estado
    después usando los ids que ahora expone (``clientes_creados_ids`` etc.).
    Un tenant tiene, en la práctica, de decenas a pocos miles de
    clientes/proveedores — aceptable para una operación rara y manual como la
    relectura.

    Devuelve ``(clientes_creados_o_actualizados, proveedores_creados_o_actualizados)``.
    """
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    # F-RR Fase 6: el borrador de la sesión gana si trae su propio mapeo de
    # maestros explícito — misma prioridad que el resto de las correcciones
    # (draft > lo guardado en el confirm original).
    draft_master = (draft or {}).get("master_column_mappings")
    original_master = (file.parsed_summary_json or {}).get("master_column_mappings")
    stored = (draft_master if draft_master else original_master) or {}
    context_mappings = stored.get("context") or None
    flat_mapping = stored.get("flat") or None
    if not context_mappings and not flat_mapping:
        return 0, 0

    # Snapshot COMPLETO antes de mutar — no sabemos qué registros va a tocar
    # el motor de identidad hasta que corre.
    before_customers = {
        c.id: _snapshot_master(c, "customer")
        for c in (
            await session.execute(select(Customer).where(Customer.tenant_id == tenant_id))
        )
        .scalars()
        .all()
    }
    before_suppliers = {
        s.id: _snapshot_master(s, "supplier")
        for s in (
            await session.execute(select(Supplier).where(Supplier.tenant_id == tenant_id))
        )
        .scalars()
        .all()
    }

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
    await session.flush()

    async def _audit(
        ids: list[str],
        before_map: dict[uuid.UUID, dict[str, Any]],
        kind: Literal["customer", "supplier"],
        model: type[Any],
        action: str,
    ) -> None:
        for raw_id in ids:
            entity_id = uuid.UUID(raw_id)
            entity = await session.get(model, entity_id)
            if entity is None:
                continue
            # ``updated_at`` tiene ``onupdate=func.now()`` (server-side) — tras el
            # flush de apply_import queda marcado expirado; un ``getattr`` directo
            # fuera de un ``await`` dispara un lazy-load síncrono que revienta
            # bajo AsyncSession (``MissingGreenlet``). Refrescar explícitamente.
            await session.refresh(entity)
            before = before_map.get(entity_id)  # None si fue CREADO ahora
            session.add(
                DataRepairItem(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    source_file_id=file.id,
                    action=action,
                    before_json=before,
                    after_json=_snapshot_master(entity, kind),
                    confidence="HIGH",
                )
            )

    # Dedup: si dos filas del MISMO archivo matchean la misma identidad (ej. DNI
    # repetido, o una fila posterior "corrige" la que ésta acaba de crear),
    # apply_import indexa el recién creado en su dedup de batch — la segunda fila
    # resuelve como "matched" y ese id termina en updated_ids AUNQUE ya esté en
    # created_ids de la primera fila. Sin este filtro, esa entidad generaría DOS
    # DataRepairItem: un REREAD_MASTER_UPDATE con before_json=None (mal etiquetado
    # — no hubo estado previo real) y un REREAD_MASTER_CREATE redundante con el
    # mismo after_json. El "antes" real de esa entidad relativo a TODO este run es
    # "no existía", así que se audita UNA sola vez como CREATE.
    clientes_creados_ids = counts.get("clientes_creados_ids", [])
    clientes_actualizados_ids = [
        i
        for i in counts.get("clientes_actualizados_ids", [])
        if i not in set(clientes_creados_ids)
    ]
    proveedores_creados_ids = counts.get("proveedores_creados_ids", [])
    proveedores_actualizados_ids = [
        i
        for i in counts.get("proveedores_actualizados_ids", [])
        if i not in set(proveedores_creados_ids)
    ]

    await _audit(
        clientes_actualizados_ids, before_customers, "customer", Customer,
        "REREAD_MASTER_UPDATE",
    )
    await _audit(
        clientes_creados_ids, before_customers, "customer", Customer,
        "REREAD_MASTER_CREATE",
    )
    await _audit(
        proveedores_actualizados_ids, before_suppliers, "supplier", Supplier,
        "REREAD_MASTER_UPDATE",
    )
    await _audit(
        proveedores_creados_ids, before_suppliers, "supplier", Supplier,
        "REREAD_MASTER_CREATE",
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
    #: F-O.1 — preservados por venir de "Otros", no por edición manual. Se cuentan
    #: aparte de ``preserved_count`` porque son dos motivos distintos: uno es "el
    #: usuario corrigió este registro", el otro "el usuario decidió qué ERA esta
    #: fila". Sumarlos haría que el informe de la relectura no pueda explicar por
    #: qué no tocó algo.
    preserved_from_others: int = 0
    #: F-O.2 — los registros en sí, no sólo cuántos: después del reimport hay que
    #: preguntarle a cada uno si la fila que representa volvió a entrar.
    others_records: list[SaleEntry | ExpenseEntry] = field(default_factory=list)


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
    preserved_from_others = 0
    others_records: list[SaleEntry | ExpenseEntry] = []
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
        if is_unclassified_row_ref(ref):
            # F-O.1 — el registro que nació de clasificar a mano una fila de
            # "Otros" NO se voidea.
            #
            # Medido antes de tocarlo: se voideaba como cualquier no-editado, y el
            # reimport no lo reponía —para el parser esa fila SIGUE sin poder
            # leerse, por eso había caído a "Otros"—; encima su
            # ``UnclassifiedRecord`` ya estaba en IMPORTED, así que tampoco volvía
            # a la bandeja. La venta desaparecía del sistema: se perdía el trabajo
            # del usuario Y el dato.
            #
            # Su ``source_row_ref`` es ``unclassified:{id}``, que no corresponde a
            # ninguna fila del archivo: el camino exacto de la reconciliación no
            # tiene con qué emparejarlo. Se preserva por la misma razón que una
            # fila editada — es una decisión humana sobre esa fila—, y también se
            # preserva su efecto sobre el stock (ver ``preserved_sale_events``).
            #
            # Límite declarado: si la relectura AHORA sí sabe leer esa fila, la
            # importa además, y quedan las dos. Cerrar eso necesita un vínculo
            # fila↔registro que hoy no se persiste — es F-O.2, y es lo que le
            # permitirá a la relectura MODIFICAR el registro en vez de convivir.
            preserved += 1
            preserved_from_others += 1
            others_records.append(rec)
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
        preserved_from_others=preserved_from_others,
        others_records=others_records,
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


async def _deduce_inventory_effect(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
) -> dict[str, str]:
    """F-F.4 — qué le hace al inventario cada hoja de lo que se acaba de releer.

    Arma los mismos ``SheetInventoryProfile`` que el confirm y los resuelve con la
    misma función: si la regla se reimplementara acá, una relectura descontaría
    con un criterio y el confirm con otro sobre el MISMO archivo.

    El mapeo sale de ``derive_context_mapping_entries`` —el que la relectura ya usa
    para el riesgo de columnas— porque el mapeo de transacciones no se persiste: la
    relectura re-importa por autodetección, así que la fuente de verdad sobre qué
    columna es qué es la misma derivación que gobierna esa importación.

    Falla blanda a ``{}``: quedarse sin efecto significa no descontar, que es el
    estado en el que la relectura vivió hasta F-F.4. Un error acá no puede tumbar
    una relectura que por lo demás está bien.
    """
    try:
        entries, entities = await derive_context_mapping_entries(session, tenant_id, summary)
    except Exception:  # noqa: BLE001 — ver el fail-soft del docstring
        logger.warning("reread.inventory_effect.derivacion_fallida", exc_info=True)
        return {}
    perfiles = [
        SheetInventoryProfile(
            context_id=context_id,
            entity=entities.get(context_id),
            # Sólo campos CANÓNICOS, igual que el confirm: un `custom_field:` guarda
            # el dato y el importador no lo lee como cantidad.
            mapped_fields=frozenset(
                e.target_field
                for e in items
                if parse_target(e.target_field).kind == "canonical"
            ),
        )
        for context_id, items in entries.items()
    ]
    return resolve_inventory_effects(perfiles)


async def _refs_de_filas_clasificadas(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    clasificados: list[SaleEntry | ExpenseEntry],
) -> dict[uuid.UUID, str]:
    """F-O.2 — ``{id del registro clasificado: source_row_ref de su fila}``.

    El vínculo lo guarda la captura en ``ROW_REF_KEY``: es el ``source_row_ref``
    que le habría tocado a la fila, o sea la clave con la que el reimport la
    insertaría. Una fila capturada por un camino que no tenía el ancla a mano no
    lo trae y queda fuera del dict — degrada a F-O.1 (se preserva), que no pierde
    nada.
    """
    if not clasificados:
        return {}
    por_registro: dict[uuid.UUID, uuid.UUID] = {}
    for rec in clasificados:
        try:
            por_registro[rec.id] = uuid.UUID(
                str(rec.source_row_ref).removeprefix(UNCLASSIFIED_ROW_REF_PREFIX)
            )
        except (ValueError, AttributeError):
            # Prefijo sin uuid detrás: no resuelve a ninguna fila. Se preserva.
            continue
    if not por_registro:
        return {}
    filas = (
        (
            await session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant_id,
                    UnclassifiedRecord.id.in_(list(por_registro.values())),
                )
            )
        )
        .scalars()
        .all()
    )
    ref_de_fila = {
        fila.id: str((fila.row_data or {}).get(_ROW_REF_KEY) or "") for fila in filas
    }
    return {
        rec_id: ref_de_fila.get(fila_id, "")
        for rec_id, fila_id in por_registro.items()
        if ref_de_fila.get(fila_id)
    }


async def _superseder_clasificados_de_otros(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    clasificados: list[SaleEntry | ExpenseEntry],
    refs_de_otros: dict[uuid.UUID, str],
    refs_reimportadas: set[str],
    run: DataRepairRun,
    dry_run: bool,
) -> int:
    """F-O.2 — la fila que la relectura YA sabe leer reemplaza a la clasificada.

    F-O.1 preserva el registro nacido de "Otros" porque el reimport no podía
    reponerlo. Cuando SÍ puede —el parser mejoró, o el archivo se corrigió—
    preservarlo dejaría dos: el que cargó el usuario y el que acaba de entrar.

    **Gana la relectura**, decisión explícita del usuario: el archivo es la fuente
    y lo que se lee reemplaza lo cargado a mano. Por eso se anula sin comparar
    campo por campo — comparar sólo tendría sentido si la clasificación pudiera
    ganar en algo, y no puede.

    Se anula el movimiento de inventario junto con la venta: el guard de
    preservación lo salvó del void general (V28) porque en ese momento la fila se
    conservaba, y ahora ya no.

    Las que NO fueron reemplazadas dejan su captura nueva en DISMISSED: la huella
    se liberó antes del reimport, así que una fila que sigue sin poder leerse
    volvió a "Otros" — y ofrecerle al usuario clasificar de nuevo algo que ya
    clasificó es ruido, no información.
    """
    if not clasificados:
        return 0

    reemplazados = 0
    ahora = datetime.now(UTC)
    refs_conservadas: set[str] = set()
    for rec in clasificados:
        ref = refs_de_otros.get(rec.id)
        if not ref:
            continue
        if ref not in refs_reimportadas:
            refs_conservadas.add(ref)
            continue
        snapshot = (
            _snapshot_sale(rec) if isinstance(rec, SaleEntry) else _snapshot_expense(rec)
        )
        rec.voided_at = ahora
        rec.void_reason = VOID_REASON_REREAD
        rec.voided_by_repair_run_id = run.id
        reemplazados += 1
        if isinstance(rec, SaleEntry):
            movimientos = (
                (
                    await session.execute(
                        select(InventoryMovement).where(
                            InventoryMovement.tenant_id == tenant_id,
                            InventoryMovement.source_upload_id == file_id,
                            InventoryMovement.source_event_id
                            == sale_source_event_id(rec.id),
                            InventoryMovement.voided_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for mov in movimientos:
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
        if not dry_run:
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=file_id,
                    sale_entry_id=rec.id if isinstance(rec, SaleEntry) else None,
                    action=ACTION_VOID,
                    before_json=snapshot,
                    after_json=None,
                    confidence="HIGH",
                )
            )

    if refs_conservadas:
        await _descartar_recapturas_ya_clasificadas(
            session, tenant_id, file_id, refs_conservadas, ahora
        )
    return reemplazados


async def _descartar_recapturas_ya_clasificadas(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    refs: set[str],
    ahora: datetime,
) -> None:
    """La fila que sigue sin poder leerse volvió a "Otros": se descarta la copia.

    DISMISSED y no borrado, mismo criterio que F8 con las filas de riesgo
    resueltas: queda el rastro de que la relectura la volvió a ver y no supo
    leerla. El registro que el usuario ya clasificó sigue vivo y es el que manda.
    """
    pendientes = (
        (
            await session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant_id,
                    UnclassifiedRecord.uploaded_file_id == file_id,
                    UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    for fila in pendientes:
        if str((fila.row_data or {}).get(_ROW_REF_KEY) or "") in refs:
            fila.status = UNCLASSIFIED_STATUS_DISMISSED
            fila.resolved_at = ahora


async def _reconcile(
    session: AsyncSession,
    file: UploadedFile,
    tenant_id: uuid.UUID,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    run: DataRepairRun,
    dry_run: bool = False,
    draft: dict[str, Any] | None = None,
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
    def _se_preserva(rec: SaleEntry | ExpenseEntry) -> bool:
        """Los DOS motivos por los que el reimport no toca un registro.

        Se pregunta una sola vez y desde acá, porque cada guard que lo re-derive
        por su cuenta puede quedarse con la mitad: fue exactamente lo que pasó con
        el movimiento de la venta editada (V28).
        """
        return bool(getattr(rec, "has_user_edits", False)) or is_unclassified_row_ref(
            rec.source_row_ref
        )

    preserved_refs: set[str] = {
        rec.source_row_ref for rec in all_existing if _se_preserva(rec) and rec.source_row_ref
    }
    # F-F.4 — la MISMA regla para el descuento de una venta preservada.
    #
    # El movimiento de descuento no lleva `source_row_ref` (lo identifica
    # `source_event_id = "sale:{id}"`), así que la regla de arriba no lo protegía:
    # se voideaba, y quien tenía que restituirlo era el replay posterior. Eso sólo
    # funciona si el filtro por hoja del replay alcanza a esa venta — y no la
    # alcanza, porque la venta preservada conserva el sello del import ANTERIOR y
    # la relectura deduce sus hojas de nuevo. Resultado: la venta editada a mano se
    # quedaba en los libros y sus unidades volvían al stock.
    #
    # Se protege igual que la fila: si el reimport no la toca, su efecto sobre el
    # stock tampoco se toca. La reversa deja de depender de que dos derivaciones
    # distintas coincidan.
    preserved_sale_events: set[str] = {
        sale_source_event_id(rec.id)
        for rec in all_existing
        if _se_preserva(rec) and isinstance(rec, SaleEntry)
    }

    # F-O.2 — liberar la huella de las filas que el usuario clasificó desde "Otros".
    #
    # Sin esto la relectura NUNCA puede volver a leerlas: la captura a "Otros" es
    # output persistido y registra su huella de idempotencia, así que el reimport
    # saltea esa fila para siempre y la pregunta "¿ya la sabés leer?" no llega a
    # hacerse. Es el mismo movimiento que F8 hace con las filas de riesgo
    # corregidas (``_reconcile_column_risk`` borra su huella para que entren en el
    # mismo reimport).
    #
    # Liberarla no importa nada por sí solo: si la fila sigue sin poder leerse,
    # vuelve a "Otros" y la captura re-registra la huella — y la de más abajo
    # descarta esa captura nueva, porque el usuario ya la resolvió.
    refs_de_otros = await _refs_de_filas_clasificadas(
        session, tenant_id, recon.others_records
    )
    fingerprints_to_delete |= {ref for ref in refs_de_otros.values() if ref}

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
    movimientos_preservados: set[uuid.UUID] = set()
    for mov in prev_movements_res.scalars().all():
        # No voidear el movimiento de una fila editada preservada: el reimport la saltea,
        # así que su stock debe quedar intacto (si no, se subestimaría). Las dos
        # señales, porque los movimientos se identifican de dos formas: la compra
        # por la fila que la trajo, el descuento de venta por su `source_event_id`.
        if mov.source_row_ref and mov.source_row_ref in preserved_refs:
            movimientos_preservados.add(mov.id)
            continue
        if mov.source_event_id and mov.source_event_id in preserved_sale_events:
            movimientos_preservados.add(mov.id)
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
    # F-RR Fase 6: el borrador de la sesión gana si el usuario lo cambió EN VIVO
    # durante esta relectura (misma prioridad que el resto de las correcciones).
    _draft_treatment = (draft or {}).get("stock_treatment")
    _stored_treatment = (
        _draft_treatment
        if _draft_treatment is not None
        else (file.parsed_summary_json or {}).get("stock_treatment")
    )
    # F-F.4: el efecto de inventario se DEDUCE de lo que esta relectura acaba de
    # leer, no del que resolvió el confirm original.
    #
    # Es la razón de ser de la relectura: puede detectar cantidades donde antes no
    # las veía, o ventas y gastos que la lectura anterior no había leído. Si el
    # efecto saliera del summary guardado, esas filas entrarían **sin mover
    # stock** —el dict viejo no las conoce— y la relectura habría importado una
    # venta de mercadería que no descuenta, que es justo lo que F-F.4 elimina.
    # Deducirlo de nuevo también es lo consistente con la fase: el efecto es
    # consecuencia del contenido, y acá el contenido se volvió a leer.
    #
    # Consecuencia declarada y elegida por el usuario: un archivo importado ANTES
    # de F-F.4 —cuyas ventas nunca descontaron— queda al día en cuanto se relee.
    _stored_effect = await _deduce_inventory_effect(session, tenant_id, fresh)
    _draft_context_mappings, _draft_context_entity = _draft_effective_mappings(draft)
    _reimport_detail = await insert_confirmed_data(
        session,
        tenant_id,
        fresh,
        confirmed_fields,
        source="reread",
        uploaded_file_id=file_id,
        stock_treatment=_stored_treatment,
        inventory_effect=_stored_effect,
        # Corrección C1 (revisión externa 2026-08-19): el mapeo que el usuario
        # corrigió EN VIVO en esta sesión tiene que llegar hasta el import
        # real — antes solo se usaba para las decisiones de riesgo y el
        # reimport seguía detectando columnas 100% por heurística. Prioridad
        # por (contexto, columna): draft explícito > heurística — cualquier
        # columna que el borrador NO toque sigue cayendo a la detección de
        # siempre (ver ``_val``/``_row_val`` en ``ingestion_import_service``).
        context_mappings=_draft_context_mappings,
        context_entity=_draft_context_entity,
        # Revisión final F9b (Hallazgo 2): en preview (dry_run=True) el detalle
        # nunca se consume (ver el bloque `if not dry_run` de abajo) — pedirlo
        # igual dispara N `session.get`/`refresh` en
        # `_stamp_updated_at_on_product_details` por cada producto tocado, solo
        # para descartar el resultado. Pedirlo condicionado a `not dry_run`
        # evita ese costo en el path síncrono de preview.
        return_details=not dry_run,
        # El undo compara este `updated_at` contra el vivo del producto.
        stamp_product_updated_at=not dry_run,
    )
    await session.flush()

    # F9b (Task 6): auditar productos creados/actualizados por el reimport —
    # antes ``product_details`` ni se pedía acá (default ``return_details=False``),
    # así que una relectura nunca dejaba rastro de qué producto tocó. En
    # ``dry_run`` (preview) NO se escribe (mismo criterio que los voids/inserts de
    # arriba: es descartable, no hace falta auditarla).
    if not dry_run:
        for _pd in _reimport_detail.get("product_details", []):
            session.add(
                DataRepairItem(
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=file_id,
                    product_id=uuid.UUID(_pd["product_id"]),
                    action="CREATE_PRODUCT" if _pd["action"] == "CREATED" else "UPDATE_PRODUCT",
                    before_json=_pd["before"],
                    after_json=_pd["after"],
                    confidence="HIGH",
                )
            )
        await session.flush()

    # F-F.4 — la relectura DESCUENTA, con el mismo núcleo que el confirm.
    #
    # Hasta acá la relectura re-importaba las ventas y no tocaba una unidad: el
    # void de más arriba había revertido los descuentos del import anterior
    # (`void_movement` sobre todo movimiento vivo del archivo, incluidos los del
    # replay) y nada los volvía a aplicar, así que releer un archivo BAJABA el
    # stock de golpe. Se llama a `run_inventory_replay` —el mismo que el confirm y
    # el panel— por la razón de siempre: lo que descuenta un camino y lo que
    # descuenta el otro tienen que ser la misma operación.
    #
    # **Va ANTES de auditar los movimientos nuevos, y no es un detalle de orden:**
    # el bloque de abajo es el que los deja revertibles por el undo. Corriendo
    # después, el descuento quedaría fuera del `DataRepairItem` y el undo dejaría
    # el stock descontado sin las ventas que lo justifican.
    _alcance_replay = replay_scope(_stored_effect)
    if _alcance_replay.corre:
        await session.flush()
        await run_inventory_replay(
            session,
            tenant_id,
            file_id,
            context_ids=_alcance_replay.context_ids,
            apply=not dry_run,
        )
        await session.flush()

    # Auditar los movimientos de inventario recién insertados por el reimport, para
    # poder revertirlos en el undo.
    #
    # "Vivo y de este archivo" ya NO alcanza como definición de "nuevo": desde que
    # hay movimientos que el void PRESERVA —el de una fila editada a mano (V28) y
    # el de una clasificada desde "Otros" (F-O.1)— quedan vivos sin que esta
    # relectura los haya creado. Auditarlos como inserción hacía que el undo los
    # anulara: devolvía un stock que la relectura nunca tocó, y encima de forma
    # irreversible desde el punto de vista del usuario (la venta seguía ahí, sin su
    # movimiento). Se excluyen explícitamente.
    if not dry_run:
        new_movements_res = await session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.source_upload_id == file_id,
                InventoryMovement.voided_at.is_(None),
            )
        )
        for mov in new_movements_res.scalars().all():
            if mov.id in movimientos_preservados:
                continue
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

    # F-O.2: la fila que el reimport SÍ pudo leer reemplaza a la que el usuario
    # había clasificado desde "Otros". Corre acá y no antes porque la pregunta es
    # "¿entró esta fila?", y eso recién se sabe con lo que el reimport insertó.
    _refs_reimportadas = {
        str((item.after_json or {}).get("source_row_ref") or "")
        for item in inserted_items
    }
    _reemplazados_de_otros = await _superseder_clasificados_de_otros(
        session,
        tenant_id,
        file_id,
        recon.others_records,
        refs_de_otros,
        _refs_reimportadas,
        run,
        dry_run,
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
        preserved=recon.preserved_count - _reemplazados_de_otros,
        preserved_from_others=recon.preserved_from_others - _reemplazados_de_otros,
        new=new_count,
        voided=voided + _reemplazados_de_otros,
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


async def estimate_unlinked_products(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    catalog: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> UnlinkedProductsEstimate:
    """Proyecta, SIN escribir nada, si cada venta/compra quedaría vinculada a
    un producto — usando la MISMA decisión determinística que el apply real
    (``ingestion_import_service``), no una copia que pueda divergir:

      - Ventas: ``_resolve_product`` (idéntico matcher usado en
        ``_venta_producto_id``/``_venta_producto_id_plana``).
      - Compras de mercadería: el MISMO gate (`nombre + cantidad>0` o
        categoría INVENTORY) que ``ingestion_import_service`` evalúa antes de
        siquiera intentar vincular — una fila que lo pierde no se cuenta como
        "sin producto" genérico, se distingue como ``compras_gate_bloqueado``
        (releer con el mismo mapeo NUNCA la arregla sola) de
        ``movimientos_sin_producto_esperado`` (un service/alquiler real).
      - Gate pasado: ``_resolve_link`` sobre ``ProductIdentityIndexes`` recién
        cargados (SELECT puro, sin mutar) — resuelto/ambiguo/a-crear.

    Deliberadamente NO reusa ``_resolve_purchase_identity``: esa función
    CREA productos incompletos en el caso "create" (vía
    ``build_incomplete_product``) — inaceptable en un preview, que nunca debe
    escribir datos de negocio. ``_resolve_link`` es su mitad pura.

    Aproximación conocida: no simula el orden fila-por-fila del import real
    (un producto que la fila 5 crearía y la fila 20 del MISMO archivo
    vincularía después aparece acá como dos "compras_producto_nuevo"
    separadas, no como alta+reposición) — replicar eso exactamente exigiría
    simular la corrida completa, lo que iría en contra del propio objetivo
    de "estimar sin escribir". Tampoco resuelve por barcode/marca (esas
    columnas no llegan resueltas a esta capa) — sku/nombre cubre el caso
    típico de un libro de compras.
    """
    by_sku, by_name, by_token = catalog
    result = UnlinkedProductsEstimate()

    for _ctx, _idx, row, kind in _iter_importable_fresh_rows(fresh, confirmed_fields):
        if kind != "sale":
            continue
        name = _row_val(row, _NOMBRE_COLS)
        sku = _row_val(row, _SKU_COLS)
        pid = _resolve_product(by_sku, by_name, name, sku, by_token)
        if pid is not None:
            result.ventas_con_producto += 1
        else:
            result.ventas_sin_producto += 1
            if len(result.ventas_sin_producto_samples) < _SAMPLE_PER_KIND:
                result.ventas_sin_producto_samples.append(
                    {"name": _clean_str(name, 120), "sku": _clean_str(sku, 60)}
                )

    is_stock = fresh.get("inferred_type") == "stock"
    if is_stock or not (confirmed_fields.get("gastos") or confirmed_fields.get("ventas")):
        return result

    vertical = await _load_tenant_vertical(session, tenant_id)
    indexes = await _load_product_identity_indexes(session, tenant_id)
    empty_cache: dict[str, Product] = {}

    purchase_rows: list[dict[str, Any]] = []
    for bucket_key in ("gastos_detectados", "otros_detectados"):
        purchase_rows += [r for r in (fresh.get(bucket_key) or []) if isinstance(r, dict)]

    for row in purchase_rows:
        clean_name = _clean_str(_row_val(row, _NOMBRE_COLS), 299)
        sku = _row_val(row, _SKU_COLS)
        has_qty = _parse_qty(_row_val(row, _CANTIDAD_COLS)) > 0
        # MISMO gate que ingestion_import_service._is_merch_purchase / el
        # `if expense.product_id is None and (...)` que lo envuelve.
        is_merch_purchase = bool(clean_name) and has_qty

        cat_code, _label, _ = classify_expense_with_vertical(
            _clean_str(_row_val_categoria(row)), vertical
        )

        if not (is_merch_purchase or (cat_code == "INVENTORY" and has_qty)):
            sample = {"name": clean_name, "categoria": cat_code}
            # `_NOMBRE_COLS` (heurística, sin la columna MAPEADA explícita que
            # sí tiene el import real) también matchea la descripción genérica
            # de un gasto real ("detalle"/"concepto") — "Alquiler local" parece
            # nombre de producto igual que "Yerba Mate 1kg". La categoría
            # clasificada desempata: si ya resolvió a un OPEX reconocido
            # (RENT, SERVICES, etc. — no "OTHER" ni "INVENTORY"), la fila SÍ
            # se identificó como lo que es y no necesita producto. Solo cuando
            # la categoría queda "OTHER" (no reconocida) Y hay nombre sin
            # cantidad es plausible que sea mercadería con la columna de
            # cantidad perdida — el bug real de ASTERIA.
            if clean_name and not has_qty and cat_code in ("OTHER", "INVENTORY"):
                result.compras_gate_bloqueado += 1
                if len(result.compras_gate_bloqueado_samples) < _SAMPLE_PER_KIND:
                    result.compras_gate_bloqueado_samples.append(sample)
            else:
                result.movimientos_sin_producto_esperado += 1
            continue

        resolution = _resolve_link(
            clean_name, sku, None, None, indexes=indexes, cache=empty_cache
        )
        if resolution.status == "resolved":
            result.compras_vinculadas += 1
        elif resolution.status in ("ambiguous", "conflict"):
            result.compras_sin_producto += 1
            if len(result.compras_sin_producto_samples) < _SAMPLE_PER_KIND:
                result.compras_sin_producto_samples.append(
                    {"name": clean_name, "sku": _clean_str(sku, 60)}
                )
        else:  # "create"
            result.compras_producto_nuevo += 1

    return result


async def _verify_unlinked_products_reconciliation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    projected: UnlinkedProductsEstimate,
) -> dict[str, Any] | None:
    """Post-apply (F-RR Fase 4): compara lo que ``estimate_unlinked_products``
    proyectó ANTES de aplicar contra lo que efectivamente quedó persistido —
    la invariante de reconciliación central de este módulo (ver docstring de
    ``UnlinkedProductsEstimate``: el resumen de ASTERIA decía "sin_producto: 0"
    mientras la base tenía cientos sin vincular).

    Alcance HONESTO — no las 5 categorías completas, las 2 que tienen un
    equivalente exacto y estable en lo persistido:

      - ``con_producto``: ``ventas_con_producto + compras_vinculadas +
        compras_producto_nuevo`` (proyectado) vs. filas vivas con
        ``product_id IS NOT NULL`` (real). Da igual si una fila proyectada
        como "nueva" terminó matcheando el catálogo recién creado por OTRA
        fila del mismo archivo — se suman antes de comparar, el total no
        depende de esa distinción interna.
      - ``ventas_sin_producto``: exacto en ambos lados.

    NO reconciliable 1:1 desde lo persistido: ``compras_gate_bloqueado`` vs
    ``movimientos_sin_producto_esperado`` colapsan al mismo estado final
    (OPEX, ``product_id`` NULL) — la distinción solo existe en el momento de
    la estimación, sobre la fila cruda. Y ``compras_sin_producto``
    (ambiguo/conflicto) no persiste como ``ExpenseEntry`` en absoluto — cae a
    "Otros"/``unclassified_records``, sin equivalente directo acá.

    Excluye filas con ``has_user_edits=True``: una edición manual puede (a
    propósito) tener un vínculo de producto distinto al que la estimación
    fresca hubiera calculado — eso es la edición ganando, no una divergencia.

    Nunca lanza: un fallo en la verificación misma no debe abortar un apply
    que ya escribió correctamente. Devuelve tres formas distinguibles —
    hallazgo de code review: antes un fallo del chequeo (timeout, error SQL)
    devolvía ``None``, lo mismo que "reconcilió sin desvío" — ocultando que
    la verificación ni siquiera corrió:

      - ``None``: se verificó y coincide, nada que avisar.
      - ``{"con_producto": {...}, ...}``: se verificó y DIVERGE.
      - ``{"check_failed": {"reason": ...}}``: la verificación misma falló —
        no se sabe si coincide o no, y eso se dice explícitamente.
    """
    try:
        sales_linked = (
            await session.scalar(
                select(func.count())
                .select_from(SaleEntry)
                .where(
                    SaleEntry.tenant_id == tenant_id,
                    SaleEntry.source_upload_id == file_id,
                    SaleEntry.voided_at.is_(None),
                    SaleEntry.has_user_edits.is_(False),
                    SaleEntry.product_id.is_not(None),
                )
            )
        ) or 0
        sales_unlinked = (
            await session.scalar(
                select(func.count())
                .select_from(SaleEntry)
                .where(
                    SaleEntry.tenant_id == tenant_id,
                    SaleEntry.source_upload_id == file_id,
                    SaleEntry.voided_at.is_(None),
                    SaleEntry.has_user_edits.is_(False),
                    SaleEntry.product_id.is_(None),
                )
            )
        ) or 0
        expenses_linked = (
            await session.scalar(
                select(func.count())
                .select_from(ExpenseEntry)
                .where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.source_upload_id == file_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.has_user_edits.is_(False),
                    ExpenseEntry.product_id.is_not(None),
                )
            )
        ) or 0

        expected_con_producto = (
            projected.ventas_con_producto
            + projected.compras_vinculadas
            + projected.compras_producto_nuevo
        )
        actual_con_producto = sales_linked + expenses_linked

        mismatches: dict[str, Any] = {}
        if expected_con_producto != actual_con_producto:
            mismatches["con_producto"] = {
                "esperado": expected_con_producto,
                "real": actual_con_producto,
            }
        if projected.ventas_sin_producto != sales_unlinked:
            mismatches["ventas_sin_producto"] = {
                "esperado": projected.ventas_sin_producto,
                "real": sales_unlinked,
            }
        if not mismatches:
            return None
        logger.error(
            "reread.reconciliation_mismatch",
            tenant_id=str(tenant_id),
            file_id=str(file_id),
            mismatches=mismatches,
        )
        return mismatches
    except Exception as exc:  # noqa: BLE001 — nunca aborta un apply ya escrito
        logger.error(
            "reread.reconciliation_check_failed", file_id=str(file_id), error=str(exc)
        )
        return {"check_failed": {"reason": "No se pudo verificar el resultado."}}


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
        preserved_from_others=recon.preserved_from_others,
        new=new_count,
        to_void=len(recon.non_edited),
        unchanged=unchanged_count,
        products_new=products_new,
        products_restock=products_restock,
        legacy_fallback=recon.legacy_fallback,
        sample_changes=void_samples + update_samples + new_samples + product_samples,
    )


# ── F8b (Task 5): decisiones de riesgo de columnas en la relectura ──────────────


def _load_risk_decisions(file: UploadedFile) -> list[ColumnRiskDecision]:
    """Decisiones de riesgo de columnas que el confirm (Task 4/5) persistió en
    ``parsed_summary_json`` — mismo patrón que ``master_column_mappings``.

    Vacío si el archivo se confirmó antes de F8b o sin decisiones: la relectura
    simplemente no reaplica nada (cobertura documentada, no un bug). Una decisión
    corrupta se ignora sin romper la relectura."""
    raw = (file.parsed_summary_json or {}).get("column_risk_decisions") or []
    decisions: list[ColumnRiskDecision] = []
    if not isinstance(raw, list):
        return decisions
    for d in raw:
        if isinstance(d, dict):
            try:
                decisions.append(ColumnRiskDecision(**d))
            except Exception:  # noqa: BLE001 — decisión ilegible: se saltea
                continue
    return decisions


def _apply_risk_decisions(
    file: UploadedFile, fresh: dict[str, Any]
) -> AppliedColumnRisk | None:
    """Reaplica las decisiones persistidas sobre el summary fresco (copia; el
    original no se muta). ``None`` si no hay decisiones. RECOMPUTA las filas
    afectadas con el criterio canónico (``_classify_cell`` vía
    ``apply_column_risk_decisions``) — NUNCA confía en un ``affected_rows``
    guardado (invariante 3)."""
    decisions = _load_risk_decisions(file)
    if not decisions:
        return None
    # ``context_entities={}``: la entidad efectiva se toma del summary por contexto
    # (fallback dentro de ``apply_column_risk_decisions``); el confirm ya validó las
    # decisiones, acá solo se reaplican.
    return apply_column_risk_decisions(fresh, decisions, {})


# ── F9a: sanitización de nombres de columna no confiables ─────────────────────
# Vienen de headers de archivos subidos por el usuario — nunca confiar en ellos
# antes de persistirlos en ``reread_summary`` o devolverlos al caller.

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SANITIZE_MAX_LEN = 200


def _sanitize_column_name(name: str) -> str:
    """Quita caracteres de control y trunca a ``_SANITIZE_MAX_LEN`` caracteres."""
    cleaned = _CONTROL_CHARS_RE.sub("", name)
    return cleaned[:_SANITIZE_MAX_LEN]


def build_reread_summary(
    outcome: str,
    *,
    algorithm_version: int = INGESTION_VERSION,
    ambiguous: list[dict[str, Any]] | None = None,
    forced_unverified: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape ÚNICO de ``UploadedFile.reread_summary`` — fix round post-review
    (hallazgo Important #3): antes de esto, ``apply_reread`` (acá) y
    ``record_bookkeeping`` (``scripts/reanalyze_ingestion.py``) escribían dos
    shapes incompatibles (``ambiguous_columns``/``forced_unverified_columns``
    sueltos vs. ``bucket``/``risk_columns`` plano), y ningún consumidor podía
    confiar en nada más allá de ``outcome``/``algorithm_version`` porque cambiaba
    según quién hubiera escrito por última vez.

    Ambos escritores llaman a ESTE helper — no pueden volver a divergir. Claves
    base (siempre presentes): ``outcome``, ``algorithm_version``, ``risk_columns``
    (``{"ambiguous": [...], "forced_unverified": [...]}``, anidado para preservar
    la distinción sin perder una key top-level común). ``extra`` agrega claves
    adicionales propias de cada escritor (ej. ``run_id`` acá, ``bucket``/
    ``has_user_edits``/``scanned_at``/``scanned_by`` en el script) sin pisar las
    base."""
    summary: dict[str, Any] = {
        "outcome": outcome,
        "algorithm_version": algorithm_version,
        "risk_columns": {
            "ambiguous": ambiguous or [],
            "forced_unverified": forced_unverified or [],
        },
    }
    if extra:
        summary.update(extra)
    return summary


def _sanitize_risk_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitiza ``source_column``/``target_field`` en una lista de filas de riesgo
    (ambiguas o forzadas) antes de exponerlas al caller o persistirlas. El resto
    de las claves (montos agregados, ratios, flags) no vienen de un header crudo
    y se preservan tal cual."""
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        for key in ("source_column", "target_field"):
            value = new_row.get(key)
            if isinstance(value, str):
                new_row[key] = _sanitize_column_name(value)
        sanitized.append(new_row)
    return sanitized


# ── F9a: outcome explícito de la resolución de riesgo de columnas ────────────


@dataclass
class ResolvedRisk:
    """Resultado de ``_resolve_risk_decisions`` — outcome explícito que reemplaza
    el booleano implícito (None/no-None) de ``_apply_risk_decisions``.

    Invariante de seguridad (no negociable, ver Global Constraints del plan): solo
    ``outcome == "REAPPLIED"`` puede traer ``applied`` no-``None`` — es el ÚNICO
    caso donde el mapeo reaplicado es el REAL que el usuario eligió en el confirm
    (F8b+). Para archivos confirmados ANTES de F8, cualquier mapeo que
    ``derive_context_mapping_entries`` derive es un GUESS sobre datos ya
    importados: ``NO_RISK_FOUND``, ``FORCED_UNVERIFIED`` y ``AMBIGUOUS`` NUNCA
    tocan el summary ni se auto-aplican, aunque una acción sea la única legal
    (``FORCED_UNVERIFIED``) — existen solo para el reporte."""

    outcome: RiskOutcome
    applied: AppliedColumnRisk | None = None
    ambiguous_rows: list[dict[str, Any]] = field(default_factory=list)
    forced_rows: list[dict[str, Any]] = field(default_factory=list)


def _draft_effective_mappings(
    draft: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, str]] | None, dict[str, str] | None]:
    """Corrección C1 (revisión externa 2026-08-19): traduce ``draft[
    "column_mappings"]``/``draft["context_entities"]`` — el mapeo que el
    usuario corrigió EN VIVO en la sesión de relectura, ver
    ``ingestion.py::reread_preview`` — al par ``(context_mappings,
    context_entity)`` que ``insert_confirmed_data`` acepta (mismo shape que
    usa el confirm inicial, ver ``api/v1/ingestion.py::confirm_ingestion``).

    Antes, este mapeo solo se usaba para resolver las decisiones de riesgo
    (``_apply_draft_risk_decisions``) — el reimport en sí seguía detectando
    columnas 100% por heurística sobre el contenido re-leído, así que
    corregir "Producto" o "Cantidad" en pantalla no cambiaba nada de lo
    efectivamente importado. Es seguro pasar un mapeo PARCIAL (solo las
    columnas que el usuario tocó): ``insert_confirmed_data`` resuelve
    columna-por-columna, con fallback a la heurística de siempre para
    cualquier campo que el borrador no mencione (ver ``_val``/``_row_val`` en
    ``ingestion_import_service``) — no hace falta que el borrador cubra el
    mapeo completo de todas las hojas para que esto sea correcto.

    Una columna DROPEADA por una decisión de riesgo (``drop_column``) no se
    incluye — ``apply_column_risk_decisions`` ya la sacó del summary que se
    va a reimportar, mapearla apuntaría a una columna que ya no está.

    Columnas sin ``context_id`` explícito caen al contexto ``"table"`` —
    misma convención que ``_dropped_pairs``/``reread_preview`` usan para el
    archivo de una sola hoja.

    ``(None, None)`` si el borrador no trae mapeo — el caller cae al criterio
    heurístico de siempre, sin cambios."""
    mappings = (draft or {}).get("column_mappings") or []
    if not mappings:
        return None, None
    dropped = {
        (d.get("context_id") or "table", d.get("source_column"))
        for d in (draft or {}).get("column_risk_decisions") or []
        if d.get("action") == "drop_column"
    }
    by_context: dict[str, dict[str, str]] = defaultdict(dict)
    for m in mappings:
        target = m.get("target_field")
        if not target or parse_target(target).kind in ("ignore", "none"):
            continue
        context_id = m.get("context_id") or "table"
        if (context_id, m.get("source_column")) in dropped:
            continue
        by_context[context_id][m["source_column"]] = target
    context_entity = dict((draft or {}).get("context_entities") or {}) or None
    return (dict(by_context) or None), context_entity


def _apply_draft_risk_decisions(
    draft: dict[str, Any], fresh: dict[str, Any]
) -> AppliedColumnRisk | None:
    """F-RR Fase 6: aplica el mapeo + decisiones de riesgo que el usuario armó EN
    VIVO durante esta sesión de relectura (persistidos en
    ``run.details_json["draft"]`` por el endpoint de preview al recibir
    correcciones — ver ``ingestion.py::reread_preview``).

    Mismo patrón que el confirm (``POST /confirm``): el mapeo efectivo por
    contexto sale DIRECTO de ``draft["column_mappings"]`` (lo que el usuario ya
    eligió, no un guess de ``derive_context_mapping_entries``, que es solo para
    SUGERIR), con ``context_entities`` ya resuelto (misma prioridad que
    ``_entity_for`` del confirm — se resolvió una vez al persistir el borrador,
    ver el endpoint). Las decisiones de riesgo ya se validaron
    (``validate_column_risk_decisions``) antes de persistirse — acá solo se
    aplican. ``None`` si el borrador no trae mapeo (nada que aplicar)."""
    if not draft.get("column_mappings"):
        return None
    raw_decisions = draft.get("column_risk_decisions") or []
    decisions = [ColumnRiskDecision(**d) for d in raw_decisions]
    context_entities = dict(draft.get("context_entities") or {})
    return apply_column_risk_decisions(fresh, decisions, context_entities)


def _uncovered_ambiguous_risk(
    risk_rows: list[dict[str, Any]], draft: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Corrección C2 (revisión externa 2026-08-19): de los ``risk_rows`` YA
    calculados sobre el mapeo efectivo del borrador (``build_reread_sheets``),
    separa las filas verdaderamente AMBIGUAS (2+ acciones legales,
    ``split_derivable_decisions``) y devuelve solo las que ``draft`` NO cubre
    con una ``column_risk_decisions`` explícita — clave ``(context_id,
    source_column, target_field)``, igual que valida ``validate_column_risk_
    decisions`` al persistir. Vacía si no hay ambigüedad sin cubrir."""
    _, ambiguous = split_derivable_decisions(risk_rows)
    if not ambiguous:
        return []
    decided = {
        (d.get("context_id"), d.get("source_column"), d.get("target_field"))
        for d in (draft or {}).get("column_risk_decisions") or []
    }
    return [
        row
        for row in ambiguous
        if (row["context_id"], row["source_column"], row["target_field"]) not in decided
    ]


async def _resolve_risk_decisions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file: UploadedFile,
    fresh: dict[str, Any],
    confirmed_fields: dict[str, bool],
    draft: dict[str, Any] | None = None,
) -> ResolvedRisk:
    """Resuelve el riesgo de columnas para la relectura, con outcome explícito.

    0. F-RR Fase 6: si la sesión tiene un borrador con mapeo explícito del
       usuario (revisión completa EN VIVO, no un guess), se aplica DIRECTO →
       ``USER_REVIEWED``. Prioridad más alta que todo lo demás: es la
       corrección MÁS RECIENTE que el usuario hizo, más autoritativa que lo
       guardado en el confirm original (que puede ser justamente el mapeo mal
       resuelto que motivó la relectura).
    1. Si el confirm original guardó ``column_risk_decisions`` (F8b+): se
       REAPLICAN tal cual (``_apply_risk_decisions``, comportamiento idéntico al
       histórico) → ``REAPPLIED``. Es el mapeo REAL que el usuario eligió.
    2. Si no hay decisiones guardadas (archivo pre-F8): se DERIVA un mapeo nuevo
       (``derive_context_mapping_entries``) para diagnosticar, nunca para aplicar
       — es un GUESS sobre datos ya importados. Sin contextos tabulares con
       headers válidos → ``NO_RISK_FOUND``.
    3. Con mapeo derivado: ``build_contextual_column_risk`` + ``split_derivable_
       decisions`` separan filas forzadas (una sola acción legal) de ambiguas
       (2+ acciones legales).
    4. Sin ninguna fila accionable → ``NO_RISK_FOUND``.
    5. Con al menos una fila AMBIGUA (con o sin forzadas junto) → ``AMBIGUOUS``,
       todo-o-nada: ninguna decisión (ni siquiera las forzadas) se aplica. Evita
       reconciliación parcial difícil de auditar.
    6. Solo forzadas, sin ambiguas → ``FORCED_UNVERIFIED``. A diferencia de
       ``REAPPLIED``, acá NUNCA se llama ``apply_column_risk_decisions`` — el
       mapeo es un guess no verificado (ver docstring de ``ResolvedRisk``); este
       outcome existe para el reporte, no para aplicar.
    """
    if draft is not None:
        draft_applied = _apply_draft_risk_decisions(draft, fresh)
        if draft_applied is not None:
            return ResolvedRisk(outcome="USER_REVIEWED", applied=draft_applied)

    applied = _apply_risk_decisions(file, fresh)
    if applied is not None:
        return ResolvedRisk(outcome="REAPPLIED", applied=applied)

    # F9a (hallazgo post-review): ``derive_context_mapping_entries`` es una
    # derivación best-effort (mismo criterio que su hermano en
    # ``get_file_preview``, api/v1/ingestion.py, que envuelve la MISMA llamada
    # en un try/except) — un fallo transitorio (ej. DB en
    # ``ColumnMappingService.suggest_mappings``) NO debe romper el reread
    # preview/apply con un 500 genérico. Degradamos al outcome más
    # conservador: ``NO_RISK_FOUND`` ya significa "no se pudo/no hizo falta
    # derivar nada" — no auto-aplica ni confía sin revisión humana.
    try:
        entries, entities = await derive_context_mapping_entries(session, tenant_id, fresh)
    except Exception:  # noqa: BLE001 — degradación best-effort, ver comentario arriba.
        logger.warning("reread.resolve_risk.derive_context_mapping_failed", file_id=str(file.id))
        return ResolvedRisk(outcome="NO_RISK_FOUND")
    if not entries:
        return ResolvedRisk(outcome="NO_RISK_FOUND")

    risk_rows = build_contextual_column_risk(
        fresh, entries, context_entities=entities, confirmed_fields=confirmed_fields
    )
    decisions, ambiguous = split_derivable_decisions(risk_rows)

    if not decisions and not ambiguous:
        return ResolvedRisk(outcome="NO_RISK_FOUND")

    forced_rows = _sanitize_risk_rows([d.model_dump() for d in decisions])
    if ambiguous:
        # Todo-o-nada: cualquier ambigüedad hace AMBIGUO al archivo entero, aunque
        # haya forzadas junto — no se aplican ni siquiera esas.
        return ResolvedRisk(
            outcome="AMBIGUOUS",
            ambiguous_rows=_sanitize_risk_rows(ambiguous),
            forced_rows=forced_rows,
        )
    # Solo forzadas: NO se aplican (ver docstring) — solo para el reporte.
    return ResolvedRisk(outcome="FORCED_UNVERIFIED", forced_rows=forced_rows)


def _parse_risk_ref(row_data: dict[str, Any] | None) -> tuple[str, int] | None:
    """``(context_id, row_index)`` desde la clave de correlación ``__risk_ref__``
    que la captura (Task 5) guardó en el ``UnclassifiedRecord``. ``None`` si el
    registro no proviene de una captura de riesgo o la clave es ilegible."""
    if not row_data:
        return None
    raw = row_data.get(_RISK_REF_KEY)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return str(obj["context_id"]), int(obj["row_index"])
    except Exception:  # noqa: BLE001
        return None


async def _reconcile_column_risk(
    session: AsyncSession,
    file: UploadedFile,
    tenant_id: uuid.UUID,
    applied: AppliedColumnRisk,
) -> None:
    """Honra las decisiones de riesgo en la relectura (dentro de la transacción del
    apply):

    1. Re-captura en "Otros" las filas TODAVÍA afectadas (recomputadas sobre el
       summary fresco). Idempotente por la huella ``risk:*`` de Task 3 — una fila
       ya capturada no se duplica.
    2. Resuelve el "Otros" de las filas CORREGIDAS: las que tenían captura previa
       (``__risk_ref__``) pero ya NO están afectadas se marcan DISMISSED (resueltas,
       alineado a ``VOID_REASON_REREAD``) y se borra su huella de riesgo. Así se
       importan normal en este mismo reimport (ya estaban en ``applied.summary``,
       que solo removió las que siguen mal) y una relectura futura vuelve a
       capturarlas si el problema reaparece.
    """
    file_id = file.id

    # (1) Re-capturar afectadas (dedup por huella risk:*). ``routed_rows`` ya viene
    # recomputado por ``apply_column_risk_decisions``.
    still_affected: set[tuple[str, int]] = set()
    for cid, rows_by_idx in applied.routed_rows.items():
        for idx in rows_by_idx:
            still_affected.add((cid, idx))
        if rows_by_idx:
            await _capture_column_risk_rows(
                session,
                tenant_id,
                file_id,
                cid,
                applied.routed_entity.get(cid) or "otros",
                rows_by_idx,
                source="reanalysis",
            )

    # (2) Resolver Otros de filas corregidas.
    pending_res = await session.execute(
        select(UnclassifiedRecord).where(
            UnclassifiedRecord.tenant_id == tenant_id,
            UnclassifiedRecord.uploaded_file_id == file_id,
            UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
        )
    )
    now = datetime.now(UTC)
    corrected_fps: set[str] = set()
    for rec in pending_res.scalars().all():
        ref = _parse_risk_ref(rec.row_data)
        if ref is None:
            continue  # no es una captura de riesgo correlacionada
        if ref in still_affected:
            continue  # sigue afectada → preservar sin duplicar
        rec.status = UNCLASSIFIED_STATUS_DISMISSED
        rec.resolved_at = now
        cid, idx = ref
        corrected_fps.add(_hash_anchor(_risk_row_anchor(tenant_id, file_id, cid, idx)))
    await _delete_fingerprints(session, tenant_id, corrected_fps)


# ── API pública ────────────────────────────────────────────────────────────────


# ── Sesión de relectura (F-RR): preview persistido + control de versión ──────
#
# Antes, cada llamada a `preview_reread`/`apply_reread` re-descargaba y
# re-parseaba el archivo de forma independiente, y el `apply` no tenía forma
# de saber si estaba aplicando la MISMA interpretación que el usuario vio en
# el preview. Esto materializa una "sesión" como un `DataRepairRun` que nace
# en PREVIEWING (descarga+parseo UNA sola vez, cacheados en `details_json`),
# pasa a READY_TO_APPLY cuando el preview terminó, y a RUNNING cuando el
# usuario confirma — el `run_id` (+ `draft_version`) es la referencia que ata
# el apply al preview exacto que se mostró.
#
# Alcance del guard de sesión ABIERTA (PREVIEWING/NEEDS_REVIEW/READY_TO_APPLY):
# POR ARCHIVO — dos usuarios revisando archivos distintos no se bloquean entre
# sí. Esto es DISTINTO del guard de `start_background_apply` de más abajo
# (POR TENANT, preexistente): revisar el mapeo de un archivo no debería
# competir con la revisión de otro, pero solo puede haber un apply corriendo
# de verdad por tenant a la vez (ese alcance no se tocó).

_SESSION_STATUSES_OPEN = ("PREVIEWING", "NEEDS_REVIEW", "READY_TO_APPLY")
# Una sesión de revisión legítima puede quedar abierta mucho más que el
# umbral de "colgado" del apply (15 min) mientras el usuario corrige mapeos a
# mano — por eso el umbral acá es mucho más generoso.
_PREVIEW_SESSION_STALE_AFTER_SECONDS = 60 * 60


def _age_seconds(moment: datetime | None, now: datetime) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds()


# Hallazgo de code review (F-RR): un run que termina FAILED/cancelado NO
# necesita conservar `fresh_summary` (el contenido crudo re-parseado del
# archivo — potencialmente miles de filas de datos de negocio). Sin esto,
# cada sesión abandonada/vencida/cancelada infla `data_repair_runs` para
# siempre con una copia completa del archivo que nunca se va a aplicar.
# `projected_impact` (agregados livianos, no filas) SÍ se conserva — lo
# necesita la reconciliación aunque el run haya fallado en el camino.
def _strip_bulky_fields(details: dict[str, Any]) -> dict[str, Any]:
    stripped = dict(details)
    stripped.pop("fresh_summary", None)
    return stripped


async def _expire_stale_preview_sessions(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> uuid.UUID | None:
    """Cierra (FAILED) sesiones de revisión de ESTE archivo abandonadas hace
    más de `_PREVIEW_SESSION_STALE_AFTER_SECONDS` sin avanzar. Usa
    `updated_at` (no `created_at`): una revisión activa que el usuario sigue
    corrigiendo no es huérfana aunque sea vieja.

    Devuelve el ``id`` de la última sesión cerrada (para lineage vía
    ``source_run_id`` en la sesión que la reemplace), o ``None`` si no cerró
    ninguna."""
    result = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(_SESSION_STATUSES_OPEN),
        )
    )
    now = datetime.now(UTC)
    last_expired: uuid.UUID | None = None
    for r in result.scalars().all():
        if (r.details_json or {}).get("file_id") != str(file_id):
            continue
        if _age_seconds(r.updated_at, now) < _PREVIEW_SESSION_STALE_AFTER_SECONDS:
            continue
        logger.error(
            "reread.session.expire_stale",
            run_id=str(r.id),
            tenant_id=str(tenant_id),
            status=r.status,
        )
        r.status = "FAILED"
        r.completed_at = now
        details = _strip_bulky_fields(r.details_json or {})
        details["reason"] = "stale_review_session"
        r.details_json = details
        last_expired = r.id
    return last_expired


async def _active_preview_session(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[DataRepairRun | None, uuid.UUID | None]:
    """Devuelve ``(sesión activa o None, id de la última expirada o None)`` —
    el segundo valor es lineage para la sesión NUEVA que se cree si no hay
    una activa."""
    expired = await _expire_stale_preview_sessions(session, file_id, tenant_id)
    result = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(_SESSION_STATUSES_OPEN),
        )
    )
    for r in result.scalars().all():
        if (r.details_json or {}).get("file_id") == str(file_id):
            return r, expired
    return None, expired


def _total_rows_in_summary(fresh: dict[str, Any]) -> int:
    """Total de filas del archivo, sumado por hoja/contexto.

    Fase 10 (progreso con contexto, revisión externa 2026-08-20): no toca el
    motor de import — solo le da al indicador de "Aplicando…" un total
    conocido de antemano (en vez de una barra indeterminada sin ningún dato),
    calculado UNA vez al crear la sesión, igual que el resto del summary."""
    return sum(
        int(ctx.get("row_count") or len(ctx.get("preview_rows") or []))
        for ctx in resolve_contexts(fresh)
    )


async def start_or_resume_preview_session(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
) -> tuple[DataRepairRun, dict[str, Any]]:
    """Punto de entrada de "Volver a leer": reusa la sesión de revisión ya
    abierta para este archivo si hay una viva (sin volver a tocar S3), o crea
    una nueva descargando + parseando + tomando metadata de S3 UNA sola vez.

    Devuelve ``(run, fresh)`` — ``fresh`` es SIEMPRE una copia propia del
    summary cacheado, segura de mutar sin afectar lo guardado en el run."""
    existing, expired = await _active_preview_session(session, file_id, tenant_id)
    if existing is not None:
        cached = (existing.details_json or {}).get("fresh_summary")
        if cached is not None:
            return existing, deepcopy(cached)
        # Sesión sin summary cacheado (dato legado/corrupto) — se recrea abajo
        # en vez de fallar; no debería ocurrir por este camino.

    # A punto de CREAR: lock por (tenant, archivo) — sin esto, dos "Volver a
    # leer" casi simultáneos del MISMO archivo (doble click, dos pestañas)
    # pasan el chequeo de arriba en paralelo (ninguno ve la sesión del otro
    # todavía) y crean dos runs PREVIEWING, cada uno pagando su propia
    # descarga+parseo. Namespace PROPIO — a propósito por archivo, no por
    # tenant como el guard de apply (dos archivos no deben bloquearse entre
    # sí acá).
    await _acquire_preview_session_lock(session, tenant_id, file_id)
    # Re-chequear YA con el lock tomado: si el otro request ganó la carrera
    # mientras este esperaba, reusar lo que creó en vez de duplicar.
    existing, expired_after_lock = await _active_preview_session(session, file_id, tenant_id)
    expired = expired_after_lock or expired
    if existing is not None:
        cached = (existing.details_json or {}).get("fresh_summary")
        if cached is not None:
            return existing, deepcopy(cached)

    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = await _fresh_summary(file, s3)
    snapshot = await s3.head(file.s3_key)

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_REREAD,
        status="PREVIEWING",
        dry_run=True,
        # Lineage (F-RR Fase 5): si esta sesión reemplaza una abandonada por
        # `_expire_stale_preview_sessions`, queda trazable de cuál viene —
        # "un reintento no es un evento sin historia".
        source_run_id=expired,
        details_json={
            "file_id": str(file_id),
            "fresh_summary": fresh,
            "s3_snapshot": snapshot,
            "draft_version": 0,
            "total_rows": _total_rows_in_summary(fresh),
        },
    )
    session.add(run)
    await session.flush()
    return run, deepcopy(fresh)


def mark_session_ready_to_apply(
    run: DataRepairRun, *, column_risk_outcome: str | None = None
) -> None:
    """Re-evalúa el estado de la sesión tras un preview (inicial o recomputado
    con un borrador nuevo).

    F-RR Fase 6: ``AMBIGUOUS`` (riesgo de columnas sin resolver — el usuario
    tiene decisiones pendientes) deja la sesión en ``NEEDS_REVIEW`` en vez de
    ``READY_TO_APPLY``: ``apply`` exige ``READY_TO_APPLY`` (ver
    ``validate_ready_to_apply``), así que un borrador ambiguo queda bloqueado
    hasta que el usuario lo resuelva. Se re-evalúa SIEMPRE (no solo al crear la
    sesión): un borrador que resuelve la ambigüedad debe poder pasar a
    READY_TO_APPLY, y uno que la introduce debe poder volver a NEEDS_REVIEW,
    aunque la sesión ya hubiera estado READY_TO_APPLY con un borrador anterior.
    No toca sesiones que ya salieron del ciclo de revisión (QUEUED en
    adelante)."""
    if run.status not in _SESSION_STATUSES_OPEN:
        return
    run.status = "NEEDS_REVIEW" if column_risk_outcome == "AMBIGUOUS" else "READY_TO_APPLY"


class StaleDraftVersionError(ValueError):
    """El ``draft_version`` que mandó el cliente no coincide con el actual del
    run — hay correcciones más recientes sin ver (ej. otra pestaña, o el
    usuario refrescó y el estado del cliente quedó desactualizado)."""


class FileChangedSincePreviewError(ValueError):
    """El archivo en S3 cambió (etag/size/last_modified) desde que se generó
    el preview — aplicar ahora sería aplicar una interpretación distinta a la
    que el usuario vio."""


async def validate_ready_to_apply(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    draft_version: int,
    s3: S3Client | None = None,
) -> DataRepairRun:
    """Valida que ``run_id`` sea una sesión READY_TO_APPLY de ``file_id``, con
    el ``draft_version`` esperado y el archivo sin cambios en S3 desde el
    preview. Lanza ``ValueError``/subclases — el caller HTTP las traduce a 404
    (no encontrado) o 409 (conflicto). No muta nada; el caller decide qué
    hacer con el run validado."""
    run = await session.get(DataRepairRun, run_id)
    if (
        run is None
        or run.tenant_id != tenant_id
        or run.repair_type != REPAIR_TYPE_REREAD
        or (run.details_json or {}).get("file_id") != str(file_id)
    ):
        raise FileNotFoundError(run_id)
    if run.status != "READY_TO_APPLY":
        raise ValueError(
            f"La sesión de relectura no está lista para aplicarse (estado actual: "
            f"{run.status})."
        )
    current_version = (run.details_json or {}).get("draft_version", 0)
    if current_version != draft_version:
        raise StaleDraftVersionError(
            "Hay cambios más recientes sin aplicar en esta revisión — "
            "actualizala antes de continuar."
        )

    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh_snapshot = await s3.head(file.s3_key)
    saved_snapshot = (run.details_json or {}).get("s3_snapshot") or {}
    if any(fresh_snapshot.get(k) != saved_snapshot.get(k) for k in ("etag", "size")):
        raise FileChangedSincePreviewError(
            "El archivo cambió desde que se generó esta revisión — generá un "
            "preview nuevo."
        )
    return run


async def cancel_preview_session(
    session: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID, file_id: uuid.UUID
) -> DataRepairRun:
    """Abandona una sesión de revisión abierta (PREVIEWING/NEEDS_REVIEW/
    READY_TO_APPLY) sin esperar el timeout de `stale` — libera el archivo para
    una relectura nueva de inmediato."""
    run = await session.get(DataRepairRun, run_id)
    if (
        run is None
        or run.tenant_id != tenant_id
        or run.repair_type != REPAIR_TYPE_REREAD
        or (run.details_json or {}).get("file_id") != str(file_id)
        or run.status not in _SESSION_STATUSES_OPEN
    ):
        raise ValueError("No hay ninguna sesión de revisión abierta con ese id.")
    run.status = "FAILED"
    run.completed_at = datetime.now(UTC)
    details = _strip_bulky_fields(run.details_json or {})
    details["reason"] = "cancelled_by_user"
    run.details_json = details
    return run


# Las 5 entidades del protocolo de riesgo/mapeo contextual (F8a) — mismo set
# que ``derive_context_mapping_entries`` filtra internamente (no exportado).
_SHEET_RISK_ENTITIES = frozenset({"sale", "expense", "product", "customer", "supplier"})


async def build_reread_sheets(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    fresh: dict[str, Any],
    draft: dict[str, Any] | None,
    confirmed_fields: dict[str, bool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """F-RR Fase 8 (backend): estado por hoja/contexto para la revisión completa
    del preview de relectura — mismo patrón que ``GET /files/{id}/preview``
    (F8a): ``derive_context_mapping_entries`` + ``build_contextual_column_risk``.
    Best-effort: un fallo acá nunca debe tumbar el preview completo (igual
    criterio que ese endpoint).

    Si el borrador de la sesión trae ``column_mappings``/``context_entities``
    explícitos (el usuario ya está corrigiendo), se usan como override — la
    pantalla debe reflejar SU corrección, no solo la sugerencia automática.

    Devuelve ``(sheets, contextual_risk)`` — ambos listos para serializar en
    ``RereadPreviewResponse`` (``RereadSheetStatus``/``ContextualColumnRisk``)."""
    draft = draft or {}
    raw_mappings = draft.get("column_mappings") or []
    user_mappings = [ColumnMapping(**m) for m in raw_mappings] if raw_mappings else None
    context_entity_override = cast("dict[str, str] | None", draft.get("context_entities") or None)
    context_confirmed = cast("dict[str, bool]", draft.get("context_confirmed") or {})

    try:
        entries, entities = await derive_context_mapping_entries(
            session,
            tenant_id,
            fresh,
            user_mappings=user_mappings,
            context_entity=context_entity_override,
        )
        risk_rows = build_contextual_column_risk(
            fresh,
            entries,
            context_entities=entities,
            confirmed_fields=confirmed_fields,
            context_confirmed=context_confirmed,
        )
    except Exception:  # noqa: BLE001 — best-effort, ver docstring.
        logger.warning("reread.preview.sheets_failed", tenant_id=str(tenant_id))
        return [], []

    risk_by_context: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in risk_rows:
        risk_by_context[row["context_id"]].append(row)

    sheets: list[dict[str, Any]] = []
    for ctx in resolve_contexts(fresh):
        context_id = ctx.get("context_id")
        if not context_id:
            continue
        label = str(ctx.get("label") or context_id).strip()
        headers = ctx.get("headers") or []
        entity = entities.get(context_id) or ctx.get("entity_type")
        row_count = int(ctx.get("row_count") or len(ctx.get("preview_rows") or []))

        # Sin headers (texto/imagen) o entidad fuera del protocolo de riesgo
        # (F8a no tiene nada que decir de estos): no hay columnas que revisar.
        if not headers or entity not in _SHEET_RISK_ENTITIES:
            sheets.append(
                {
                    "context_id": context_id,
                    "label": label,
                    "entity_type": entity or "otros",
                    "row_count": row_count,
                    "status": "completa",
                    "columns_mapped": 0,
                    "columns_pending": 0,
                    "is_summary_or_derived": False,
                }
            )
            continue

        context_entries = entries.get(context_id, [])
        mapped_targets = {
            e.target_field
            for e in context_entries
            if parse_target(e.target_field).kind == "canonical"
        }
        columns_pending = max(len(headers) - len(context_entries), 0)

        if not context_is_included(context_id, entity, confirmed_fields, context_confirmed):
            status_value = "ignorada"
        else:
            context_risk = risk_by_context.get(context_id, [])
            missing = missing_required_fields(entity, mapped_targets)
            ambiguous = any(len(r.get("allowed_actions") or []) >= 2 for r in context_risk)
            if ambiguous:
                status_value = "ambigua"
            elif missing or context_risk:
                status_value = "requiere_revision"
            else:
                status_value = "completa"

        sheets.append(
            {
                "context_id": context_id,
                "label": label,
                "entity_type": entity,
                "row_count": row_count,
                "status": status_value,
                "columns_mapped": len(mapped_targets),
                "columns_pending": columns_pending,
                "is_summary_or_derived": False,
            }
        )

    return sheets, risk_rows


async def preview_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
    fresh_override: dict[str, Any] | None = None,
    run: DataRepairRun | None = None,
) -> RereadPreview:
    """Preview RÁPIDO de la relectura: re-descarga + re-parsea el archivo y estima
    los cambios en memoria, **sin escribir en la DB** (sub-segundo incluso en
    archivos grandes). ``to_void``/``preserved`` exactos; ``new``/``to_update``
    estimados — el apply (``apply_reread``) es la fuente de verdad exacta. Devuelve
    un sample antes/después real para ver qué va a cambiar antes de aplicar.

    ``fresh_override``: si viene, se usa TAL CUAL en vez de re-descargar/parsear
    (F-RR: sesión de relectura — el summary fresco ya se descargó una vez al
    crear la sesión y quedó cacheado en ``DataRepairRun.details_json``; sin esto
    cada corrección del usuario durante la revisión pagaría S3+parseo de nuevo).
    El caller es responsable de pasar una copia si el mismo dict cacheado se va
    a reusar en llamadas posteriores — esta función no lo muta en el lugar.

    ``run``: si viene, persiste ``preview.unlinked_products`` en
    ``run.details_json["projected_impact"]`` — hallazgo de code review: la
    reconciliación post-apply debe comparar contra lo que el usuario REALMENTE
    VIO en este preview, no contra un recálculo hecho después de escribir (que
    ya vería el catálogo modificado por el propio apply y podría esconder una
    divergencia real). Sin ``run``, el preview sigue siendo puramente de
    lectura, como siempre."""
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = fresh_override if fresh_override is not None else await _fresh_summary(file, s3)
    # F-RR Fase 6: borrador de correcciones EN VIVO de esta sesión, si el
    # usuario ya mandó alguna (ver ``ingestion.py::reread_preview``).
    draft = (run.details_json or {}).get("draft") if run is not None else None
    confirmed_fields = _confirmed_fields_for(file, fresh, draft)
    # F9a: resuelve el riesgo de columnas con outcome explícito — REAPPLIED/
    # USER_REVIEWED son los ÚNICOS que mutan ``fresh`` para que el estimado
    # refleje el drop/route (filas ruteadas salen de los buckets → no se
    # cuentan como nuevas). Los demás outcomes (mapeo derivado/guess sobre
    # archivos pre-F8) NO tocan el summary — ver invariante en ``ResolvedRisk``.
    resolved = await _resolve_risk_decisions(
        session, tenant_id, file, fresh, confirmed_fields, draft
    )
    if resolved.applied is not None:
        fresh = resolved.applied.summary
    sales, expenses = await _load_existing_records(session, file_id, tenant_id)
    # Huellas de import (lo que el apply usa para deduplicar) + catálogo de productos
    # (para estimar altas/reposiciones). Dos queries, en memoria.
    fingerprints = await _load_import_fingerprints(session, tenant_id)
    catalog = await _load_product_index(session, tenant_id)
    preview = _estimate_reread(
        file, tenant_id, fresh, confirmed_fields, sales, expenses, fingerprints, catalog
    )
    preview.column_risk_outcome = resolved.outcome
    preview.column_risk_ambiguous = resolved.ambiguous_rows
    preview.column_risk_forced_unverified = resolved.forced_rows
    preview.unlinked_products = await estimate_unlinked_products(
        session, tenant_id, fresh, confirmed_fields, catalog
    )
    # F-RR Fase 8 (backend): revisión completa — hojas/mapeo/riesgo, sobre el
    # MISMO `fresh` que ya refleja drop/route (si `resolved.applied` mutó el
    # summary arriba) — la pantalla no puede mostrar columnas que el propio
    # preview ya descartó.
    preview.sheets, preview.contextual_column_risk = await build_reread_sheets(
        session, tenant_id, fresh, draft, confirmed_fields
    )
    # Corrección C2 (revisión externa 2026-08-19): ``USER_REVIEWED`` arriba solo
    # verificó que EL BORRADOR trajera mapeo — no que el mapeo resultante deje
    # SIN riesgo ambiguo sin resolver en otras columnas/hojas. Recalculamos acá
    # con el mapeo efectivo completo (lo que ``build_reread_sheets`` acaba de
    # derivar) y downgradeamos a ``AMBIGUOUS`` si queda algo sin decisión — el
    # mismo criterio "todo-o-nada" que ya rige el camino sin borrador.
    if preview.column_risk_outcome == "USER_REVIEWED":
        uncovered = _uncovered_ambiguous_risk(preview.contextual_column_risk, draft)
        if uncovered:
            preview.column_risk_outcome = "AMBIGUOUS"
            preview.column_risk_ambiguous = _sanitize_risk_rows(uncovered)
    preview.mapping_contexts = list(fresh.get("mapping_contexts") or [])
    if run is not None:
        details = dict(run.details_json or {})
        details["projected_impact"] = asdict(preview.unlinked_products)
        run.details_json = details
    return preview


async def apply_reread(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    s3: S3Client | None = None,
    run: DataRepairRun | None = None,
    origin: Literal["interactive", "batch_auto", "batch_manual"] = "interactive",
    fresh_override: dict[str, Any] | None = None,
) -> RereadApplyResult:
    """Aplica la relectura: void no-editados + reimport corregido, auditado y
    reversible. El commit lo hace el caller (get_db_session o el worker).

    Si se pasa ``run`` (creado por ``start_background_apply`` y ejecutado por el
    worker), se reusa; si no, se crea uno (camino síncrono / tests).

    ``origin`` distingue quién disparó el reread — el servicio no puede inferirlo
    por sí solo: ``"interactive"`` (default, humano vía UI/endpoint HTTP),
    ``"batch_auto"`` (batch sin supervisión, Task 4) o ``"batch_manual"`` (batch
    con revisión humana previa). Solo afecta el ``reread_status`` cuando el
    outcome de riesgo es ``REAPPLIED`` o ``USER_REVIEWED`` (ver stamping más
    abajo).

    ``fresh_override``: ver docstring de ``preview_reread`` — evita volver a
    descargar/parsear S3 cuando el ``run`` ya trae el summary de su sesión de
    preview cacheado."""
    # F3-T3: la relectura crea/void productos+stock. Shared lock ANTES de mutar.
    # No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)

    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)
    s3 = s3 or S3Client()
    fresh = fresh_override if fresh_override is not None else await _fresh_summary(file, s3)
    # F-RR Fase 6: borrador de correcciones EN VIVO de la sesión de preview que
    # originó este apply — capturado ANTES de que ``run`` se reasigne más abajo
    # (camino directo/legado sin sesión previa, donde no hay borrador posible).
    draft = (run.details_json or {}).get("draft") if run is not None else None
    confirmed_fields = _confirmed_fields_for(file, fresh, draft)

    # F9a: resuelve el riesgo de columnas con outcome explícito. Solo REAPPLIED/
    # USER_REVIEWED mutan el summary usado para reimportar — honra drop/route:
    # una fila corregida vuelve al bucket y se importa, una que sigue mal queda
    # fuera. Los demás outcomes (mapeo derivado/guess sobre un archivo pre-F8) NO
    # tocan el summary — invariante de seguridad, ver ``ResolvedRisk``.
    resolved = await _resolve_risk_decisions(
        session, tenant_id, file, fresh, confirmed_fields, draft
    )
    summary_for_import = (
        resolved.applied.summary if resolved.applied is not None else fresh
    )

    if run is None:
        # Camino directo/legado (sin pasar por start_background_apply — ej.
        # scripts, tests): arranca en APPLYING directamente, ya que acá se
        # está ejecutando de verdad. Mismo vocabulario de estados que el
        # camino en background (QUEUED→APPLYING→APPLIED/FAILED), aunque este
        # camino nunca pasa por QUEUED.
        run = DataRepairRun(
            tenant_id=tenant_id,
            repair_type=REPAIR_TYPE_REREAD,
            status="APPLYING",
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
        session, tenant_id, file, summary_for_import, confirmed_fields, run.id, draft
    )

    result = await _reconcile(
        session, file, tenant_id, summary_for_import, confirmed_fields, run, draft=draft
    )
    result.clientes = clientes_count
    result.proveedores = proveedores_count
    result.column_risk_outcome = resolved.outcome
    result.column_risk_ambiguous = resolved.ambiguous_rows
    result.column_risk_forced_unverified = resolved.forced_rows

    # F8b (Task 5): tras el reimport, honrar las decisiones de riesgo — re-capturar
    # afectadas (dedup) y resolver el "Otros" de las filas corregidas. Dentro de la
    # transacción del apply (atómico con el resto de la relectura). Solo ocurre en
    # REAPPLIED (único outcome con ``applied`` no-``None``).
    if resolved.applied is not None:
        await _reconcile_column_risk(session, file, tenant_id, resolved.applied)

    # F-RR (Fase 4): reconciliación post-apply — compara lo que el preview
    # proyectó (guardado en `run.details_json["projected_impact"]` cuando el
    # usuario lo VIO, en `preview_reread`) contra lo que efectivamente quedó
    # persistido. Hallazgo de code review: recomputar la proyección ACÁ, con
    # el catálogo YA modificado por este mismo apply, compara "un recálculo
    # contra sí mismo" y puede esconder una divergencia real (ej. un producto
    # duplicado creado por un bug de resolución ya aparece como match válido
    # al recalcular después). Por eso se lee la copia INMUTABLE que quedó
    # guardada al momento del preview, nunca se recalcula acá.
    #
    # `session.flush()` explícito ANTES de la verificación: la sesión de
    # producción corre con `autoflush=False` (ver `app/persistence/db/session.py`
    # — ya mordió antes, ver el bug histórico de `session.get()` no viendo
    # productos recién creados) y las queries de conteo no verían las filas
    # que `_reconcile` recién escribió sin este flush.
    await session.flush()
    saved_projection = (run.details_json or {}).get("projected_impact")
    if saved_projection is None:
        # Camino directo/legado (sin sesión de preview previa — ej. scripts,
        # tests que llaman apply_reread a mano): no hay "lo que el usuario
        # vio" para comparar. No se inventa un recompute post-apply acá —
        # sería exactamente el problema que este diseño evita.
        result.reconciliation_warning = {
            "check_skipped": {
                "reason": "sin proyección de preview guardada en este run "
                "(no pasó por una sesión de preview)."
            }
        }
    else:
        try:
            projected = UnlinkedProductsEstimate(**saved_projection)
        except TypeError as exc:
            logger.error(
                "reread.reconciliation_check_failed",
                file_id=str(file_id),
                error=f"projected_impact con forma inesperada: {exc}",
            )
            result.reconciliation_warning = {
                "check_failed": {"reason": "No se pudo verificar el resultado."}
            }
        else:
            result.reconciliation_warning = await _verify_unlinked_products_reconciliation(
                session, tenant_id, file_id, projected
            )

    # F9a: stamping de versionado/estado de la relectura sobre el archivo.
    # REAPPLIED/USER_REVIEWED son los ÚNICOS outcomes que bumpean
    # ``ingestion_version`` — los únicos casos donde el mapeo aplicado es el REAL
    # (F8b+ reaplicado, o la corrección explícita del usuario en esta sesión —
    # F-RR Fase 6, todavía más autoritativa), no un guess re-derivado.
    #
    # Fix round post-review (hallazgo Important #2): guardamos el valor PREVIO
    # de ``ingestion_version`` en ``run.details_json`` ANTES de pisarlo — sin
    # esto, ``undo_reread`` no tenía forma de restaurarlo y un archivo
    # revertido quedaba con la versión bumpeada para siempre, excluido de
    # ``select_candidate_files`` (filtra por ``ingestion_version < to_version``)
    # aunque sus datos hubieran vuelto al estado pre-reread.
    previous_ingestion_version = file.ingestion_version
    if resolved.outcome in ("REAPPLIED", "USER_REVIEWED"):
        file.ingestion_version = INGESTION_VERSION
        file.reread_status = (
            REREAD_STATUS_AUTO_APPLIED if origin == "batch_auto" else REREAD_STATUS_APPLIED
        )
    else:
        file.reread_status = REREAD_STATUS_NEEDS_REVIEW
    file.reread_at = datetime.now(UTC)
    # Fix round post-review (hallazgo Important #3): shape único vía
    # ``build_reread_summary`` — ver su docstring (antes divergía de lo que
    # escribe ``scripts/reanalyze_ingestion.py::record_bookkeeping``).
    file.reread_summary = build_reread_summary(
        resolved.outcome,
        ambiguous=resolved.ambiguous_rows,
        forced_unverified=resolved.forced_rows,
        extra={"run_id": str(run.id)},
    )

    run.status = "APPLIED"
    run.completed_at = datetime.now(UTC)
    run.sales_detected = result.to_update + result.new
    run.sales_voided = result.voided
    run.details_json = {
        "file_id": str(file_id),
        "previous_ingestion_version": previous_ingestion_version,
        "to_update": result.to_update,
        "preserved": result.preserved,
        "new": result.new,
        "voided": result.voided,
        "inserted": result.inserted,
        "legacy_fallback": result.legacy_fallback,
        "clientes": result.clientes,
        "proveedores": result.proveedores,
        "column_risk_outcome": result.column_risk_outcome,
        # F-RR Fase 4: None si reconcilia; si no, el detalle del desvío —
        # nunca se descarta en silencio (ver invariante del módulo).
        "reconciliation_warning": result.reconciliation_warning,
        # Sample para el diff antes/después en el frontend (limitado para no inflar).
        "sample_changes": list(result.items)[:24],
        "products_limitation": (
            "Products no se vinculan por source_upload_id; insert_confirmed_data "
            "los re-deriva idempotentemente (upsert por SKU/nombre)."
        ),
    }
    await session.flush()

    _trigger_score(session, tenant_id)
    return result


# ── Apply en background (Celery) ───────────────────────────────────────────────

# Una relectura RUNNING más vieja que esto se considera colgada (worker caído) y
# NO bloquea una nueva — evita que un run zombie trabe el archivo para siempre.
_STALE_RUNNING_AFTER_SECONDS = 15 * 60


# Namespace propio (2 claves int4) para no acoplar este guard con el advisory
# lock de mantenimiento general (maintenance_lock_service._advisory_key usa la
# forma de 1 clave bigint — Postgres mantiene ambos espacios separados, nunca
# colisionan aunque el valor numérico coincida).
_REREAD_GUARD_LOCK_NAMESPACE = 0x52524447  # "RRDG" en hex, arbitrario y estable


async def _acquire_reread_guard_lock(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Advisory lock transaccional que serializa el guard anti-duplicado de
    ``start_background_apply`` para el mismo tenant. Se libera solo al
    commit/rollback de la transacción actual del caller (la sesión de la
    request, que commitea después de ``start_background_apply``). No-op en
    SQLite — mismo criterio que ``maintenance_lock_service``."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(tenant_id.bytes[:4], "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _REREAD_GUARD_LOCK_NAMESPACE, "key": key},
    )


# Namespace PROPIO, distinto de `_REREAD_GUARD_LOCK_NAMESPACE` (que es a
# propósito tenant-wide, para el apply). Este es por (tenant, archivo) — dos
# archivos del mismo tenant no deben bloquearse entre sí al crear su sesión
# de preview.
_PREVIEW_SESSION_LOCK_NAMESPACE = 0x52524453  # "RRDS" (reread draft session)


async def _acquire_preview_session_lock(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID
) -> None:
    """Advisory lock por (tenant, archivo) que serializa la CREACIÓN de una
    sesión de preview — evita que dos "Volver a leer" casi simultáneos del
    mismo archivo creen dos ``DataRepairRun`` PREVIEWING en paralelo, cada
    uno pagando su propia descarga+parseo. No-op en SQLite."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    key1 = int.from_bytes(tenant_id.bytes[:4], "big", signed=True)
    key2 = int.from_bytes(file_id.bytes[:4], "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _PREVIEW_SESSION_LOCK_NAMESPACE, "key": key1 ^ key2},
    )


async def start_background_apply(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    existing_run: DataRepairRun | None = None,
) -> DataRepairRun:
    """Deja un ``DataRepairRun`` en status QUEUED (esperando que un worker lo
    reclame) para un apply en background y lo devuelve. Guard anti-duplicado:
    si ya hay una relectura QUEUED/APPLYING reciente del tenant (alcance POR
    TENANT, preexistente — no confundir con el guard de sesión de preview,
    que es por archivo), levanta ``ValueError`` (el caller responde 409). El
    caller commitea y encola la task. Evita el ciclo timeout→reintento→duplicados.

    ``existing_run``: si viene (F-RR — la sesión READY_TO_APPLY que
    ``validate_ready_to_apply`` ya validó), se REUSA ese mismo run — conserva
    su ``run_id`` y su ``fresh_summary`` cacheado (así el worker no vuelve a
    descargar/parsear) — en vez de crear uno nuevo desde cero (camino legado,
    sin sesión de preview previa).

    Nota de diseño (code review): dejar este método en QUEUED, NO en RUNNING,
    es lo que le permite al worker (``reread_worker.py``) RECLAMAR el run con
    un ``UPDATE ... WHERE status='QUEUED'`` atómico antes de ejecutar nada —
    sin esa transición real de estado, dos entregas del mismo mensaje de
    Celery (reentrega por red, no solo por crash) podían leer el mismo run
    "en curso" y aplicar la relectura dos veces."""
    await _acquire_reread_guard_lock(session, tenant_id)

    existing = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.tenant_id == tenant_id,
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(("QUEUED", "APPLYING")),
        )
    )
    now = datetime.now(UTC)
    last_expired_run_id: uuid.UUID | None = None
    for r in existing.scalars().all():
        # `updated_at`, no `created_at` — mismo fix que `sweep_stale_reread_runs`
        # (code review): `created_at` cuenta el tiempo de revisión previo a
        # encolarse, así que un run recién puesto en QUEUED podía nacer ya
        # "vencido" si el usuario tardó revisando el borrador.
        age = _age_seconds(r.updated_at, now)
        if age < _STALE_RUNNING_AFTER_SECONDS:
            raise ValueError(
                "Ya hay una relectura en curso. Esperá a que termine antes de "
                "aplicar otra."
            )
        # Huérfano: nunca avanzó de QUEUED/APPLYING en el tiempo esperado.
        # Antes esto solo lo IGNORABA para no bloquear una relectura nueva —
        # el run zombie quedaba así para siempre en la auditoría (caso real:
        # ASTERIA, dos runs sin que ningún worker los tomara). Ahora se cierra.
        reason = "stale_never_picked_up" if r.status == "QUEUED" else "stale_timeout"
        logger.error(
            "reread.guard.expire_stale_run",
            run_id=str(r.id),
            tenant_id=str(tenant_id),
            age_seconds=age,
            status=r.status,
            reason=reason,
        )
        r.status = "FAILED"
        r.completed_at = now
        details = _strip_bulky_fields(r.details_json or {})
        details["reason"] = reason
        r.details_json = details
        last_expired_run_id = r.id

    # Validar que el archivo exista/pertenezca antes de encolar.
    file = await _load_file(session, file_id, tenant_id)
    if file is None:
        raise FileNotFoundError(file_id)

    if existing_run is not None:
        # UPDATE atómico condicionado por status, no una mutación ORM directa:
        # dos requests de apply concurrentes con el MISMO run_id (doble click,
        # o dos pestañas) pasan `validate_ready_to_apply` cada uno con su
        # propia copia en memoria del run, ya leída ANTES de este punto — sin
        # el WHERE de acá, ambos pisarían `existing_run.status = "QUEUED"`
        # sin chequear el estado real en DB, reabriendo exactamente el
        # doble-apply que este guard existe para evitar. El advisory lock de
        # arriba serializa el SCAN de otros runs, no esta transición puntual.
        result = await session.execute(
            update(DataRepairRun)
            .where(
                DataRepairRun.id == existing_run.id,
                DataRepairRun.status == "READY_TO_APPLY",
            )
            # Fase 10 (progreso con contexto): `updated_at` explícito, mismo
            # motivo que el reclamo QUEUED→APPLYING del worker — este es
            # también un statement Core, que no dispara el `onupdate`
            # Python-side. Sin esto, "empezado hace..." arrancaría contando
            # desde el último toque de la sesión de PREVIEW, no desde que
            # entró en cola.
            .values(status="QUEUED", dry_run=False, updated_at=datetime.now(UTC))
        )
        if cast("CursorResult[Any]", result).rowcount == 0:
            raise ValueError(
                "Esta sesión de relectura ya no está lista para aplicarse — "
                "alguien más ya la aplicó o canceló. Generá un preview nuevo."
            )
        await session.refresh(existing_run)
        return existing_run

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_REREAD,
        status="QUEUED",
        dry_run=False,
        # Lineage (F-RR Fase 5): si este run reemplaza uno huérfano que el
        # scan de arriba acaba de cerrar, queda trazable de cuál viene.
        source_run_id=last_expired_run_id,
        details_json={"file_id": str(file_id)},
    )
    session.add(run)
    await session.flush()
    return run


async def sweep_stale_reread_runs(session: AsyncSession) -> dict[str, int]:
    """F-RR (Fase 5): housekeeping GLOBAL, independiente de que alguien
    reintente. Los guards reactivos de arriba (``start_background_apply``,
    ``_expire_stale_preview_sessions``) solo cierran un run colgado cuando
    alguien vuelve a tocar ESE archivo/tenant — si nadie reintenta nunca, un
    run zombie queda RUNNING/PREVIEWING para siempre en la auditoría sin que
    nada lo note. Pensado para correr desde un Celery beat periódico (ver
    ``jobs/reread_sweep_worker.py``), sobre TODOS los tenants.

    MISMOS umbrales que los guards reactivos (``_STALE_RUNNING_AFTER_SECONDS``,
    ``_PREVIEW_SESSION_STALE_AFTER_SECONDS``) — es la misma noción de "colgado",
    solo que evaluada proactivamente en vez de al vuelo de un request. El
    caller (el worker) hace el commit.

    Devuelve cuántos runs cerró por categoría, para logueo/métricas."""
    now = datetime.now(UTC)
    closed = {"apply_stuck": 0, "preview_session_abandoned": 0}

    running = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(("QUEUED", "APPLYING")),
        )
    )
    for r in running.scalars().all():
        # `updated_at`, no `created_at`: `created_at` es el momento en que se
        # creó la sesión de PREVIEWING, que puede llevar minutos de revisión
        # del usuario ANTES de encolarse — usarlo acá vencía un apply recién
        # encolado que todavía no tuvo tiempo de correr (hallazgo de code
        # review). `updated_at` se toca en la transición real a QUEUED/APPLYING.
        if _age_seconds(r.updated_at, now) < _STALE_RUNNING_AFTER_SECONDS:
            continue
        reason = "stale_never_picked_up" if r.status == "QUEUED" else "stale_timeout"
        logger.error(
            "reread.sweep.expire_apply",
            run_id=str(r.id),
            tenant_id=str(r.tenant_id),
            status=r.status,
            reason=reason,
        )
        r.status = "FAILED"
        r.completed_at = now
        details = _strip_bulky_fields(r.details_json or {})
        details["reason"] = reason
        r.details_json = details
        closed["apply_stuck"] += 1

    sessions_result = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.repair_type == REPAIR_TYPE_REREAD,
            DataRepairRun.status.in_(_SESSION_STATUSES_OPEN),
        )
    )
    for r in sessions_result.scalars().all():
        if _age_seconds(r.updated_at, now) < _PREVIEW_SESSION_STALE_AFTER_SECONDS:
            continue
        logger.error(
            "reread.sweep.expire_preview_session",
            run_id=str(r.id),
            tenant_id=str(r.tenant_id),
            status=r.status,
        )
        r.status = "FAILED"
        r.completed_at = now
        details = _strip_bulky_fields(r.details_json or {})
        details["reason"] = "stale_review_session"
        r.details_json = details
        closed["preview_session_abandoned"] += 1

    return closed


async def get_reread_run(
    session: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID, file_id: uuid.UUID
) -> DataRepairRun | None:
    """Devuelve el run de relectura (para el polling de estado), validando
    tenant Y que el run pertenezca al ``file_id`` pedido — evita que un
    ``run_id`` válido de OTRO archivo del mismo tenant devuelva un
    ``file_id`` que no le corresponde (el endpoint antes lo tomaba de la URL
    sin chequear)."""
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.tenant_id != tenant_id or run.repair_type != REPAIR_TYPE_REREAD:
        return None
    stored_file_id = (run.details_json or {}).get("file_id")
    if stored_file_id != str(file_id):
        return None
    return run


async def _undo_master_and_product_items(
    session: AsyncSession, tenant_id: uuid.UUID, items: list[DataRepairItem]
) -> list[dict[str, str]]:
    """F9b (Task 7): revierte los ``DataRepairItem`` de maestros (clientes/
    proveedores) y productos de un run de relectura — restaura los que nadie
    tocó después de la relectura, saltea (y reporta) los editados después.
    Nunca pisa una edición manual en silencio (política touched-since decidida
    por el usuario: comparar el ``updated_at`` ACTUAL contra el capturado en
    ``after_json`` en el momento exacto en que la relectura dejó el registro).

    Para los CREADOS por la relectura y no tocados después: se desactivan
    (``deactivated_at``, mismo criterio que el borrado protegido existente) en
    vez de restaurar campos — no había "antes" al que volver. Producto además
    usa ``deactivation_reason="REREAD_UNDO"`` (Task 4) y ``is_active=False``
    (mismo patrón que ``product_dedup_service``) — NUNCA hard delete, rompería
    el ``ON DELETE SET NULL`` de ventas/gastos que ya referencian ese producto.

    ``stock_units`` de producto NUNCA se toca acá — su reversa es
    EXCLUSIVAMENTE el mecanismo incremental de movimientos de inventario (Paso
    4 de ``undo_reread``, ``void_movement``/``unvoid_movement``), nunca un
    ``setattr`` desde este snapshot. ``unit_cost_ars`` es la excepción: a
    diferencia de ``stock_units``, el mecanismo de movimientos NUNCA lo ajusta
    (``void_movement``/``unvoid_movement`` solo tocan stock/``current_qty``),
    así que SÍ se restaura acá por ``setattr`` como cualquier otro campo
    mutable no-stock (revisión final F9b, Hallazgo 1) — si no, el undo dejaría
    el costo unitario permanentemente en lo que dijo el archivo releído.

    Productos: a diferencia de maestros (Task 5 ya dedupea a lo sumo un item
    por entidad por run), Task 6 NO dedupea — dos filas del mismo archivo que
    tocan el mismo producto dejan DOS ``DataRepairItem`` (``CREATE_PRODUCT``/
    ``UPDATE_PRODUCT``) para el mismo ``product_id``. Acá se separan DOS
    preguntas distintas para cada ``product_id``: (1) ¿cuáles son los valores
    de before/after a usar? — el item MÁS RECIENTE (por ``created_at`` —
    ``items`` llega ordenado por el caller); (2) ¿el producto fue CREADO por
    ESTE run? — ``True`` si CUALQUIER item de ese ``product_id`` en este run
    es ``CREATE_PRODUCT``, sin importar cuál sea el más reciente. Un catálogo
    con dos filas del mismo producto (fila 1 lo crea, fila 2 lo actualiza —
    mecanismo intencional de Task 6) deja ``[CREATE_PRODUCT, UPDATE_PRODUCT]``
    para el mismo id: si (2) dependiera del item más reciente (el UPDATE),
    un producto genuinamente nuevo de este run iría por la rama de restore de
    campos en vez de desactivarse — quedaría ACTIVO tras el undo, cuando no
    existía antes de la relectura."""
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    _model_by_kind: dict[str, type[Any]] = {
        "customer": Customer,
        "supplier": Supplier,
        "product": Product,
    }
    # Allowlist compartida con el borrado de archivo (`_ledger_restore`): nunca
    # stock_units, sí los tres precios — ver el docstring de ese módulo.

    # Maestros: Task 5 ya dedupea (a lo sumo 1 item por entidad por run).
    to_process: list[tuple[str, uuid.UUID, bool, DataRepairItem]] = []
    for it in items:
        if it.action not in ("REREAD_MASTER_UPDATE", "REREAD_MASTER_CREATE"):
            continue
        after = it.after_json or {}
        kind = after.get("kind")
        raw_id = after.get("id")
        if kind not in ("customer", "supplier") or not raw_id:
            continue
        to_process.append((kind, uuid.UUID(raw_id), it.action == "REREAD_MASTER_CREATE", it))

    # Productos: agrupar TODOS los items por product_id — "más reciente" (para
    # before/after) y "fue creado en este run" (para elegir la rama) son
    # preguntas independientes, ver docstring.
    product_items_by_id: dict[uuid.UUID, list[DataRepairItem]] = {}
    for it in items:
        if it.action not in ("CREATE_PRODUCT", "UPDATE_PRODUCT") or it.product_id is None:
            continue
        product_items_by_id.setdefault(it.product_id, []).append(it)
    for product_id, pitems in product_items_by_id.items():
        latest = pitems[-1]  # items ya ordenados por created_at por el caller
        was_created_this_run = any(i.action == "CREATE_PRODUCT" for i in pitems)
        to_process.append(("product", product_id, was_created_this_run, latest))

    not_reverted: list[dict[str, str]] = []
    for kind, entity_id, is_create, it in to_process:
        entity = await session.get(_model_by_kind[kind], entity_id)
        if entity is None or entity.tenant_id != tenant_id:
            continue

        # ``updated_at`` tiene ``onupdate=func.now()`` server-side — puede quedar
        # expirado tras flushes previos de esta misma transacción (mismo patrón
        # MissingGreenlet que Task 5/6, ver ``_reread_master_entities._audit`` /
        # ``_stamp_updated_at_on_product_details``). Refrescar antes de leerlo.
        await session.refresh(entity)
        if entity_changed_since_ledger(entity, it.after_json):
            not_reverted.append(
                {"kind": kind, "id": str(entity_id), "reason": "edited_after_reread"}
            )
            continue

        if is_create:
            entity.deactivated_at = datetime.now(UTC)
            if kind == "product":
                entity.is_active = False
                entity.deactivation_reason = "REREAD_UNDO"
        else:
            restore_from_before(entity, kind, it.before_json or {})

    return not_reverted


async def undo_reread(
    session: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    background: BackgroundTasks | None = None,
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

    # Orden determinístico por ``created_at``: ``_undo_master_and_product_items``
    # asume que, para un mismo ``product_id`` con varios ``DataRepairItem`` (Task
    # 6 no dedupea productos), el ÚLTIMO de la lista es el más reciente. Los items
    # de un mismo producto se crean en un loop sin ``await`` entre medio antes de
    # un solo flush — pueden empatar en microsegundos (SQLite/CI, no descartado en
    # Postgres). ``DataRepairItem.id`` como desempate secundario NO vuelve el
    # orden "semánticamente correcto" ante un empate exacto (es un UUID
    # arbitrario), pero sí lo vuelve DETERMINÍSTICO — misma respuesta en cada
    # corrida, sin agregar una columna nueva (eso sería scope creep de esta task).
    items_res = await session.execute(
        select(DataRepairItem)
        .where(DataRepairItem.run_id == run_id)
        .order_by(DataRepairItem.created_at, DataRepairItem.id)
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

    # Fix round post-review (hallazgo Important #2): revertir el stamping de
    # versionado que ``apply_reread`` hizo sobre el archivo. Sin esto, deshacer
    # una relectura REAPPLIED dejaba el archivo "diciendo" ``ingestion_version``
    # bumpeado + ``reread_status`` APPLIED/AUTO_APPLIED aunque sus datos
    # volvieron al estado previo — quedaba excluido PARA SIEMPRE de
    # ``select_candidate_files`` (filtra por ``ingestion_version < to_version``)
    # pese a seguir necesitando revisión. ``previous_ingestion_version`` fue
    # guardado en ``run.details_json`` por ``apply_reread`` antes de bumpear.
    # ``reread_status`` SIEMPRE vuelve a NEEDS_REVIEW tras un undo (nunca debe
    # quedar APPLIED/AUTO_APPLIED, sin importar el outcome original).
    details = run.details_json or {}
    undo_file_id = details.get("file_id")
    if undo_file_id:
        undone_file = await session.get(UploadedFile, uuid.UUID(undo_file_id))
        if undone_file is not None and undone_file.tenant_id == tenant_id:
            previous_ingestion_version = details.get("previous_ingestion_version")
            if previous_ingestion_version is not None:
                undone_file.ingestion_version = previous_ingestion_version
            undone_file.reread_status = REREAD_STATUS_NEEDS_REVIEW
            undone_file.reread_at = datetime.now(UTC)

    # 5. Maestros (clientes/proveedores) + productos: restaurar los no tocados
    # después de la relectura, saltear (y reportar) los editados después.
    not_reverted_entities = await _undo_master_and_product_items(session, tenant_id, items)

    run.status = "REVERTED"
    run.completed_at = datetime.now(UTC)
    await session.flush()

    _trigger_score(session, tenant_id, background)
    return {
        "run_id": str(run_id),
        "restored": restored,
        "removed": removed,
        "status": "REVERTED",
        "not_reverted_entities": not_reverted_entities,
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


def _trigger_score(
    session: AsyncSession, tenant_id: uuid.UUID, background: BackgroundTasks | None = None
) -> None:
    """Encola el recálculo de score DESPUÉS del commit del caller.

    Ni el apply ni el undo comitean (lo hace el caller: el worker de Celery, el
    script del batch o la dependency del request). Encolar en el flush, como se
    hacía antes, dejaba al worker de score —que abre su propia sesión— leyendo un
    estado que todavía no existía; si además esa transacción hacía rollback, el
    score se recalculaba por una relectura que nunca ocurrió.

    ``background`` solo llega desde el request (el undo). El apply corre en un
    worker, donde no hay respuesta que proteger.
    """
    from app.application.services.score_trigger_service import (  # noqa: PLC0415
        trigger_score_recalculation_after_commit,
    )

    trigger_score_recalculation_after_commit(
        session, str(tenant_id), "reread_file", background=background
    )


# Re-export para tests
__all__ = [
    "ACTION_INSERT",
    "ACTION_VOID",
    "REPAIR_TYPE_REREAD",
    "VOID_REASON_REREAD",
    "FileChangedSincePreviewError",
    "ResolvedRisk",
    "RereadApplyResult",
    "RereadPreview",
    "StaleDraftVersionError",
    "apply_reread",
    "build_reread_sheets",
    "build_reread_summary",
    "cancel_preview_session",
    "file_has_user_edits",
    "latest_applied_run_for_file",
    "load_reread_run_summary",
    "mark_session_ready_to_apply",
    "preview_reread",
    "start_or_resume_preview_session",
    "sweep_stale_reread_runs",
    "undo_reread",
    "validate_ready_to_apply",
]
