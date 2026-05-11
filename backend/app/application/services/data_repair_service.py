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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import infer_spreadsheet_type
from app.application.services.ingestion_import_service import (
    _NOMBRE_COLS,
    _PRECIO_VENTA_COLS,
    _find_col,
    insert_confirmed_data,
)
from app.observability.logger import get_logger
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry

logger = get_logger(__name__)

REPAIR_TYPE_MISCLASSIFIED = "MISCLASSIFIED_PRODUCT_IMPORT"
REPAIR_TYPE_DATA_QUALITY = "DATA_QUALITY_REPAIR"
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


@dataclass
class ChatSaleRepairCandidate:
    tenant_id: uuid.UUID
    sale: SaleEntry
    quantity: int
    product_description: str
    product: Product | None
    alternatives: list[dict[str, Any]]
    confidence: str
    reason: str


_SALE_QTY_RE = re.compile(
    r"\b(?:venta\s+de|vend[ií]|vendi|vendió|vendio)\s+(\d+(?:[,.]\d+)?)\s+(.+)",
    re.IGNORECASE,
)


def _sale_snapshot(sale: SaleEntry) -> dict[str, Any]:
    return {
        "id": str(sale.id),
        "amount": str(sale.amount),
        "quantity": sale.quantity,
        "transaction_date": str(sale.transaction_date),
        "payment_method": sale.payment_method,
        "product_id": str(sale.product_id) if sale.product_id else None,
        "notes": sale.notes,
    }


def _parse_quantity_from_notes(notes: str | None) -> tuple[int, str] | None:
    if not notes:
        return None
    match = _SALE_QTY_RE.search(notes.strip())
    if not match:
        return None
    try:
        quantity = int(float(match.group(1).replace(",", ".")))
    except ValueError:
        return None
    if quantity <= 1:
        return None
    desc = re.sub(
        r"\b(?:con|en|por)\s+(?:efectivo|d[eé]bito|credito|cr[eé]dito|transferencia|qr)\b.*$",
        "",
        match.group(2).strip(),
        flags=re.IGNORECASE,
    ).strip(" .,-")
    if not desc:
        return None
    return quantity, desc


async def _find_unique_product_for_description(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    description: str,
) -> tuple[Product | None, list[dict[str, Any]]]:
    tokens = sorted(
        {t.lower() for t in re.findall(r"[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9]{3,}", description)},
        key=len,
        reverse=True,
    )
    if not tokens:
        return None, []

    matches: dict[uuid.UUID, Product] = {}
    for token in tokens:
        result = await db.execute(
            select(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.provenance == "REAL",
                Product.is_active.is_(True),
                Product.sale_price_ars > 0,
                func.lower(Product.name).contains(token),
            )
            .limit(5)
        )
        for product in result.scalars().all():
            matches[product.id] = product

    alternatives = [
        {
            "product_id": str(product.id),
            "name": product.name,
            "sale_price_ars": str(product.sale_price_ars),
        }
        for product in matches.values()
    ]
    if len(matches) != 1:
        return None, alternatives
    return next(iter(matches.values())), alternatives


async def detect_chat_sale_quality_issues(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
) -> list[ChatSaleRepairCandidate]:
    sale_q = select(SaleEntry).where(
        SaleEntry.provenance == "REAL",
        SaleEntry.voided_at.is_(None),
        SaleEntry.notes.isnot(None),
    )
    if tenant_id is not None:
        sale_q = sale_q.where(SaleEntry.tenant_id == tenant_id)

    try:
        result = await db.execute(sale_q)
    except StopAsyncIteration:
        return []
    candidates: list[ChatSaleRepairCandidate] = []
    for sale in result.scalars().all():
        parsed = _parse_quantity_from_notes(sale.notes)
        if parsed is None:
            continue
        quantity, description = parsed
        product: Product | None = None
        alternatives: list[dict[str, Any]] = []

        if sale.product_id is not None:
            product = await db.get(Product, sale.product_id)
            if product is None or product.tenant_id != sale.tenant_id or not product.is_active:
                product = None
        if product is None:
            product, alternatives = await _find_unique_product_for_description(
                db, sale.tenant_id, description
            )

        if product is None:
            candidates.append(ChatSaleRepairCandidate(
                tenant_id=sale.tenant_id,
                sale=sale,
                quantity=quantity,
                product_description=description,
                product=None,
                alternatives=alternatives,
                confidence="MEDIUM",
                reason="ambiguous_or_missing_product",
            ))
            continue

        expected_amount = product.sale_price_ars * Decimal(str(quantity))
        needs_update = (
            sale.quantity != quantity
            or sale.product_id != product.id
            or sale.amount != expected_amount
        )
        if not needs_update:
            continue
        candidates.append(ChatSaleRepairCandidate(
            tenant_id=sale.tenant_id,
            sale=sale,
            quantity=quantity,
            product_description=description,
            product=product,
            alternatives=[],
            confidence="HIGH",
            reason="unique_catalog_match",
        ))

    logger.info(
        "data_repair.detect_chat_sales.completed",
        candidates=len(candidates),
        tenant_id=str(tenant_id) if tenant_id else "all",
    )
    return candidates


