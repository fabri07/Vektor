"""Servicio de reparación auditada para importaciones mal clasificadas.

Detecta SaleEntry creadas a partir de CSVs de productos clasificados erróneamente
como ventas, reconstruye los Product desde el archivo original y anula (soft delete)
las ventas incorrectas. Cada ejecución genera un DataRepairRun + DataRepairItems para
trazabilidad completa.

Dry-run persistente: crea el run + items pero NO modifica SaleEntry ni Product.
Apply: modifica + persiste run con status COMPLETED.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import infer_spreadsheet_type
from app.application.services.ingestion_import_service import (
    _find_col,
    _NOMBRE_COLS,
    _PRECIO_VENTA_COLS,
    insert_confirmed_data,
)
from app.observability.logger import get_logger
from app.persistence.models.file import UploadedFile
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry

logger = get_logger(__name__)

REPAIR_TYPE_MISCLASSIFIED = "MISCLASSIFIED_PRODUCT_IMPORT"
VOID_REASON = "REPAIR_MISCLASSIFIED_IMPORT"

# Ventana temporal para asociar SaleEntry con UploadedFile:
# se usa created_at de SaleEntry vs created_at de UploadedFile (más fiable que transaction_date)
_WINDOW_DAYS = 2


def normalize_product_name(name: str) -> str:
    """Normaliza para comparación: minúsculas, espacios normalizados, sin guiones."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", name.strip().lower()))


@dataclass
class RepairCandidate:
    tenant_id: uuid.UUID
    uploaded_file: UploadedFile
    suspicious_sales: list[SaleEntry]
    product_rows: list[dict[str, Any]]
    price_set: set[Decimal]
    confidence: str


@dataclass
class RepairResult:
    run_id: uuid.UUID
    dry_run: bool
    candidates_found: int
    sales_detected: int
    sales_voided: int
    products_detected: int
    products_created: int
    products_updated: int
    products_skipped: int
    tenant_results: list[dict[str, Any]] = field(default_factory=list)


def _re_evaluate_summary(summary: dict[str, Any]) -> str | None:
    """Re-evalúa el parsed_summary_json con la lógica actual de clasificación.

    Retorna inferred_type actual o None si no hay datos.
    """
    if not summary:
        return None
    has_fecha = bool(summary.get("has_fecha"))
    has_venta = bool(summary.get("has_venta"))
    has_gasto = bool(summary.get("has_gasto"))
    has_producto = bool(summary.get("has_producto"))
    has_precio_ambiguo = bool(
        any(
            col in ("precio", "total", "price", "valor")
            for col in [h.lower().strip().replace(" ", "_") for h in summary.get("columns", [])]
        )
    )
    # Detectar señales fuertes/débiles desde headers guardados
    from app.application.services.file_parsing import CATALOGO_COLS, NOMBRE_COLS  # noqa: PLC0415
    headers_norm = [h.lower().strip().replace(" ", "_") for h in summary.get("columns", [])]
    has_catalogo_fuerte = any(any(k in col for k in CATALOGO_COLS) for col in headers_norm)
    has_nombre = any(any(k in col for k in NOMBRE_COLS) for col in headers_norm)

    return infer_spreadsheet_type(
        has_fecha=has_fecha,
        has_venta=has_venta,
        has_gasto=has_gasto,
        has_producto=has_producto,
        has_precio_ambiguo=has_precio_ambiguo,
        has_catalogo_fuerte=has_catalogo_fuerte,
        has_nombre=has_nombre,
    )


