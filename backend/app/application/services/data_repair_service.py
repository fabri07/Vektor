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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service, stock_service
from app.application.services.file_parsing import infer_spreadsheet_type
from app.application.services.ingestion_import_service import (
    _NOMBRE_COLS,
    _PRECIO_VENTA_COLS,
    _find_col,
    _parse_qty,
    build_incomplete_product,
    insert_confirmed_data,
)
from app.application.services.operation_identity_service import (
    _IDS_POR_SENTENCIA,
    en_tandas,
    liberar_identidades_de,
)
from app.domain.expense_categories import (
    classify_expense_with_vertical,
    infer_expense_type,
    normalize_expense_category,
)
from app.domain.sale_unit import importe
from app.domain.verticals import Vertical, try_parse_vertical
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.operation_identity import OperationIdentityLink
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

REPAIR_TYPE_MISCLASSIFIED = "MISCLASSIFIED_PRODUCT_IMPORT"
REPAIR_TYPE_DATA_QUALITY = "DATA_QUALITY_REPAIR"
VOID_REASON = "REPAIR_MISCLASSIFIED_IMPORT"

# B2 — correctivo genérico:
VOID_REASON_DUPLICATE = "DUPLICATE"

# T3 (E-2): por qué un grupo de duplicados se saltea entero, sin cambios.
MOTIVO_COMPRA_YA_VENDIDA = "compra_ya_vendida"
MOTIVO_COMPRA_SIN_PAR_CLARO = "compra_sin_par_claro"

# T3 (E-3): la corrección automática de ventas del chat está APAGADA (decisión
# del dueño). El detector sigue y sus ventas quedan «para revisar».
REVIEW_REASON_CORRECCION_APAGADA = "correccion_automatica_apagada"

# Ventana temporal para asociar SaleEntry con UploadedFile:
# se usa created_at de SaleEntry vs created_at de UploadedFile (más fiable que transaction_date)
_WINDOW_DAYS = 2

# Sufijo "— proveedor" / "— importado" que el import histórico anexaba a la
# descripción: se quita para comparar la base (ya no es señal de duplicado, E-1).
_DESC_IMPORT_SUFFIX_RE = re.compile(r"\s*[—\-–]\s*[^—\-–]+$")


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
    # Contadores entity-aware (reporting honesto: no mezclar gastos con ventas/productos).
    expenses_voided: int = 0
    expenses_reclassified: int = 0


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


@dataclass
class DuplicateGroup:
    """Grupo de transacciones activas idénticas que entraron por re-subir un archivo.

    ``entity`` distingue ventas de gastos. ``keep`` es una fila de la carga más
    antigua (no se toca); ``duplicates`` son las copias de las re-subidas, que se
    anulan (soft delete ``DUPLICATE``).
    """

    tenant_id: uuid.UUID
    entity: str  # "sale" | "expense"
    keep_id: uuid.UUID
    duplicates: Sequence[SaleEntry | ExpenseEntry]
    confidence: str


class GrupoNoReparableError(Exception):
    """T3 (E-2): el grupo no se puede anular sin romper el stock; se saltea ENTERO.

    Lo levanta el plan de stock antes de tocar nada; el savepoint del grupo
    deshace igual cualquier cosa que hubiera quedado a medias.
    """

    def __init__(self, motivo: str, detalle: str) -> None:
        super().__init__(f"{motivo}: {detalle}")
        self.motivo = motivo
        self.detalle = detalle


@dataclass
class MisclassifiedExpenseCandidate:
    """Gasto OPEX cuyo texto matchea producto vendible del vertical → debe ser COGS."""

    tenant_id: uuid.UUID
    expense: ExpenseEntry
    matched_text: str
    new_category: str
    new_label: str | None
    confidence: str


_SALE_QTY_RE = re.compile(
    r"\b(?:venta\s+de|vend[ií]|vendi|vendió|vendio)\s+(\d+(?:[,.]\d+)?)\s+(.+)",
    re.IGNORECASE,
)