def _re_evaluate_summary(summary: dict[str, Any]) -> str | None:
    """Re-evalúa el parsed_summary_json con la lógica actual de clasificación.

    Retorna "stock" si el archivo debe ser tratado como catálogo de productos,
    cualquier otro tipo si no, o None si no hay datos.

    Además de re-ejecutar infer_spreadsheet_type con la lógica nueva, aplica
    un fallback: si el summary guardado tiene has_producto=True Y fue clasificado
    como ventas, es señal del bug original (ej: "descripcion+importe+fecha" donde
    "descripcion" no está en NOMBRE_COLS pero SÍ en PRODUCTO_COLS). En ese caso
    también se trata como "stock" para fines de detección de reparación.
    """
    if not summary:
        return None

    has_fecha = bool(summary.get("has_fecha"))
    has_venta = bool(summary.get("has_venta"))
    has_gasto = bool(summary.get("has_gasto"))
    has_producto = bool(summary.get("has_producto"))  # incluye "descripcion", "articulo", etc.
    has_precio_ambiguo = bool(
        any(
            col in ("precio", "total", "price", "valor")
            for col in [h.lower().strip().replace(" ", "_") for h in summary.get("columns", [])]
        )
    )
    from app.application.services.file_parsing import CATALOGO_COLS, NOMBRE_COLS  # noqa: PLC0415

    # Derivar headers de la lista "columns" guardada, o de las claves de las primeras filas
    # cuando "columns" es null (formato viejo que no lo almacenaba).
    stored_columns = summary.get("columns") or []
    if not stored_columns:
        # Intentar extraer nombres de columna desde las filas de cualquier bucket
        for bucket in ("ventas_detectadas", "stock_detectado", "preview_rows", "gastos_detectados"):
            rows_bucket = summary.get(bucket) or []
            if rows_bucket and isinstance(rows_bucket[0], dict):
                stored_columns = list(rows_bucket[0].keys())
                break

    headers_norm = [h.lower().strip().replace(" ", "_") for h in stored_columns]
    has_catalogo_fuerte = any(any(k in col for k in CATALOGO_COLS) for col in headers_norm)
    has_nombre = any(any(k in col for k in NOMBRE_COLS) for col in headers_norm)

    current_type = infer_spreadsheet_type(
        has_fecha=has_fecha,
        has_venta=has_venta,
        has_gasto=has_gasto,
        has_producto=has_producto,
        has_precio_ambiguo=has_precio_ambiguo,
        has_catalogo_fuerte=has_catalogo_fuerte,
        has_nombre=has_nombre,
    )

    if current_type == "stock":
        return "stock"

    # Fallback para el bug original (has_venta+has_fecha ganaba sobre has_producto).
    # Solo aplica cuando el summary guardado tenía inferred_type="ventas" Y has_producto=True.
    #
    # Dos niveles de certeza:
    # 1. ALTA: columnas inequívocamente de catálogo (articulo, sku, codigo, stock, inventario,
    #    nombre, producto) → siempre tratamos como "stock".
    # 2. MEDIA: solo "descripcion" (ambigua) → aceptar como "stock" solo si TAMBIÉN hay
    #    columna de precio-catálogo (precio, precio_venta, price) y NO solo monto/importe
    #    (que son columnas transaccionales de ventas).
    stored_inferred = summary.get("inferred_type") or ""  # null → ""

    # Caso especial: formato viejo sin inferred_type guardado.
    # Si has_producto=True + ventas_detectadas tiene filas + stock_detectado vacío,
    # los productos fueron almacenados en el bucket equivocado (ventas en lugar de stock).
    if not stored_inferred and has_producto:
        has_ventas_bucket = bool(summary.get("ventas_detectadas"))
        has_stock_bucket = bool(summary.get("stock_detectado"))
        if has_ventas_bucket and not has_stock_bucket:
            return "stock"

    if not (has_producto and stored_inferred == "ventas"):
        return current_type

    # Señales fuertes: columnas inequívocamente de catálogo
    strong_catalog = CATALOGO_COLS | NOMBRE_COLS
    has_strong_catalog = any(any(k in col for k in strong_catalog) for col in headers_norm)
    if has_strong_catalog:
        return "stock"

    # Señal débil: "descripcion" sola
    # Solo aceptar si hay columna de precio-catálogo (precio, precio_venta, price)
    # y NO solo columnas transaccionales (monto, importe, total_cobrado, etc.)
    catalog_price_cols = {"precio", "precio_venta", "price", "p_venta"}
    transactional_amount_cols = {
        "monto",
        "importe",
        "total_venta",
        "total_cobrado",
        "cobro",
        "ingreso",
    }
    has_catalog_price = any(any(k in col for k in catalog_price_cols) for col in headers_norm)
    has_transactional_only = any(
        any(k in col for k in transactional_amount_cols) for col in headers_norm
    )

    if has_catalog_price and not has_transactional_only:
        # descripcion + precio → catálogo de productos
        return "stock"

    # descripcion + monto/importe sin precio-catálogo → probablemente ventas reales
    return current_type


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
    - El original fue importado como ventas

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
        repair_type=REPAIR_TYPE_DATA_QUALITY,
        status="RUNNING",
        dry_run=dry_run,
        source_run_id=source_run_id,
        created_at=now,
    )
    db.add(run)
    await db.flush()

    candidates = await detect_misclassified_product_imports(db, tenant_id=tenant_id)
    chat_candidates = await detect_chat_sale_quality_issues(db, tenant_id=tenant_id)

    result = RepairResult(
        run_id=run.id,
        dry_run=dry_run,
        candidates_found=len(candidates) + len(chat_candidates),
        sales_detected=sum(len(c.suspicious_sales) for c in candidates) + len(chat_candidates),
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
            result.sales_detected = (
                sum(len(c.suspicious_sales) for c in candidates) + len(chat_candidates)
            )
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

    for candidate in chat_candidates:
        savepoint = await db.begin_nested()
        try:
            await _process_chat_sale_candidate(db, run, candidate, dry_run, result)
            await savepoint.commit()
        except Exception as exc:
            await savepoint.rollback()
            logger.warning(
                "data_repair.chat_sale.failed",
                tenant_id=str(candidate.tenant_id),
                sale_id=str(candidate.sale.id),
                error=str(exc),
            )
            result.tenant_results.append({
                "tenant_id": str(candidate.tenant_id),
                "sale_id": str(candidate.sale.id),
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

        from app.application.services.ingestion_import_service import (
            _normalize_name,  # noqa: PLC0415
        )
        from app.persistence.models.product import Product as _Product  # noqa: PLC0415

        for row in candidate.product_rows:
            nombre_col = _find_col(list(row.keys()), _NOMBRE_COLS)
            name = str(row.get(nombre_col, "")).strip()[:299] if nombre_col else ""
            if not name or name.lower() in {"none", "nan", ""}:
                continue

            # Verificar si el producto ya existe.
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
                    "sale_price_ars": (
                        str(existing_product.sale_price_ars) if existing_product else None
                    ),
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


async def _process_chat_sale_candidate(
    db: AsyncSession,
    run: DataRepairRun,
    candidate: ChatSaleRepairCandidate,
    dry_run: bool,
    result: RepairResult,
) -> None:
    now = datetime.now(UTC)
    sale = candidate.sale
    before = _sale_snapshot(sale)

    if candidate.product is None:
        db.add(DataRepairItem(
            id=uuid.uuid4(),
            run_id=run.id,
            tenant_id=candidate.tenant_id,
            sale_entry_id=sale.id,
            action="REVIEW_SALE",
            before_json=before,
            after_json={
                "planned": True,
                "reason": candidate.reason,
                "quantity_detected": candidate.quantity,
                "product_description": candidate.product_description,
                "alternatives": candidate.alternatives,
            },
            confidence=candidate.confidence,
            created_at=now,
        ))
        result.tenant_results.append({
            "tenant_id": str(candidate.tenant_id),
            "sale_id": str(sale.id),
            "status": "REVIEW_REQUIRED",
            "reason": candidate.reason,
            "alternatives": candidate.alternatives,
        })
        await db.flush()
        return

    expected_amount = candidate.product.sale_price_ars * Decimal(str(candidate.quantity))
    after = {
        **before,
        "amount": str(expected_amount),
        "quantity": candidate.quantity,
        "product_id": str(candidate.product.id),
        "product_name": candidate.product.name,
        "planned": dry_run,
    }

    if not dry_run:
        sale.amount = expected_amount
        sale.quantity = candidate.quantity
        sale.product_id = candidate.product.id
        result.products_skipped += 0

    db.add(DataRepairItem(
        id=uuid.uuid4(),
        run_id=run.id,
        tenant_id=candidate.tenant_id,
        sale_entry_id=sale.id,
        product_id=candidate.product.id,
        action="UPDATE_SALE",
        before_json=before,
        after_json=after,
        confidence=candidate.confidence,
        created_at=now,
    ))

    if not dry_run:
        result.sales_voided += 0
        result.tenant_results.append({
            "tenant_id": str(candidate.tenant_id),
            "sale_id": str(sale.id),
            "status": "CORRECTED",
            "amount": str(expected_amount),
            "quantity": candidate.quantity,
            "product_id": str(candidate.product.id),
        })
    else:
        result.tenant_results.append({
            "tenant_id": str(candidate.tenant_id),
            "sale_id": str(sale.id),
            "status": "PLANNED_CORRECTION",
            "amount": str(expected_amount),
            "quantity": candidate.quantity,
            "product_id": str(candidate.product.id),
        })

    await db.flush()