def _extract_product_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrae las filas de producto del parsed_summary_json."""
    rows = (
        summary.get("stock_detectado")
        or summary.get("preview_rows")
        or summary.get("ventas_detectadas")
        or []
    )
    return [r for r in rows if isinstance(r, dict)]


def _extract_price_set(rows: list[dict[str, Any]]) -> set[Decimal]:
    """Extrae el conjunto de precios detectados en las filas para matching con SaleEntry."""
    from app.application.services.ingestion_import_service import _parse_amount  # noqa: PLC0415
    prices: set[Decimal] = set()
    if not rows:
        return prices
    headers = list(rows[0].keys())
    precio_col = _find_col(headers, _PRECIO_VENTA_COLS)
    if not precio_col:
        return prices
    for row in rows:
        p = _parse_amount(row.get(precio_col))
        if p is not None:
            prices.add(p)
    return prices


async def detect_misclassified_product_imports(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
) -> list[RepairCandidate]:
    """Detecta archivos mal clasificados y las SaleEntry incorrectas asociadas.

    Criterios de archivo sospechoso:
    - parsed_summary_json no vacío
    - processing_status IN (DONE, NEEDS_CONFIRMATION)
    - Re-evaluado con lógica actual → inferred_type == 'stock'
    - El original fue importado como ventas (ventas_detectadas no vacías O inferred_type original fue ventas)

    Criterios de SaleEntry sospechosa:
    - notes == 'Importado desde archivo'
    - payment_method == 'cash'
    - provenance == 'REAL'
    - product_id IS NULL
    - voided_at IS NULL
    - quantity == 1
    - created_at dentro de ±WINDOW_DAYS del uploaded_file.created_at
    - amount está en el conjunto de precios detectados (si hay precios)
    """
    file_q = select(UploadedFile).where(
        UploadedFile.parsed_summary_json.isnot(None),
        UploadedFile.processing_status.in_(["DONE", "NEEDS_CONFIRMATION"]),
    )
    if tenant_id is not None:
        file_q = file_q.where(UploadedFile.tenant_id == tenant_id)

    files_result = await db.execute(file_q)
    all_files: list[UploadedFile] = list(files_result.scalars().all())

    candidates: list[RepairCandidate] = []

    for uploaded_file in all_files:
        summary = uploaded_file.parsed_summary_json or {}
        current_type = _re_evaluate_summary(summary)

        if current_type != "stock":
            continue

        # Solo importaciones que originalmente se guardaron como ventas
        original_type = summary.get("inferred_type", "")
        had_ventas = bool(summary.get("ventas_detectadas")) or original_type == "ventas"
        if not had_ventas:
            continue

        product_rows = _extract_product_rows(summary)
        if not product_rows:
            continue

        # Verificar que hay columna de nombre (sin nombre no podemos reconstruir)
        nombre_col = _find_col(list(product_rows[0].keys()), _NOMBRE_COLS) if product_rows else None
        if not nombre_col:
            continue

        price_set = _extract_price_set(product_rows)

        # Ventana temporal basada en created_at de la SaleEntry vs upload
        window_start = uploaded_file.created_at - timedelta(days=_WINDOW_DAYS)
        window_end = uploaded_file.created_at + timedelta(days=_WINDOW_DAYS)

        sale_q = select(SaleEntry).where(
            SaleEntry.tenant_id == uploaded_file.tenant_id,
            SaleEntry.notes == "Importado desde archivo",
            SaleEntry.payment_method == "cash",
            SaleEntry.provenance == "REAL",
            SaleEntry.product_id.is_(None),
            SaleEntry.voided_at.is_(None),
            SaleEntry.quantity == 1,
            SaleEntry.created_at >= window_start,
            SaleEntry.created_at <= window_end,
        )
        if price_set:
            sale_q = sale_q.where(SaleEntry.amount.in_(price_set))

        sales_result = await db.execute(sale_q)
        suspicious_sales: list[SaleEntry] = list(sales_result.scalars().all())

        if not suspicious_sales:
            continue

        # Confidencia: HIGH si hay precios que matchean; MEDIUM si solo nombre
        confidence = "HIGH" if price_set and suspicious_sales else "MEDIUM"

        candidates.append(RepairCandidate(
            tenant_id=uploaded_file.tenant_id,
            uploaded_file=uploaded_file,
            suspicious_sales=suspicious_sales,
            product_rows=product_rows,
            price_set=price_set,
            confidence=confidence,
        ))

    logger.info(
        "data_repair.detect.completed",
        candidates=len(candidates),
        tenant_id=str(tenant_id) if tenant_id else "all",
    )
    return candidates


async def _load_planned_sale_ids(
    db: AsyncSession,
    source_run_id: uuid.UUID,
) -> frozenset[uuid.UUID] | None:
    """Carga los sale_entry_id planificados en un dry-run anterior.

    Retorna frozenset de UUIDs o None si el run no existe / no es dry-run.
    """
    source_run = await db.get(DataRepairRun, source_run_id)
    if source_run is None or not source_run.dry_run:
        logger.warning(
            "data_repair.source_run_not_found_or_not_dry_run",
            source_run_id=str(source_run_id),
        )
        return None

    items_result = await db.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == source_run_id,
            DataRepairItem.action == "VOID_SALE",
            DataRepairItem.sale_entry_id.isnot(None),
        )
    )
    planned = frozenset(
        item.sale_entry_id
        for item in items_result.scalars().all()
        if item.sale_entry_id is not None
    )
    logger.info(
        "data_repair.source_run_loaded",
        source_run_id=str(source_run_id),
        planned_sale_ids=len(planned),
    )
    return planned


def _filter_candidates_to_plan(
    candidates: list[RepairCandidate],
    planned_sale_ids: frozenset[uuid.UUID],
) -> list[RepairCandidate]:
    """Filtra los suspicious_sales de cada candidato a solo los planificados.

    Elimina candidatos que ya no tienen ventas a anular tras el filtro.
    Loguea si alguna venta planificada ya no aparece (puede haber sido anulada manualmente).
    """
    from app.observability.logger import get_logger as _get_logger  # noqa: PLC0415
    _log = _get_logger(__name__)

    filtered: list[RepairCandidate] = []
    for candidate in candidates:
        filtered_sales = [s for s in candidate.suspicious_sales if s.id in planned_sale_ids]
        missing = {s.id for s in candidate.suspicious_sales} - {s.id for s in filtered_sales}
        # Ventas en el plan pero no detectadas ahora (pueden haber sido ya anuladas)
        undetected = planned_sale_ids - {s.id for s in candidate.suspicious_sales}
        if undetected:
            _log.warning(
                "data_repair.plan_sales_not_found",
                tenant_id=str(candidate.tenant_id),
                undetected_count=len(undetected),
            )
        if missing:
            _log.info(
                "data_repair.sales_not_in_plan_skipped",
                tenant_id=str(candidate.tenant_id),
                skipped_count=len(missing),
            )
        if filtered_sales or candidate.product_rows:
            filtered.append(RepairCandidate(
                tenant_id=candidate.tenant_id,
                uploaded_file=candidate.uploaded_file,
                suspicious_sales=filtered_sales,
                product_rows=candidate.product_rows,
                price_set=candidate.price_set,
                confidence=candidate.confidence,
            ))
    return filtered


async def apply_repair(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    dry_run: bool = True,
    source_run_id: uuid.UUID | None = None,
) -> RepairResult:
    """Ejecuta la reparación (o simula si dry_run=True).

    dry_run=True: persiste DataRepairRun + DataRepairItems pero NO modifica
                  SaleEntry ni Product.
    dry_run=False: voidea las SaleEntry, crea/actualiza Products, persiste run.
                   Si source_run_id apunta a un dry-run anterior, lo marca como APPLIED.

    Fail parcial: cada candidato corre bajo savepoint propio. Si falla, se hace rollback
                  local a ese savepoint y el run continúa con los demás candidatos.
    """
    now = datetime.now(UTC)

    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_MISCLASSIFIED,
        status="RUNNING",
        dry_run=dry_run,
        source_run_id=source_run_id,
        created_at=now,
    )
    db.add(run)
    await db.flush()

    candidates = await detect_misclassified_product_imports(db, tenant_id=tenant_id)

    result = RepairResult(
        run_id=run.id,
        dry_run=dry_run,
        candidates_found=len(candidates),
        sales_detected=sum(len(c.suspicious_sales) for c in candidates),
        sales_voided=0,
        products_detected=sum(len(c.product_rows) for c in candidates),
        products_created=0,
        products_updated=0,
        products_skipped=0,
    )

    # Si apply con source_run_id: filtrar ventas a exactamente las planificadas en el dry-run
    planned_sale_ids: frozenset[uuid.UUID] | None = None
    if not dry_run and source_run_id is not None:
        planned_sale_ids = await _load_planned_sale_ids(db, source_run_id)
        if planned_sale_ids is not None:
            candidates = _filter_candidates_to_plan(candidates, planned_sale_ids)
            result.candidates_found = len(candidates)
            result.sales_detected = sum(len(c.suspicious_sales) for c in candidates)
            result.products_detected = sum(len(c.product_rows) for c in candidates)

    run.candidates_found = result.candidates_found
    run.sales_detected = result.sales_detected
    run.products_detected = result.products_detected

    for candidate in candidates:
        # Savepoint por candidato: si falla, rollback local y continuamos con el siguiente
        savepoint = await db.begin_nested()
        try:
            await _process_candidate(db, run, candidate, dry_run, result)
            await savepoint.commit()
        except Exception as exc:
            await savepoint.rollback()
            logger.warning(
                "data_repair.candidate.failed",
                tenant_id=str(candidate.tenant_id),
                file_id=str(candidate.uploaded_file.id),
                error=str(exc),
            )
            result.tenant_results.append({
                "tenant_id": str(candidate.tenant_id),
                "file_id": str(candidate.uploaded_file.id),
                "status": "FAILED",
                "error": str(exc),
            })

    run.sales_voided = result.sales_voided
    run.products_created = result.products_created
    run.products_updated = result.products_updated
    run.products_skipped = result.products_skipped
    run.status = "COMPLETED"
    run.completed_at = datetime.now(UTC)
    run.details_json = {"tenant_results": result.tenant_results} if result.tenant_results else {}

    # Si es apply con source_run_id, marcar el run fuente como APPLIED
    if not dry_run and source_run_id is not None:
        source_run = await db.get(DataRepairRun, source_run_id)
        if source_run is not None and source_run.dry_run:
            source_run.status = "APPLIED"

    await db.flush()

    logger.info(
        "data_repair.apply.completed",
        run_id=str(run.id),
        dry_run=dry_run,
        candidates=result.candidates_found,
        sales_voided=result.sales_voided,
        products_created=result.products_created,
    )
    return result


async def _process_candidate(
    db: AsyncSession,
    run: DataRepairRun,
    candidate: RepairCandidate,
    dry_run: bool,
    result: RepairResult,
) -> None:
    """Procesa un candidato: crea productos y anula ventas (o solo planifica en dry_run)."""
    now = datetime.now(UTC)
    file_id = candidate.uploaded_file.id
    tenant_id = candidate.tenant_id

    # ── Crear/actualizar productos ────────────────────────────────────────────
    if not dry_run:
        summary = candidate.uploaded_file.parsed_summary_json or {}
        # Forzar stock_detectado desde product_rows para que insert_confirmed_data los use
        summary_copy = dict(summary)
        summary_copy["stock_detectado"] = candidate.product_rows
        summary_copy["inferred_type"] = "stock"

        counts = await insert_confirmed_data(
            db,
            tenant_id=tenant_id,
            summary=summary_copy,
            confirmed_fields={"ventas": False, "gastos": False, "productos": True},
            return_details=True,
        )
        product_details: list[dict[str, Any]] = counts.get("product_details", [])
        for detail in product_details:
            action = detail["action"]
            if action == "CREATED":
                result.products_created += 1
            elif action == "UPDATED":
                result.products_updated += 1
            db.add(DataRepairItem(
                id=uuid.uuid4(),
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                product_id=uuid.UUID(detail["product_id"]) if detail.get("product_id") else None,
                action="CREATE_PRODUCT" if action == "CREATED" else "UPDATE_PRODUCT",
                before_json=detail.get("before"),
                after_json=detail.get("after"),
                confidence=candidate.confidence,
                created_at=now,
            ))
    else:
        # Dry-run: registrar items planificados; distingue CREATE vs UPDATE comprobando DB
        from sqlalchemy import func as _func  # noqa: PLC0415
        from app.application.services.ingestion_import_service import _normalize_name  # noqa: PLC0415
        from app.persistence.models.product import Product as _Product  # noqa: PLC0415

        for row in candidate.product_rows:
            nombre_col = _find_col(list(row.keys()), _NOMBRE_COLS)
            name = str(row.get(nombre_col, "")).strip()[:299] if nombre_col else ""
            if not name or name.lower() in {"none", "nan", ""}:
                continue

            # Verificar si el producto ya existe (mismo lookup que apply: exacto + fallback normalizado)
            existing_res = await db.execute(
                select(_Product).where(
                    _Product.tenant_id == tenant_id,
                    _func.lower(_func.trim(_Product.name)) == name.lower(),
                )
            )
            existing_product = existing_res.scalar_one_or_none()
            if existing_product is None:
                all_res = await db.execute(
                    select(_Product).where(_Product.tenant_id == tenant_id)
                )
                norm_input = _normalize_name(name)
                for prod in all_res.scalars().all():
                    if _normalize_name(prod.name) == norm_input:
                        existing_product = prod
                        break
            planned_action = "UPDATE_PRODUCT" if existing_product else "CREATE_PRODUCT"

            db.add(DataRepairItem(
                id=uuid.uuid4(),
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                product_id=existing_product.id if existing_product else None,
                action=planned_action,
                before_json={
                    "sale_price_ars": str(existing_product.sale_price_ars) if existing_product else None,
                    "planned": True,
                },
                after_json={"name": name, "planned": True},
                confidence=candidate.confidence,
                created_at=now,
            ))

    # ── Anular SaleEntry sospechosas ─────────────────────────────────────────
    for sale in candidate.suspicious_sales:
        if not dry_run:
            sale.voided_at = now
            sale.void_reason = VOID_REASON
            sale.voided_by_repair_run_id = run.id
            result.sales_voided += 1

        db.add(DataRepairItem(
            id=uuid.uuid4(),
            run_id=run.id,
            tenant_id=tenant_id,
            source_file_id=file_id,
            sale_entry_id=sale.id,
            action="VOID_SALE",
            before_json={
                "amount": str(sale.amount),
                "transaction_date": str(sale.transaction_date),
                "notes": sale.notes,
                "planned": dry_run,
            },
            confidence=candidate.confidence,
            created_at=now,
        ))

    result.tenant_results.append({
        "tenant_id": str(tenant_id),
        "file_id": str(file_id),
        "status": "COMPLETED",
        "sales_processed": len(candidate.suspicious_sales),
        "product_rows": len(candidate.product_rows),
    })

    await db.flush()