def _normalize_desc_base(desc: str | None) -> str:
    """Base de descripción para agrupar duplicados: minúsculas, sin sufijo "— X".

    El import histórico anexaba "— proveedor"/"— importado" al final; quitarlo
    deja la base común para detectar dos filas que son la misma operación.
    """
    if not desc:
        return ""
    base = _DESC_IMPORT_SUFFIX_RE.sub("", desc.strip())
    return re.sub(r"\s+", " ", base.lower()).strip()


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
            "base_units_per_sale_unit": product.base_units_per_sale_unit,
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
        if product is None and alternatives:
            amount_matches = [
                alt
                for alt in alternatives
                # Sólo por unidad: ver la exclusión de productos por peso abajo.
                if int(alt.get("base_units_per_sale_unit") or 1) == 1
                and importe(Decimal(str(alt["sale_price_ars"])), quantity, 1) == sale.amount
            ]
            if len(amount_matches) == 1:
                product = await db.get(Product, uuid.UUID(amount_matches[0]["product_id"]))
                if product is None or product.tenant_id != sale.tenant_id or not product.is_active:
                    product = None

        if product is None:
            candidates.append(
                ChatSaleRepairCandidate(
                    tenant_id=sale.tenant_id,
                    sale=sale,
                    quantity=quantity,
                    product_description=description,
                    product=None,
                    alternatives=alternatives,
                    confidence="MEDIUM",
                    reason="ambiguous_or_missing_product",
                )
            )
            continue

        # Un producto por peso o volumen queda fuera: la cantidad de la nota del
        # chat ("2 queso") no dice la unidad, y repararla con un importe
        # calculado sería inventarlo. El chat ya no registra esas ventas.
        if (product.base_units_per_sale_unit or 1) > 1:
            continue
        expected_amount = importe(
            product.sale_price_ars, quantity, product.base_units_per_sale_unit
        )
        needs_update = (
            sale.quantity != quantity
            or sale.product_id != product.id
            or sale.amount != expected_amount
        )
        if not needs_update:
            continue
        candidates.append(
            ChatSaleRepairCandidate(
                tenant_id=sale.tenant_id,
                sale=sale,
                quantity=quantity,
                product_description=description,
                product=product,
                alternatives=[],
                confidence="HIGH",
                reason="unique_catalog_match",
            )
        )

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
    from app.application.services.file_parsing import (  # noqa: PLC0415
        CATALOGO_COLS,
        FECHA_COLS,
        FECHA_NO_TRANSACCIONAL_COLS,
        FORMA_PAGO_COLS,
        GASTO_SIGNAL_COLS,
        NOMBRE_COLS,
        PRECIO_COL_KEYS,
        PROVEEDOR_COLS,
        TRANSACCION_MONTO_COLS,
        VENTA_SIGNAL_COLS,
    )
    from app.domain.header_keys import match_key  # noqa: PLC0415

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
    collapsed = [match_key(col) for col in headers_norm]
    has_catalogo_fuerte = any(any(k in col for k in CATALOGO_COLS) for col in headers_norm)
    has_nombre = any(any(k in col for k in NOMBRE_COLS) for col in headers_norm)

    # Señales de OPERACIÓN, derivadas de los headers guardados con las mismas reglas
    # que `analyze_headers`. Sin ellas, esta re-evaluación se quedaba con la lógica
    # vieja y devolvía "stock" para un archivo de ventas legítimo con columna de
    # producto — es decir, lo proponía para que le anularan las ventas.
    #
    # has_fecha/has_venta/has_gasto/has_producto siguen viniendo del summary
    # guardado a propósito: los summaries viejos se calcularon con sets de keywords
    # anteriores, y recalcularlos desde los headers cambiaría el tipo de archivos
    # legacy que este detector justamente tiene que reconocer.
    has_monto_transaccion = any(col in TRANSACCION_MONTO_COLS for col in headers_norm)
    has_forma_pago = any(key in FORMA_PAGO_COLS for key in collapsed)
    has_proveedor = any(col in PROVEEDOR_COLS for col in headers_norm)
    venta_score = sum(any(k in col for k in VENTA_SIGNAL_COLS) for col in headers_norm)
    gasto_score = sum(any(k in col for k in GASTO_SIGNAL_COLS) for col in headers_norm)
    has_contexto_operacion = any(
        (any(k in col for k in VENTA_SIGNAL_COLS) or any(k in col for k in GASTO_SIGNAL_COLS))
        and not any(p in col for p in PRECIO_COL_KEYS)
        for col in headers_norm
    )
    has_fecha_transaccional = any(
        any(k in col for k in FECHA_COLS)
        and not any(k in col for k in FECHA_NO_TRANSACCIONAL_COLS)
        for col in headers_norm
    )

    current_type = infer_spreadsheet_type(
        has_fecha=has_fecha,
        has_fecha_transaccional=has_fecha_transaccional if headers_norm else None,
        has_contexto_operacion=has_contexto_operacion if headers_norm else None,
        has_venta=has_venta,
        has_gasto=has_gasto,
        has_producto=has_producto,
        has_precio_ambiguo=has_precio_ambiguo,
        has_catalogo_fuerte=has_catalogo_fuerte,
        has_nombre=has_nombre,
        has_monto_transaccion=has_monto_transaccion,
        has_forma_pago=has_forma_pago,
        has_proveedor=has_proveedor,
        venta_score=venta_score,
        gasto_score=gasto_score,
    )

    if current_type == "stock":
        return "stock"

    # El fallback de abajo compensa el sesgo del clasificador VIEJO, que dejaba
    # ganar has_venta+has_fecha sobre has_producto. No aplica cuando la
    # re-evaluación actual vio la evidencia de operación completa (fecha de
    # operación + monto de la transacción): ahí "ventas"/"gastos" es un libro real
    # con columna de producto, no un catálogo mal guardado, y mandarlo al fallback
    # lo propondría para que le anulen los movimientos.
    if has_monto_transaccion and has_fecha_transaccional and current_type in ("ventas", "gastos"):
        return current_type

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

        candidates.append(
            RepairCandidate(
                tenant_id=uploaded_file.tenant_id,
                uploaded_file=uploaded_file,
                suspicious_sales=suspicious_sales,
                product_rows=product_rows,
                price_set=price_set,
                confidence=confidence,
            )
        )

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
            filtered.append(
                RepairCandidate(
                    tenant_id=candidate.tenant_id,
                    uploaded_file=candidate.uploaded_file,
                    suspicious_sales=filtered_sales,
                    product_rows=candidate.product_rows,
                    price_set=candidate.price_set,
                    confidence=candidate.confidence,
                )
            )
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
    # B2 — correctivo genérico: duplicados + misclasificación OPEX→COGS.
    duplicate_groups = await detect_duplicate_imports(db, tenant_id=tenant_id)
    misclassified_expenses = await detect_misclassified_opex_to_cogs(db, tenant_id=tenant_id)

    result = RepairResult(
        run_id=run.id,
        dry_run=dry_run,
        candidates_found=(
            len(candidates)
            + len(chat_candidates)
            + len(duplicate_groups)
            + len(misclassified_expenses)
        ),
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
            result.sales_detected = sum(len(c.suspicious_sales) for c in candidates) + len(
                chat_candidates
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
            result.tenant_results.append(
                {
                    "tenant_id": str(candidate.tenant_id),
                    "file_id": str(candidate.uploaded_file.id),
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    for chat_candidate in chat_candidates:
        savepoint = await db.begin_nested()
        try:
            await _process_chat_sale_candidate(db, run, chat_candidate, dry_run, result)
            await savepoint.commit()
        except Exception as exc:
            await savepoint.rollback()
            logger.warning(
                "data_repair.chat_sale.failed",
                tenant_id=str(chat_candidate.tenant_id),
                sale_id=str(chat_candidate.sale.id),
                error=str(exc),
            )
            result.tenant_results.append(
                {
                    "tenant_id": str(chat_candidate.tenant_id),
                    "sale_id": str(chat_candidate.sale.id),
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    # B2 Detector 1 — duplicados (ventas/gastos de un archivo re-subido, E-1).
    dup_planned: frozenset[uuid.UUID] | None = None
    reclass_planned: frozenset[uuid.UUID] | None = None
    if not dry_run and source_run_id is not None:
        dup_planned = await _load_planned_ids_by_action(
            db, source_run_id, "VOID_DUPLICATE", "sale_entry_id"
        )
        reclass_planned = await _load_planned_ids_by_action(
            db, source_run_id, "RECLASSIFY_EXPENSE", "before_id"
        )
    # Dry-run: lo que cada grupo ya le habría sacado al stock (ver el plan).
    efecto_previo: dict[uuid.UUID, int] | None = {} if dry_run else None
    for group in duplicate_groups:
        savepoint = await db.begin_nested()
        try:
            await _process_duplicate_group(
                db, run, group, dry_run, result, dup_planned, efecto_previo
            )
            await savepoint.commit()
        except GrupoNoReparableError as exc:
            # T3 (E-2): salteado entero, sin cambios, y se informa.
            await savepoint.rollback()
            logger.warning(
                "data_repair.duplicate_group.skipped",
                tenant_id=str(group.tenant_id),
                entity=group.entity,
                motivo=exc.motivo,
            )
            result.tenant_results.append(
                {
                    "tenant_id": str(group.tenant_id),
                    "entity": group.entity,
                    "status": "SKIPPED",
                    "kept_id": str(group.keep_id),
                    "reason": exc.motivo,
                    "detail": exc.detalle,
                }
            )
        except Exception as exc:
            await savepoint.rollback()
            logger.warning(
                "data_repair.duplicate_group.failed",
                tenant_id=str(group.tenant_id),
                entity=group.entity,
                error=str(exc),
            )
            result.tenant_results.append(
                {
                    "tenant_id": str(group.tenant_id),
                    "entity": group.entity,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    # B2 Detector 2 — misclasificación OPEX→COGS vertical-aware.
    for mis_candidate in misclassified_expenses:
        savepoint = await db.begin_nested()
        try:
            await _process_misclassified_expense(
                db, run, mis_candidate, dry_run, result, reclass_planned
            )
            await savepoint.commit()
        except Exception as exc:
            await savepoint.rollback()
            logger.warning(
                "data_repair.misclassified_expense.failed",
                tenant_id=str(mis_candidate.tenant_id),
                expense_id=str(mis_candidate.expense.id),
                error=str(exc),
            )
            result.tenant_results.append(
                {
                    "tenant_id": str(mis_candidate.tenant_id),
                    "expense_id": str(mis_candidate.expense.id),
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    run.sales_voided = result.sales_voided
    run.products_created = result.products_created
    run.products_updated = result.products_updated
    run.products_skipped = result.products_skipped
    run.status = "COMPLETED"
    run.completed_at = datetime.now(UTC)
    run.details_json = {
        "expenses_voided": result.expenses_voided,
        "expenses_reclassified": result.expenses_reclassified,
        **({"tenant_results": result.tenant_results} if result.tenant_results else {}),
    }

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
        expenses_voided=result.expenses_voided,
        expenses_reclassified=result.expenses_reclassified,
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
            db.add(
                DataRepairItem(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    tenant_id=tenant_id,
                    source_file_id=file_id,
                    product_id=uuid.UUID(detail["product_id"])
                    if detail.get("product_id")
                    else None,
                    action="CREATE_PRODUCT" if action == "CREATED" else "UPDATE_PRODUCT",
                    before_json=detail.get("before"),
                    after_json=detail.get("after"),
                    confidence=candidate.confidence,
                    created_at=now,
                )
            )
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
                all_res = await db.execute(select(_Product).where(_Product.tenant_id == tenant_id))
                norm_input = _normalize_name(name)
                for prod in all_res.scalars().all():
                    if _normalize_name(prod.name) == norm_input:
                        existing_product = prod
                        break
            planned_action = "UPDATE_PRODUCT" if existing_product else "CREATE_PRODUCT"

            db.add(
                DataRepairItem(
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
                )
            )

    # ── Anular SaleEntry sospechosas ─────────────────────────────────────────
    for sale in candidate.suspicious_sales:
        movimiento: InventoryMovement | None = None
        if not dry_run:
            # T3 (E-2): anular la venta devuelve su descuento de stock, si lo
            # tuvo (el mismo `sale:{id}` del vivo y del replay del import).
            movimiento = await stock_service._live_sale_movement(sale.id, tenant_id, db)
            if movimiento is not None:
                await stock_service.void_movement(movimiento, db)
            sale.voided_at = now
            sale.void_reason = VOID_REASON
            sale.voided_by_repair_run_id = run.id
            result.sales_voided += 1

        db.add(
            DataRepairItem(
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
                after_json=(
                    {
                        "movement_id": str(movimiento.id),
                        "movement_qty": movimiento.qty,
                    }
                    if movimiento is not None
                    else None
                ),
                confidence=candidate.confidence,
                created_at=now,
            )
        )
    if not dry_run and candidate.suspicious_sales:
        # Re-subir el archivo correcto vuelve a poder cargar esas filas.
        await liberar_identidades_de(
            db, tenant_id, [("sale", s.id) for s in candidate.suspicious_sales]
        )

    result.tenant_results.append(
        {
            "tenant_id": str(tenant_id),
            "file_id": str(file_id),
            "status": "COMPLETED",
            "sales_processed": len(candidate.suspicious_sales),
            "product_rows": len(candidate.product_rows),
        }
    )

    await db.flush()


async def _process_chat_sale_candidate(
    db: AsyncSession,
    run: DataRepairRun,
    candidate: ChatSaleRepairCandidate,
    dry_run: bool,
    result: RepairResult,
) -> None:
    """Deja la venta del chat «para revisar» (``REVIEW_SALE``); NUNCA la modifica.

    T3 (E-3), decisión del dueño: la corrección automática (``UPDATE_SALE``) está
    APAGADA. Pisaba importe, cantidad y producto sin tocar el descuento de stock
    ya hecho (el ``sale:{id}`` quedaba con la cantidad y el producto viejos), sin
    mirar el arqueo del día, y cambiaba el importe cobrado por el de lista. Cuando
    el detector encontró un producto único, la corrección queda como SUGERENCIA
    en el ítem, igual en el dry-run que en el apply.
    """
    now = datetime.now(UTC)
    sale = candidate.sale
    before = _sale_snapshot(sale)

    after: dict[str, Any] = {
        "planned": True,
        "reason": candidate.reason,
        "quantity_detected": candidate.quantity,
        "product_description": candidate.product_description,
        "alternatives": candidate.alternatives,
    }
    entry: dict[str, Any] = {
        "tenant_id": str(candidate.tenant_id),
        "sale_id": str(sale.id),
        "status": "REVIEW_REQUIRED",
        "reason": candidate.reason,
        "alternatives": candidate.alternatives,
    }
    if candidate.product is not None:
        sugerencia = {
            "amount": str(
                importe(
                    candidate.product.sale_price_ars,
                    candidate.quantity,
                    candidate.product.base_units_per_sale_unit,
                )
            ),
            "quantity": candidate.quantity,
            "product_id": str(candidate.product.id),
            "product_name": candidate.product.name,
        }
        after["suggested_correction"] = sugerencia
        after["auto_correction"] = REVIEW_REASON_CORRECCION_APAGADA
        entry["suggested_correction"] = sugerencia

    db.add(
        DataRepairItem(
            id=uuid.uuid4(),
            run_id=run.id,
            tenant_id=candidate.tenant_id,
            sale_entry_id=sale.id,
            product_id=candidate.product.id if candidate.product is not None else None,
            action="REVIEW_SALE",
            before_json=before,
            after_json=after,
            confidence=candidate.confidence,
            created_at=now,
        )
    )
    result.tenant_results.append(entry)
    await db.flush()


# ── B2 Detector 1: DUPLICADOS de import (ventas/gastos) ───────────────────────


def _as_date(value: Any) -> Any:
    """Día de un transaction_date que puede venir como datetime o date."""
    if value is None:
        return None
    date_attr = getattr(value, "date", None)
    return date_attr() if callable(date_attr) else value


def _sale_dup_key(sale: SaleEntry) -> tuple[Any, ...]:
    """Clave de contenido fuerte para agrupar ventas idénticas."""
    return (
        _as_date(sale.transaction_date),
        str(sale.amount),
        _normalize_desc_base(sale.notes),
        sale.product_id,
        sale.payment_method,
    )


def _expense_dup_key(expense: ExpenseEntry) -> tuple[Any, ...]:
    """Clave de contenido fuerte para agrupar gastos idénticos."""
    return (
        _as_date(expense.transaction_date),
        str(expense.amount),
        _normalize_desc_base(expense.description),
        expense.product_id,
        expense.payment_method,
    )


#: Una carga: (cuándo se subió, id). Sólo ordena cuál copia se conserva.
_OrdenDeCarga = tuple[datetime | None, uuid.UUID]


def _orden_de_fila(fila: SaleEntry | ExpenseEntry) -> tuple[bool, str, str]:
    return (fila.created_at is None, str(fila.created_at), str(fila.id))


def _copias_de_la_misma_fila(
    bucket: Sequence[SaleEntry | ExpenseEntry],
) -> tuple[
    list[tuple[SaleEntry | ExpenseEntry, list[SaleEntry | ExpenseEntry]]],
    list[SaleEntry | ExpenseEntry],
]:
    """La misma fila de un archivo aplicada dos veces sobre la MISMA carga.

    Un doble confirm o un reintento zombie (``jobs.reread_apply`` antes del #53)
    escribe dos veces la fila: mismo ``source_upload_id`` y mismo
    ``source_row_ref`` NO nulo, y no hay unique que lo impida. Los renglones
    repetidos legítimos tienen refs distintos (el ref sale del índice de la fila),
    así que E-1 no vuelve. Sin ref no se sabe de qué fila vino: no se toca.

    Devuelve ``([(conservada, copias)], sobrevivientes)``: uno por fila repetida,
    y las filas del bucket menos esas copias, para comparar entre cargas.
    """
    por_fila: dict[tuple[uuid.UUID, str], list[SaleEntry | ExpenseEntry]] = {}
    for fila in bucket:
        if fila.source_upload_id is not None and fila.source_row_ref is not None:
            por_fila.setdefault((fila.source_upload_id, fila.source_row_ref), []).append(fila)
    grupos: list[tuple[SaleEntry | ExpenseEntry, list[SaleEntry | ExpenseEntry]]] = []
    copias: set[uuid.UUID] = set()
    for filas in por_fila.values():
        if len(filas) < 2:
            continue
        conservada, *repetidas = sorted(filas, key=_orden_de_fila)
        grupos.append((conservada, repetidas))
        copias |= {f.id for f in repetidas}
    return grupos, [f for f in bucket if f.id not in copias]


def _duplicados_entre_cargas(
    bucket: Sequence[SaleEntry | ExpenseEntry],
    cargas: dict[uuid.UUID, tuple[str, _OrdenDeCarga]],
) -> list[tuple[SaleEntry | ExpenseEntry, list[SaleEntry | ExpenseEntry]]]:
    """Las copias de una operación que entró dos veces por re-subir el MISMO archivo.

    E-1: la señal era que dos filas compartieran ``source_upload_id``, que es
    justamente el caso legítimo de un archivo con renglones repetidos (cinco
    «Coca 500» del mismo día son cinco ventas, ingesta.md E8). Ahora:

    * dos filas de la MISMA carga nunca son duplicado entre sí (tampoco por
      compartir identidad de operación: es por comprobante y los renglones
      hermanos la comparten), salvo la misma fila aplicada dos veces
      (``_copias_de_la_misma_fila``, que corre antes);
    * entre cargas distintas, sólo si son el mismo archivo (``content_hash``
      igual, no nulo): que dos archivos distintos traigan la misma venta del mismo
      día no prueba nada, y el orden de inserción tampoco (invariante 2d);
    * las filas sin archivo (a mano, chat) o de un archivo sin huella no se tocan,
      ni por el sufijo «— importado» (no dice de qué carga vienen).

    Por cada archivo repetido se conserva la carga más vieja entera y de cada
    re-subida se anulan a lo sumo tantas filas como tiene la original (una copia
    de la operación por re-subida). El orden de carga sólo elige qué copia queda.
    Devuelve ``[(conservada, duplicados)]``, uno por archivo repetido.
    """
    por_archivo: dict[str, dict[uuid.UUID, list[SaleEntry | ExpenseEntry]]] = {}
    for fila in bucket:
        uid = fila.source_upload_id
        carga = cargas.get(uid) if uid is not None else None
        if uid is None or carga is None:
            continue
        por_archivo.setdefault(carga[0], {}).setdefault(uid, []).append(fila)
    grupos: list[tuple[SaleEntry | ExpenseEntry, list[SaleEntry | ExpenseEntry]]] = []
    for filas_por_carga in por_archivo.values():
        if len(filas_por_carga) < 2:
            continue
        orden = sorted(
            filas_por_carga,
            key=lambda uid: (cargas[uid][1][0] is None, str(cargas[uid][1][0]), str(uid)),
        )
        originales = sorted(filas_por_carga[orden[0]], key=_orden_de_fila)
        duplicados: list[SaleEntry | ExpenseEntry] = []
        for uid in orden[1:]:
            copia = sorted(filas_por_carga[uid], key=_orden_de_fila)
            duplicados.extend(copia[: len(originales)])
        grupos.append((originales[0], duplicados))
    return grupos


async def _cargas_con_huella(
    db: AsyncSession, upload_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, _OrdenDeCarga]]:
    """``{upload_id: (content_hash, orden de carga)}`` de los archivos con huella."""
    cargas: dict[uuid.UUID, tuple[str, _OrdenDeCarga]] = {}
    # I6-2: en tandas, por el tope de parámetros de asyncpg (todos los tenants).
    for tanda in en_tandas(sorted(upload_ids, key=str), _IDS_POR_SENTENCIA):
        filas = (
            await db.execute(
                select(
                    UploadedFile.id, UploadedFile.content_hash, UploadedFile.created_at
                ).where(
                    UploadedFile.id.in_(tanda),
                    UploadedFile.content_hash.is_not(None),
                )
            )
        ).all()
        cargas.update(
            {uid: (str(huella), (creado, uid)) for uid, huella, creado in filas if huella}
        )
    return cargas


def _candidatas(buckets: dict[tuple[Any, ...], list[Any]]) -> set[uuid.UUID]:
    """Uploads de los grupos con filas de al menos dos cargas distintas."""
    ids: set[uuid.UUID] = set()
    for bucket in buckets.values():
        de_carga = {f.source_upload_id for f in bucket if f.source_upload_id is not None}
        if len(de_carga) >= 2:
            ids |= de_carga
    return ids


async def detect_duplicate_imports(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
) -> list[DuplicateGroup]:
    """Detecta ventas/gastos activos que entraron dos veces desde un archivo.

    Agrupa por (fecha, monto, desc-base, product_id, payment_method) y, dentro de
    cada grupo, sólo cuenta como duplicado (E-1) la misma fila aplicada dos veces
    sobre una carga (``_copias_de_la_misma_fila``) o el mismo archivo subido más
    de una vez (``_duplicados_entre_cargas``). Renglones idénticos de un mismo
    archivo (refs distintos) y transacciones manuales idénticas NUNCA se agrupan.
    """
    groups: list[DuplicateGroup] = []

    # Ventas
    sale_q = select(SaleEntry).where(
        SaleEntry.provenance == "REAL",
        SaleEntry.voided_at.is_(None),
    )
    if tenant_id is not None:
        sale_q = sale_q.where(SaleEntry.tenant_id == tenant_id)
    sales = list((await db.execute(sale_q)).scalars().all())
    sale_buckets: dict[tuple[Any, ...], list[SaleEntry]] = {}
    for sale in sales:
        sale_buckets.setdefault((sale.tenant_id, *_sale_dup_key(sale)), []).append(sale)

    # Gastos
    exp_q = select(ExpenseEntry).where(
        ExpenseEntry.provenance == "REAL",
        ExpenseEntry.voided_at.is_(None),
    )
    if tenant_id is not None:
        exp_q = exp_q.where(ExpenseEntry.tenant_id == tenant_id)
    expenses = list((await db.execute(exp_q)).scalars().all())
    exp_buckets: dict[tuple[Any, ...], list[ExpenseEntry]] = {}
    for expense in expenses:
        exp_buckets.setdefault((expense.tenant_id, *_expense_dup_key(expense)), []).append(expense)

    cargas = await _cargas_con_huella(
        db, _candidatas(sale_buckets) | _candidatas(exp_buckets)
    )
    for entity, buckets in (("sale", sale_buckets), ("expense", exp_buckets)):
        for key, bucket in buckets.items():
            if len(bucket) < 2:
                continue
            de_la_fila, sobrevivientes = _copias_de_la_misma_fila(bucket)
            for conservada, duplicados in [
                *de_la_fila,
                *_duplicados_entre_cargas(sobrevivientes, cargas),
            ]:
                groups.append(
                    DuplicateGroup(
                        tenant_id=key[0],
                        entity=entity,
                        keep_id=conservada.id,
                        duplicates=duplicados,
                        confidence="HIGH",
                    )
                )

    logger.info(
        "data_repair.detect_duplicates.completed",
        groups=len(groups),
        tenant_id=str(tenant_id) if tenant_id else "all",
    )
    return groups


def _dup_snapshot(row: SaleEntry | ExpenseEntry, entity: str) -> dict[str, Any]:
    desc = row.notes if entity == "sale" else getattr(row, "description", None)
    return {
        "id": str(row.id),
        "entity": entity,
        "amount": str(row.amount),
        "transaction_date": str(row.transaction_date),
        "payment_method": row.payment_method,
        "description": desc,
        "source_upload_id": str(row.source_upload_id) if row.source_upload_id else None,
    }


def _es_linea_de_compra(expense: ExpenseEntry) -> bool:
    """¿El gasto es una línea (o el envío) de una compra de mercadería?

    MISMO criterio que ``es_linea_de_compra`` de ``backend/app/domain/linea_de_compra.py``
    (T3 · B-3, rama ``fix/t3-bloqueo-compras``, todavía no en main): cuando llegue,
    reemplazar esta copia por esa definición única. Es línea de compra todo gasto
    con ``custom_fields.purchase_group_id`` (compra a mano, remito sin archivo,
    «Otros», chat) y todo gasto de mercadería (``INVENTORY``, incluida la
    categoría vieja «mercaderia», o ``COGS``) con producto.
    """
    cf = expense.custom_fields
    if isinstance(cf, dict) and cf.get("purchase_group_id"):
        return True
    if expense.product_id is None:
        return False
    if expense.expense_type == "COGS":
        return True
    return normalize_expense_category(expense.category)[0] == "INVENTORY"


def _compra_coincide_con_gasto(monto: Decimal, m: InventoryMovement, factor: int) -> bool:
    """¿El gasto es costo × cantidad de la compra?

    Misma tolerancia que ``reparacion_stock_inicial_service._importe_coincide``
    (``unit_cost`` es ``Numeric(12,2)``: medio centavo por unidad más un centavo
    del importe; se prueba con el factor del producto y con 1). Copiada y no
    importada: ese módulo importa la relectura, que importa este.
    """
    if m.unit_cost is None or m.qty <= 0:
        return False
    for f in {factor, 1}:
        f = f if f > 0 else 1
        tolerancia = Decimal("0.005") * m.qty / f + Decimal("0.01")
        if abs(monto - importe(m.unit_cost, m.qty, f)) <= tolerancia:
            return True
    return False


async def _elegir_compra(
    db: AsyncSession,
    fila: ExpenseEntry,
    movimientos: list[InventoryMovement],
    usados: set[uuid.UUID],
) -> InventoryMovement:
    """La compra de la fila que le corresponde a ESTE gasto, o ``GrupoNoReparableError``.

    Una sola compra: es esa. Varias en la misma fila: si son iguales (cantidad y
    costo) da lo mismo cuál; si no, se empareja por monto (costo × cantidad) y,
    si quedan compras distintas que encajan —o ninguna—, no se adivina.
    """
    libres = [m for m in movimientos if m.id not in usados]
    if len(movimientos) == 1:
        return libres[0]
    firmas = {(m.qty, m.unit_cost) for m in libres}
    if len(firmas) == 1:
        return libres[0]
    producto = await db.get(Product, fila.product_id)
    factor = int(producto.base_units_per_sale_unit or 1) if producto is not None else 1
    encajan = [m for m in libres if _compra_coincide_con_gasto(fila.amount, m, factor)]
    if len({(m.qty, m.unit_cost) for m in encajan}) != 1:
        raise GrupoNoReparableError(
            MOTIVO_COMPRA_SIN_PAR_CLARO,
            f"la fila {fila.source_row_ref} tiene {len(movimientos)} compras distintas "
            f"y el monto {fila.amount} no elige una sola",
        )
    return encajan[0]


async def _compras_del_gasto(
    db: AsyncSession, expense: ExpenseEntry
) -> tuple[list[InventoryMovement], int]:
    """Los ``purchase`` vivos del gasto y cuántos gastos vivos comparten su fila.

    El par es por (archivo, fila, producto), como en ``reparacion_stock_inicial``.
    Sin alguno de los tres el gasto no tiene par (no movió stock por el import).
    """
    if (
        expense.product_id is None
        or expense.source_upload_id is None
        or expense.source_row_ref is None
    ):
        return [], 0
    movimientos = list(
        (
            await db.execute(
                select(InventoryMovement)
                .where(
                    InventoryMovement.tenant_id == expense.tenant_id,
                    InventoryMovement.movement_type == "purchase",
                    InventoryMovement.voided_at.is_(None),
                    InventoryMovement.source_upload_id == expense.source_upload_id,
                    InventoryMovement.source_row_ref == expense.source_row_ref,
                    InventoryMovement.product_id == expense.product_id,
                )
                .order_by(InventoryMovement.id)
            )
        ).scalars()
    )
    if not movimientos:
        return [], 0
    gastos_de_la_fila = (
        await db.execute(
            select(func.count(ExpenseEntry.id)).where(
                ExpenseEntry.tenant_id == expense.tenant_id,
                ExpenseEntry.voided_at.is_(None),
                ExpenseEntry.source_upload_id == expense.source_upload_id,
                ExpenseEntry.source_row_ref == expense.source_row_ref,
                ExpenseEntry.product_id == expense.product_id,
            )
        )
    ).scalar_one()
    return movimientos, int(gastos_de_la_fila)


async def _plan_de_stock_de_duplicados(
    db: AsyncSession,
    group: DuplicateGroup,
    filas: Sequence[SaleEntry | ExpenseEntry],
    *,
    bloquear: bool,
    efecto_previo: dict[uuid.UUID, int] | None = None,
) -> dict[uuid.UUID, InventoryMovement | None]:
    """T3 (E-2): el movimiento de stock que se anula con cada duplicado.

    Venta → su ``sale:{id}`` vivo (devolverlo repone el stock). Gasto de compra →
    su ``purchase`` vivo (anularlo baja el stock). Antes de tocar nada:

    - Una línea de compra (``_es_linea_de_compra``) sin su ``purchase`` (import
      viejo sin fila, compra a mano o del chat sin archivo), una fila con más de
      un gasto vivo y una cantidad distinta de compras, o varias compras
      distintas que el monto no distingue, no dicen cuál es de cuál: el grupo se
      saltea (``compra_sin_par_claro``). Una línea de compra NUNCA se anula
      dejando su movimiento vivo.
    - Si anular las compras deja stock negativo (esa mercadería ya se vendió),
      el grupo se saltea (``compra_ya_vendida``): nunca stock negativo.

    ``bloquear`` (apply): advisory shared + ``FOR UPDATE`` de los productos, y
    el chequeo se hace sobre el stock vigente. En dry-run se lee sin candado y
    ``efecto_previo`` trae lo que los grupos anteriores del MISMO dry-run ya le
    habrían sacado a cada producto (el apply lo ve en el stock), para que el
    preview saltee lo mismo que el apply.
    """
    plan: dict[uuid.UUID, InventoryMovement | None] = {}
    if bloquear:
        await maintenance_lock_service.acquire_write_lock_shared(db, group.tenant_id)
    if group.entity == "sale":
        for fila in filas:
            plan[fila.id] = await stock_service._live_sale_movement(
                fila.id, group.tenant_id, db
            )
        return plan

    usados: set[uuid.UUID] = set()
    for fila in filas:
        assert isinstance(fila, ExpenseEntry)
        movimientos, gastos_de_la_fila = await _compras_del_gasto(db, fila)
        if not movimientos:
            if _es_linea_de_compra(fila):
                raise GrupoNoReparableError(
                    MOTIVO_COMPRA_SIN_PAR_CLARO,
                    f"el gasto de compra {fila.id} no tiene una compra viva de su "
                    "archivo y fila",
                )
            plan[fila.id] = None
            continue
        if len(movimientos) != gastos_de_la_fila:
            raise GrupoNoReparableError(
                MOTIVO_COMPRA_SIN_PAR_CLARO,
                f"la fila {fila.source_row_ref} tiene {gastos_de_la_fila} gastos vivos "
                f"y {len(movimientos)} compras",
            )
        elegida = await _elegir_compra(db, fila, movimientos, usados)
        usados.add(elegida.id)
        plan[fila.id] = elegida

    salida: dict[uuid.UUID, int] = {}
    for mov in plan.values():
        if mov is not None:
            salida[mov.product_id] = salida.get(mov.product_id, 0) + mov.qty
    if not salida:
        return plan
    if bloquear:
        productos = await stock_service.bloquear_productos(
            db, group.tenant_id, salida, lock_held=True
        )
    else:
        productos = {}
        for pid in salida:
            producto = await db.get(Product, pid)
            if producto is not None:
                productos[pid] = producto
    for pid, qty in salida.items():
        producto = productos.get(pid)
        stock = int(producto.stock_units or 0) if producto is not None else 0
        if efecto_previo is not None:
            stock -= efecto_previo.get(pid, 0)
        if stock - qty < 0:
            raise GrupoNoReparableError(
                MOTIVO_COMPRA_YA_VENDIDA,
                f"anular la compra baja {qty} unidades y quedan {stock} del producto {pid}",
            )
    return plan


async def _transferir_identidades(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    entity: str,
    duplicados: list[uuid.UUID],
    keep_id: uuid.UUID,
) -> None:
    """Los vínculos de identidad de los duplicados pasan a la fila conservada.

    Si ``keep_id`` ya tiene el vínculo con esa identidad, el del duplicado se
    borra (la restricción única es identidad + entidad + id): nunca dos.
    """
    vinculos = list(
        (
            await db.execute(
                select(OperationIdentityLink).where(
                    OperationIdentityLink.tenant_id == tenant_id,
                    OperationIdentityLink.entity_type == entity,
                    OperationIdentityLink.entity_id.in_(duplicados),
                )
            )
        ).scalars()
    )
    if not vinculos:
        return
    ya_de_keep = set(
        (
            await db.execute(
                select(OperationIdentityLink.identity_id).where(
                    OperationIdentityLink.tenant_id == tenant_id,
                    OperationIdentityLink.entity_type == entity,
                    OperationIdentityLink.entity_id == keep_id,
                )
            )
        ).scalars()
    )
    for vinculo in vinculos:
        if vinculo.identity_id in ya_de_keep:
            await db.delete(vinculo)
        else:
            vinculo.entity_id = keep_id
            ya_de_keep.add(vinculo.identity_id)
    await db.flush()


async def _process_duplicate_group(
    db: AsyncSession,
    run: DataRepairRun,
    group: DuplicateGroup,
    dry_run: bool,
    result: RepairResult,
    planned_ids: frozenset[uuid.UUID] | None,
    efecto_previo: dict[uuid.UUID, int] | None = None,
) -> None:
    """Anula los duplicados del grupo devolviendo su stock y pasando su identidad.

    T3 (E-2): antes sólo marcaba ``voided_at``; el ``sale:{id}`` o el ``purchase``
    quedaban vivos (stock descontado por una venta que ya no existe, o inflado por
    una compra anulada) y la identidad de operación colgada. La identidad NO se
    suelta: la operación sigue viva en la fila conservada, así que sus vínculos
    pasan a ``keep_id`` (soltarla dejaba que un re-import la volviera a crear).
    El plan de stock
    (``_plan_de_stock_de_duplicados``) corre también en el dry-run, así que un
    grupo que se saltearía no deja ítems planificados.
    """
    now = datetime.now(UTC)
    # En apply con plan, solo tocar lo planificado en el dry-run.
    filas = [
        row for row in group.duplicates if planned_ids is None or row.id in planned_ids
    ]
    plan = await _plan_de_stock_de_duplicados(
        db, group, filas, bloquear=not dry_run, efecto_previo=efecto_previo
    )
    for row in filas:
        movimiento = plan.get(row.id)
        if not dry_run:
            if movimiento is not None:
                await stock_service.void_movement(movimiento, db, lock_held=True)
            row.voided_at = now
            row.void_reason = VOID_REASON_DUPLICATE
            row.voided_by_repair_run_id = run.id
            # Contar según la entidad real (no mezclar gastos duplicados con ventas).
            if group.entity == "expense":
                result.expenses_voided += 1
            else:
                result.sales_voided += 1
        item_kwargs: dict[str, Any] = {
            "id": uuid.uuid4(),
            "run_id": run.id,
            "tenant_id": group.tenant_id,
            "action": "VOID_DUPLICATE",
            "before_json": {**_dup_snapshot(row, group.entity), "planned": dry_run},
            "after_json": {
                "kept_id": str(group.keep_id),
                "planned": dry_run,
                # Para poder revertir: el movimiento anulado y su efecto.
                "movement_id": str(movimiento.id) if movimiento is not None else None,
                "movement_qty": movimiento.qty if movimiento is not None else None,
            },
            "confidence": group.confidence,
            "created_at": now,
        }
        if group.entity == "sale":
            item_kwargs["sale_entry_id"] = row.id
        if movimiento is not None:
            item_kwargs["product_id"] = movimiento.product_id
        db.add(DataRepairItem(**item_kwargs))
    if not dry_run and filas:
        await _transferir_identidades(
            db, group.tenant_id, group.entity, [row.id for row in filas], group.keep_id
        )
    if efecto_previo is not None:
        for mov in plan.values():
            if mov is not None:
                efecto_previo[mov.product_id] = efecto_previo.get(mov.product_id, 0) + mov.qty
    result.tenant_results.append(
        {
            "tenant_id": str(group.tenant_id),
            "entity": group.entity,
            "status": "DUPLICATES_VOIDED" if not dry_run else "DUPLICATES_PLANNED",
            "kept_id": str(group.keep_id),
            "voided": len(filas),
        }
    )
    await db.flush()


# ── B2 Detector 2: MISCLASIFICACIÓN OPEX→COGS (vertical-aware) ─────────────────


async def _load_vertical_by_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
) -> dict[uuid.UUID, Vertical]:
    """Vertical de cada tenant (una query) para clasificar gastos en lote.

    Los tenants cuyo `vertical_code` no es canónico quedan FUERA del dict: sus
    gastos se saltean en vez de clasificarse con el catálogo de otro rubro.
    """
    q = select(BusinessProfile.tenant_id, BusinessProfile.vertical_code)
    if tenant_id is not None:
        q = q.where(BusinessProfile.tenant_id == tenant_id)
    rows = (await db.execute(q)).all()
    verticals: dict[uuid.UUID, Vertical] = {}
    for row in rows:
        vertical = try_parse_vertical(row[1])
        if vertical is None:
            logger.warning(
                "data_repair.unknown_vertical", tenant_id=str(row[0]), vertical_code=row[1]
            )
            continue
        verticals[row[0]] = vertical
    return verticals


def _expense_match_text(expense: ExpenseEntry) -> str | None:
    """Texto a clasificar: category_label custom, descripción o categoría."""
    label = (expense.custom_fields or {}).get("category_label")
    return label or expense.description or expense.category or None


async def detect_misclassified_opex_to_cogs(
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
) -> list[MisclassifiedExpenseCandidate]:
    """Gastos OPEX cuyo texto matchea producto vendible del vertical → deben ser COGS.

    Usa ``classify_expense_with_vertical``: si reconoce el texto como mercadería
    del vertical devuelve ``("INVENTORY", label, True)``. Gastos operativos
    genuinos (alquiler, servicios, sueldos) NO matchean y quedan OPEX.
    """
    verticals = await _load_vertical_by_tenant(db, tenant_id)

    exp_q = select(ExpenseEntry).where(
        ExpenseEntry.provenance == "REAL",
        ExpenseEntry.voided_at.is_(None),
        ExpenseEntry.expense_type == "OPEX",
    )
    if tenant_id is not None:
        exp_q = exp_q.where(ExpenseEntry.tenant_id == tenant_id)
    expenses = list((await db.execute(exp_q)).scalars().all())

    candidates: list[MisclassifiedExpenseCandidate] = []
    for expense in expenses:
        text_to_match = _expense_match_text(expense)
        if not text_to_match:
            continue
        vertical = verticals.get(expense.tenant_id)
        if vertical is None:
            continue
        code, label, is_merch = classify_expense_with_vertical(text_to_match, vertical)
        if not is_merch or code != "INVENTORY":
            continue
        candidates.append(
            MisclassifiedExpenseCandidate(
                tenant_id=expense.tenant_id,
                expense=expense,
                matched_text=text_to_match,
                new_category="INVENTORY",
                new_label=label,
                confidence="HIGH",
            )
        )

    logger.info(
        "data_repair.detect_misclassified_opex.completed",
        candidates=len(candidates),
        tenant_id=str(tenant_id) if tenant_id else "all",
    )
    return candidates


async def _process_misclassified_expense(
    db: AsyncSession,
    run: DataRepairRun,
    candidate: MisclassifiedExpenseCandidate,
    dry_run: bool,
    result: RepairResult,
    planned_ids: frozenset[uuid.UUID] | None,
) -> None:
    now = datetime.now(UTC)
    expense = candidate.expense
    if planned_ids is not None and expense.id not in planned_ids:
        return

    before = {
        "id": str(expense.id),
        "category": expense.category,
        "expense_type": expense.expense_type,
        "product_id": str(expense.product_id) if expense.product_id else None,
    }

    new_product_id: uuid.UUID | None = None
    product_was_created = False
    if not dry_run:
        expense.category = candidate.new_category
        expense.expense_type = infer_expense_type(
            candidate.new_category, product_id=expense.product_id
        )
        if candidate.new_label:
            cf = dict(expense.custom_fields or {})
            cf["category_label"] = candidate.new_label
            expense.custom_fields = cf
        # Crear/vincular producto incompleto SOLO con nombre + cantidad suficiente
        # (qty>0). Sin cantidad, no se crea producto basura (gate del plan).
        if expense.product_id is None:
            qty = _parse_qty((expense.custom_fields or {}).get("quantity"))
            name = candidate.new_label or candidate.matched_text
            if qty > 0 and name:
                new_product_id, product_was_created = await build_incomplete_product(
                    db, candidate.tenant_id, name, None, None
                )
                if new_product_id is not None:
                    expense.product_id = new_product_id
                    expense.expense_type = "COGS"
        # Reclasificación de gasto OPEX→COGS: contar como reclasificación, y solo
        # como producto creado si efectivamente se CREÓ uno. F5-A: el helper puede
        # devolver un producto preexistente (el índice único resolvió una carrera);
        # contarlo como creado inflaría el informe de la reparación.
        result.expenses_reclassified += 1
        if new_product_id is not None and product_was_created:
            result.products_created += 1

    after = {
        "category": candidate.new_category,
        "expense_type": "COGS",
        "matched_text": candidate.matched_text,
        "new_product_id": str(new_product_id) if new_product_id else None,
        "planned": dry_run,
    }
    db.add(
        DataRepairItem(
            id=uuid.uuid4(),
            run_id=run.id,
            tenant_id=candidate.tenant_id,
            product_id=new_product_id,
            action="RECLASSIFY_EXPENSE",
            before_json={**before, "planned": dry_run},
            after_json=after,
            confidence=candidate.confidence,
            created_at=now,
        )
    )
    result.tenant_results.append(
        {
            "tenant_id": str(candidate.tenant_id),
            "expense_id": str(expense.id),
            "status": "RECLASSIFIED" if not dry_run else "RECLASSIFY_PLANNED",
            "matched_text": candidate.matched_text,
            "from": before["category"],
            "to": candidate.new_category,
        }
    )
    await db.flush()


async def _load_planned_ids_by_action(
    db: AsyncSession,
    source_run_id: uuid.UUID,
    action: str,
    id_field: str,
) -> frozenset[uuid.UUID] | None:
    """IDs (sale_entry_id o derivados del before_json) planificados para una acción.

    Para ``VOID_DUPLICATE`` (ventas usan sale_entry_id; gastos viven en
    before_json["id"]) y ``RECLASSIFY_EXPENSE`` (before_json["id"]) leemos el id
    desde el campo indicado. Devuelve None si el run fuente no existe / no es dry-run.
    """
    source_run = await db.get(DataRepairRun, source_run_id)
    if source_run is None or not source_run.dry_run:
        return None
    items = (
        await db.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == source_run_id,
                DataRepairItem.action == action,
            )
        )
    ).scalars().all()
    ids: set[uuid.UUID] = set()
    for item in items:
        if id_field == "sale_entry_id" and item.sale_entry_id is not None:
            ids.add(item.sale_entry_id)
        raw = (item.before_json or {}).get("id")
        if raw:
            try:
                ids.add(uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                continue
    return frozenset(ids)
