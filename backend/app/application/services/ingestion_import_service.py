"""Shared insertion logic for confirmed parsed ingestion summaries."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.cash_service import normalize_payment_method
from app.application.services.file_parsing import FECHA_COLS as _FECHA_COLS
from app.application.services.file_parsing import GASTO_COLS as _GASTO_COLS
from app.application.services.file_parsing import VENTA_COLS as _VENTA_COLS
from app.domain.expense_categories import (
    classify_expense_with_vertical,
    infer_expense_type,
)
from app.domain.product_categories import normalize_product_category
from app.observability.logger import get_logger

logger = get_logger(__name__)


class EmptyImportError(Exception):
    """Se confirmó un import con datos presentes pero no se insertó ninguna fila."""

    user_message = (
        "No se importó ninguna fila: no se detectaron automáticamente las "
        "columnas requeridas (fecha / monto / nombre) para el tipo confirmado. "
        "Mapeá las columnas manualmente o revisá el tipo de datos del archivo."
    )


def check_nonempty_import(
    counts: dict[str, Any],
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None,
    context_confirmed: dict[str, bool] | None = None,
) -> None:
    """Falla explícita ante inserción vacía con datos presentes.

    Si el archivo tenía filas y el usuario confirmó algún tipo pero NO se insertó
    nada (el fallback por keyword no encontró las columnas requeridas), lanza
    ``EmptyImportError`` en vez de dejar pasar un import silenciosamente vacío.
    Compartida por el endpoint de ingestión (→ 422) y el camino de chat
    (IMPORT_TABULAR_FILE → la pending action queda FAILED con mensaje visible).
    """
    # NOTA: counts["otros"] NO cuenta como insertado. Si el usuario confirmó un
    # tipo y ese tipo importó 0 filas, hay que cortar con error (y el rollback
    # descarta también lo capturado a "Otros") para que pueda reintentar con
    # mapeo manual — si "otros" sumara, una hoja no clasificable taparía la
    # pérdida silenciosa de los datos confirmados.
    total_inserted = (
        counts.get("ventas", 0) + counts.get("gastos", 0) + counts.get("productos", 0)
    )
    had_rows = bool(summary.get("row_count")) or any(
        summary.get(k)
        for k in (
            "ventas_detectadas",
            "gastos_detectados",
            "stock_detectado",
            "otros_detectados",
        )
    )
    confirmed_any = any((confirmed_fields or {}).values()) or any(
        (context_confirmed or {}).values()
    )
    if total_inserted == 0 and had_rows and confirmed_any:
        logger.warning(
            "ingestion.import.zero_inserted",
            row_count=summary.get("row_count"),
            confirmed_fields=confirmed_fields,
        )
        raise EmptyImportError(EmptyImportError.user_message)


def _normalize_name(name: str) -> str:
    """Normaliza nombre de producto para comparación: lower, sin guiones, espacios únicos."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", name.strip().lower()))


def _capture_unclassified(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict[str, Any]],
    headers: list[str] | None,
    source: str,
    uploaded_file_id: uuid.UUID | None,
    context_label: str | None = None,
    suggested_entity: str | None = None,
) -> int:
    """FASE F: persiste filas no clasificadas en la bandeja "Otros".

    Nada se descarta en silencio: lo que no se pudo (o no se quiso) clasificar
    como venta/gasto/producto queda en ``unclassified_records`` con estado
    PENDING para que el tenant lo importe o descarte desde /otros.
    """
    from app.persistence.models.unclassified_record import (  # noqa: PLC0415
        UnclassifiedRecord,
    )

    count = 0
    for row in rows:
        row_data = {k: v for k, v in row.items() if k != "__context__"}
        if not row_data:
            continue
        session.add(
            UnclassifiedRecord(
                tenant_id=tenant_id,
                uploaded_file_id=uploaded_file_id,
                source=source,
                context_label=(context_label or None),
                headers=list(headers) if headers else None,
                row_data={k: ("" if v is None else str(v)) for k, v in row_data.items()},
                suggested_entity=suggested_entity,
            )
        )
        count += 1
    return count


async def _load_business_type(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """Vertical del tenant para normalizar categorías de producto (1 query por import)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.business import BusinessProfile  # noqa: PLC0415

    result = await session.execute(
        select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


# ── FASE 3: vínculo de entidades (ventas/gastos → producto del catálogo) ──────


async def _load_product_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID | None]]:
    """Carga el catálogo del tenant UNA vez para vincular transacciones en memoria.

    Evita N queries (una por fila). Devuelve `(by_sku, by_name)`:
    - `by_sku[sku_lower] = product_id`
    - `by_name[norm_name] = product_id` o `None` si el nombre normalizado es
      ambiguo (varios productos lo comparten → no se vincula).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415

    result = await session.execute(
        select(Product.id, Product.name, Product.sku).where(Product.tenant_id == tenant_id)
    )
    by_sku: dict[str, uuid.UUID] = {}
    by_name: dict[str, uuid.UUID | None] = {}
    for pid, pname, psku in result.all():
        if psku:
            by_sku[str(psku).strip().lower()] = pid
        norm = _normalize_name(pname or "")
        if norm:
            by_name[norm] = pid if norm not in by_name else None  # None = ambiguo
    return by_sku, by_name


def _resolve_product(
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    name: str | None,
    sku: str | None,
) -> uuid.UUID | None:
    """Resuelve product_id desde el índice en memoria. SKU exacto gana; luego nombre."""
    if sku:
        hit = by_sku.get(str(sku).strip().lower())
        if hit:
            return hit
    if name:
        norm = _normalize_name(str(name))
        if norm:
            return by_name.get(norm)  # None si no existe o es ambiguo
    return None


async def _load_balance_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[uuid.UUID, Any]:
    """Carga TODOS los InventoryBalance del tenant en una query (batch).

    Evita un SELECT por fila en imports con movimientos de stock: el balance
    se busca/actualiza en memoria y los nuevos se registran en el índice.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import InventoryBalance  # noqa: PLC0415

    result = await session.execute(
        select(InventoryBalance).where(InventoryBalance.tenant_id == tenant_id)
    )
    return {b.product_id: b for b in result.scalars().all()}


async def _record_stock_movement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    qty: int,
    unit_cost: Decimal | None,
    movement_type: str,
    final_qty: int,
    balance_index: dict[uuid.UUID, Any] | None = None,
) -> None:
    """FASE 3: registra un InventoryMovement (audit insert-only) y sincroniza el
    `InventoryBalance` por el import, dentro de la misma transacción.

    `Product.stock_units` sigue siendo la representación canónica que lee la UI; el
    import la setea. Esta función:
      1. Inserta el movimiento de inventario (historial, antes vacío al importar).
      2. Sincroniza `InventoryBalance.current_qty`: `+= qty` (delta) si el balance
         existe; lo crea en `final_qty` (el stock total tras el import) si no, para
         mantenerlo consistente con `Product.stock_units`.

    NO hace flush ni emite EventBus por fila (a diferencia de
    `stock_service.increment_stock`): en un import masivo eso sería caro/ruidoso;
    todo entra en el commit batch del import. `qty>0` = ingreso, `qty<0` = ajuste.
    `qty==0` → no-op (ni movimiento ni balance).
    """
    if qty == 0:
        return
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.inventory import (  # noqa: PLC0415
        InventoryBalance,
        InventoryMovement,
    )

    session.add(
        InventoryMovement(
            tenant_id=tenant_id,
            product_id=product_id,
            movement_type=movement_type,
            qty=qty,
            unit_cost=unit_cost,
            source_event_id="import",
            reason="Importado desde archivo",
        )
    )

    if balance_index is not None:
        balance = balance_index.get(product_id)
    else:
        balance = (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.product_id == product_id,
                    InventoryBalance.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if balance is None:
        new_balance = InventoryBalance(
            tenant_id=tenant_id,
            product_id=product_id,
            current_qty=final_qty,
            reserved_qty=0,
        )
        session.add(new_balance)
        if balance_index is not None:
            balance_index[product_id] = new_balance
    else:
        balance.current_qty += qty


async def _apply_purchase_to_stock(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    expense: Any,
    qty_raw: Any,
    unit_cost: Decimal | None,
    balance_index: dict[uuid.UUID, Any] | None = None,
) -> None:
    """FASE D: una compra de mercadería importada suma stock.

    Gate doble contra falsos positivos / doble conteo:
      - el gasto debe tener producto del catálogo resuelto (`product_id`); y
      - la fila debe traer una columna de cantidad con valor > 0.
    A diferencia del import de productos (que SETEA stock absoluto), acá se
    INCREMENTA: `Product.stock_units += qty` + movimiento de inventario + sync
    de balance, igual que `stock_service.increment_stock` pero sin flush por
    fila (batch del import).
    """
    if expense.product_id is None:
        return
    try:
        qty = int(float(str(qty_raw))) if qty_raw not in (None, "", "None", "nan") else 0
    except (ValueError, TypeError):
        return
    if qty <= 0:
        return
    from app.persistence.models.product import Product  # noqa: PLC0415

    product = await session.get(Product, expense.product_id)
    if product is None or product.tenant_id != tenant_id:
        return
    product.stock_units += qty
    if unit_cost is not None:
        product.unit_cost_ars = unit_cost
    await _record_stock_movement(
        session,
        tenant_id,
        product.id,
        qty,
        unit_cost,
        "purchase",
        product.stock_units,
        balance_index=balance_index,
    )


_NOMBRE_COLS: set[str] = {
    "producto",
    "descripcion",
    "descripción",
    "nombre",
    "articulo",
    "artículo",
    "item",
    "name",
    "concepto",
    "detalle",
    # FASE 3: columnas de compra de mercadería sirven como nombre del producto.
    "mercaderia",
    "mercadería",
    "insumo",
    "insumos",
}
_PRECIO_VENTA_COLS: set[str] = {
    "precio_venta", "precio", "price", "p_venta",
    "precio_unitario",  # common in Argentine business files
    "precio_unit",
}
_COSTO_COLS: set[str] = {
    "costo", "cost", "precio_costo", "p_costo",
    "costo_unitario",  # common in purchase sheets
    "costo_unit",
}
# Solo nombres INEQUÍVOCOS de costo unitario: "costo" a secas suele ser el
# total de la línea en archivos de gastos (también matchea _GASTO_COLS) y no
# debe escribirse como unit_cost del producto.
_COSTO_UNITARIO_COLS: set[str] = {
    "costo_unitario", "costo_unit", "precio_costo", "p_costo", "unit_cost",
}
_STOCK_COLS: set[str] = {
    "stock", "cantidad", "inventario", "units", "qty", "existencia", "stock_actual",
}
_SKU_COLS: set[str] = {"sku", "codigo", "código", "code", "ref", "id_producto"}

# Columnas de monto de venta ampliadas para archivos multi-hoja
_VENTA_AMOUNT_COLS: set[str] = _VENTA_COLS | {"total", "importe_total", "precio_unitario"}
# Columnas de monto de gasto ampliadas
_GASTO_AMOUNT_COLS: set[str] = _GASTO_COLS | {"monto", "total", "importe"}


def _parse_amount(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    s = re.sub(r"[$\s]", "", str(raw).strip())
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = Decimal(s)
        if val <= 0:
            logger.debug("ingestion.parse.amount_discarded", raw=str(raw), reason="non_positive")
            return None
        return val
    except InvalidOperation:
        return None


def _parse_date(raw: Any) -> datetime | None:
    # transaction_date es timestamp: se intenta primero con hora (ISO o dd/mm con
    # hora) y, si no, solo fecha (queda a medianoche).
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # ISO 8601: "2026-06-05T14:30:00", "2026-06-05 14:30:00", "2026-06-05".
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    formats = (
        # con hora (probar antes que las de solo fecha)
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        # solo fecha
        "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%m/%d/%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _find_col(headers: list[str], keywords: set[str] | tuple[str, ...]) -> str | None:
    """Si `keywords` es tupla, se respeta su orden como PRIORIDAD (el primer
    keyword que matchee alguna columna gana, sin importar el orden de headers).
    Con set, gana la primera columna en orden de archivo (comportamiento legacy).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for h in headers:
                if k in h.lower().strip().replace(" ", "_"):
                    return h
        return None
    for h in headers:
        norm = h.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return h
    return None


def _row_col(row: dict[str, Any], keywords: set[str] | tuple[str, ...]) -> str | None:
    """Como `_row_val` pero devuelve el NOMBRE de la columna que matchea.

    Necesario cuando hay que comparar identidad de columnas (ej: no usar la
    misma columna como monto del gasto Y costo unitario del producto).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for key in row:
                if k in key.lower().strip().replace(" ", "_"):
                    return key
        return None
    for key in row:
        norm = key.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return key
    return None


def _row_val_categoria(row: dict[str, Any]) -> Any:
    """Valor de la columna de categoría por keyword, salteando columnas de pago.

    El keyword 'tipo' matchearía 'tipo_pago' por substring y la categoría
    terminaría siendo el método de pago — una columna de pago nunca es la
    categoría del gasto.
    """
    for k in _CATEGORIA_COLS:
        for key, val in row.items():
            norm = key.lower().strip().replace(" ", "_")
            if k in norm and "pago" not in norm and "payment" not in norm:
                return val
    return None


def _row_val(row: dict[str, Any], keywords: set[str] | tuple[str, ...]) -> Any:
    """Devuelve el valor de la primera columna de *esta* fila cuyo nombre matchea.

    A diferencia de `_find_col` (que resuelve una columna fija para todo el
    bucket a partir de la primera fila), esto resuelve por fila. Necesario en
    archivos multi-hoja donde varias hojas del mismo tipo pueden tener esquemas
    de columnas distintos: sin esto, las filas de la segunda hoja se descartaban
    en silencio porque la columna detectada no existía en sus keys.

    Con tupla, el orden de keywords es prioridad (ver `_find_col`).
    """
    if isinstance(keywords, tuple):
        for k in keywords:
            for key, val in row.items():
                if k in key.lower().strip().replace(" ", "_"):
                    return val
        return None
    for key, val in row.items():
        norm = key.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return val
    return None


def _resolve_target_cols(mapping: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Desde un mapeo explícito source_col→target, resuelve columnas por target canónico.

    Devuelve (target_to_col, custom_field_cols): el primero mapea campo canónico
    (amount, transaction_date, name, sale_price_ars, ...) → source_col; el segundo
    mapea cf_key → source_col para los targets `custom_field:{key}`. Ignora "ignore".
    """
    target_to_col: dict[str, str] = {}
    custom_field_cols: dict[str, str] = {}
    for src_col, target in mapping.items():
        if target == "ignore":
            continue
        if target.startswith("custom_field:"):
            custom_field_cols[target[len("custom_field:"):]] = src_col
        elif target not in target_to_col:
            target_to_col[target] = src_col
    return target_to_col, custom_field_cols


async def insert_confirmed_data(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None = None,
    return_details: bool = False,
    column_mappings: dict[str, str] | None = None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    context_entity: dict[str, str] | None = None,
    source: str = "ingestion",
    uploaded_file_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Parse parsed_summary_json and insert rows into sales/expense/product tables.

    When return_details=True, also returns product_details list with per-row
    action ('CREATED'|'UPDATED'), product_id, name, before/after snapshots.

    FASE F: las filas de `otros_detectados` que el usuario NO reasigna a un tipo
    importable se persisten en `unclassified_records` (bandeja "Otros") con
    ``source``/``uploaded_file_id`` — `counts["otros"]` las cuenta.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or default_confirmed_fields(summary)
    # Fallback de fecha para filas sin fecha: ahora (captura hora del import).
    today = datetime.now()
    counts: dict[str, Any] = {"ventas": 0, "gastos": 0, "productos": 0, "otros": 0}
    product_details: list[dict[str, Any]] = []
    file_type = summary.get("file_type", "spreadsheet")

    if file_type == "spreadsheet":
        inferred_type = summary.get("inferred_type", "general")

        # ── Archivos multi-hoja: delegar a helper que procesa cada tipo por separado ──
        if inferred_type == "mixed" or summary.get("multi_sheet"):
            return await _insert_multisheet_data(
                session=session,
                tenant_id=tenant_id,
                summary=summary,
                confirmed_fields=confirmed_fields,
                today=today,
                return_details=return_details,
                product_details=product_details,
                counts=counts,
                column_mappings=column_mappings,
                context_mappings=context_mappings,
                context_confirmed=context_confirmed,
                context_entity=context_entity,
                source=source,
                uploaded_file_id=uploaded_file_id,
            )

        rows: list[dict[str, Any]]
        rows_from_otros = False
        if inferred_type == "stock":
            rows = summary.get("stock_detectado", [])
        else:
            rows = summary.get("ventas_detectadas", []) or summary.get("gastos_detectados", [])
            if not rows:
                # FASE F: archivos ambiguos ("general") viven en otros_detectados;
                # la confirmación explícita del usuario sigue pudiendo importarlos.
                rows = summary.get("otros_detectados", [])
                rows_from_otros = bool(rows)
        if not rows:
            return counts

        headers = list(rows[0].keys())
        fecha_col = _find_col(headers, _FECHA_COLS)
        # Usar set ampliado para columnas de monto (ej: "precio_unitario", "total")
        venta_col = _find_col(headers, _VENTA_AMOUNT_COLS)
        gasto_col = _find_col(headers, _GASTO_AMOUNT_COLS)
        nombre_col = _find_col(headers, _NOMBRE_COLS)
        precio_col = _find_col(headers, _PRECIO_VENTA_COLS)
        costo_col = _find_col(headers, _COSTO_COLS)
        stock_col = _find_col(headers, _STOCK_COLS)
        sku_col = _find_col(headers, _SKU_COLS)

        # Columnas extra (solo disponibles con column_mappings explícitos)
        qty_col: str | None = None
        notes_col: str | None = None
        payment_col: str | None = None
        category_col: str | None = None
        recurring_col: str | None = None
        custom_field_cols: dict[str, str] = {}

        if column_mappings:
            # Construir lookup: target_field → primer source_col que lo mapee
            target_to_col: dict[str, str] = {}
            for src_col, target in column_mappings.items():
                if target == "ignore":
                    continue
                if target.startswith("custom_field:"):
                    cf_key = target[len("custom_field:"):]
                    custom_field_cols[cf_key] = src_col
                else:
                    if target not in target_to_col:
                        target_to_col[target] = src_col

            # Remapear columnas de fecha y monto usando el mapeo explícito
            fecha_col = (
                target_to_col.get("transaction_date")
                or target_to_col.get("expense_date")
                or fecha_col
            )
            if inferred_type != "stock" and "amount" in target_to_col:
                venta_col = target_to_col["amount"]
                gasto_col = target_to_col["amount"]
            nombre_col = (
                target_to_col.get("product_name")
                or target_to_col.get("name")
                or nombre_col
            )
            precio_col = target_to_col.get("sale_price_ars") or precio_col
            costo_col = target_to_col.get("unit_cost_ars") or costo_col
            stock_col = target_to_col.get("stock_units") or stock_col
            sku_col = target_to_col.get("sku") or sku_col

            # Campos extra solo disponibles con mapeo explícito
            qty_col = target_to_col.get("quantity")
            notes_col = target_to_col.get("notes")
            payment_col = target_to_col.get("payment_method")
            category_col = target_to_col.get("category")
            recurring_col = target_to_col.get("is_recurring")

        # FASE 3: en archivos ambiguos ("general") se honra la confirmación EXPLÍCITA del
        # usuario (no se requiere la señal auto-detectada). Para tipos ya inferidos se
        # mantiene la guardia original.
        wants_ventas = bool(
            inferred_type != "stock"
            and confirmed_fields.get("ventas")
            and (summary.get("has_venta") or inferred_type in ("ventas", "general"))
            and venta_col
        )
        wants_gastos = bool(
            inferred_type != "stock"
            and confirmed_fields.get("gastos")
            and (summary.get("has_gasto") or inferred_type in ("gastos", "general"))
            and gasto_col
        )
        wants_productos = bool(
            confirmed_fields.get("productos")
            and (summary.get("has_producto") or inferred_type == "stock")
            and nombre_col
        )

        # FASE 3: índice de catálogo en memoria para vincular ventas y gastos
        # (B1) al producto. Una sola carga; vacío si no hay columnas de nombre/sku.
        _by_sku, _by_name = (
            await _load_product_index(session, tenant_id)
            if (wants_ventas or wants_gastos) and (nombre_col or sku_col)
            else ({}, {})
        )
        # FASE E: vertical del tenant para normalizar categorías de producto.
        # También para gastos: una categoría que matchea el catálogo de productos
        # del vertical es compra de mercadería → INVENTORY/COGS.
        _business_type = (
            await _load_business_type(session, tenant_id)
            if (wants_productos or wants_gastos)
            else None
        )
        # Batch: balances del tenant en una query (evita un SELECT por fila
        # en los movimientos de stock del import).
        _balance_index: dict[uuid.UUID, Any] | None = (
            await _load_balance_index(session, tenant_id)
            if (wants_productos or wants_gastos)
            else None
        )

        for row_index, row in enumerate(rows):
            raw_date = row.get(fecha_col) if fecha_col else None
            tx_date = _parse_date(raw_date) if fecha_col else None
            if tx_date is None:
                if fecha_col:
                    logger.debug(
                        "ingestion.parse.date_fallback_today",
                        raw=str(raw_date),
                        row_index=row_index,
                    )
                tx_date = today

            if wants_ventas:
                assert venta_col is not None  # wants_ventas implica venta_col presente
                amount = _parse_amount(row.get(venta_col))
                if amount:
                    # Cantidad
                    qty_raw = row.get(qty_col) if qty_col else None
                    qty: int = 1
                    if qty_raw not in (None, "", "None", "nan"):
                        try:
                            qty = int(float(str(qty_raw)))
                        except (ValueError, TypeError):
                            qty = 1

                    # Notas
                    notes_raw = row.get(notes_col) if notes_col else None
                    notes_str = (
                        str(notes_raw).strip()[:499]
                        if notes_raw and str(notes_raw).strip() not in {"None", "nan", ""}
                        else "Importado desde archivo"
                    )

                    # Método de pago: canónico (antes se guardaba el texto crudo
                    # del archivo — "efectivo", "mercadopago" — y los filtros
                    # quedaban inconsistentes con los registros manuales).
                    pay_raw = _clean_str(
                        row.get(payment_col) if payment_col else _row_val(row, _PAGO_COLS),
                        50,
                    )
                    pay_str = normalize_payment_method(pay_raw) if pay_raw else "cash"

                    # Custom fields
                    cf: dict[str, str] = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }

                    entry = SaleEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        quantity=qty,
                        transaction_date=tx_date,
                        payment_method=pay_str,
                        notes=notes_str,
                        provenance="REAL",
                    )
                    if cf:
                        entry.custom_fields = cf
                    # FASE 3: vincular al producto del catálogo (por SKU/nombre de la fila).
                    entry.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        row.get(nombre_col) if nombre_col else None,
                        row.get(sku_col) if sku_col else None,
                    )
                    session.add(entry)
                    counts["ventas"] += 1

            if wants_gastos:
                assert gasto_col is not None  # wants_gastos implica gasto_col presente
                amount = _parse_amount(row.get(gasto_col))
                if amount:
                    desc_raw = row.get(nombre_col) if nombre_col else None
                    notes_raw = row.get(notes_col) if notes_col else None
                    desc = (
                        str(notes_raw or desc_raw or "").strip()[:499]
                        or "Gasto importado"
                    )
                    # Categoría: columna mapeada explícita o detección por keyword
                    # (antes, sin mapeo explícito todo caía a "importado").
                    # _row_val_categoria saltea columnas de pago (tipo_pago ≠ categoría).
                    cat_raw = (
                        row.get(category_col)
                        if category_col
                        else _row_val_categoria(row)
                    )
                    # Categoría de producto del vertical ("Bebidas") = compra de
                    # mercadería → INVENTORY/COGS, preservando el texto como label.
                    cat_code, cat_label, _ = classify_expense_with_vertical(
                        _clean_str(cat_raw), _business_type
                    )

                    # Método de pago real del archivo (antes hardcodeado "transfer").
                    exp_pay_raw = _clean_str(
                        row.get(payment_col) if payment_col else _row_val(row, _PAGO_COLS),
                        30,
                    )
                    exp_pay = (
                        normalize_payment_method(exp_pay_raw) if exp_pay_raw else "transfer"
                    )
                    recurring = _parse_bool_es(
                        row.get(recurring_col) if recurring_col else _row_val(row, _RECURRENTE_COLS)
                    )

                    # Custom fields
                    cf = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }
                    if cat_label:
                        cf = {**cf, "category_label": cat_label}

                    expense = ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category=cat_code,
                        transaction_date=tx_date,
                        description=desc,
                        is_recurring=recurring if recurring is not None else False,
                        payment_method=exp_pay,
                        provenance="REAL",
                    )
                    if cf:
                        expense.custom_fields = cf
                    # FASE 3 (B1): vincular el gasto al producto del catálogo.
                    expense.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        str(row.get(nombre_col)) if nombre_col else None,
                        str(row.get(sku_col)) if sku_col else None,
                    )
                    # FASE D: COGS si la fila es compra de mercadería (producto
                    # del catálogo o categoría INVENTORY); además suma stock si
                    # trae cantidad explícita.
                    expense.expense_type = infer_expense_type(
                        cat_code, product_id=expense.product_id
                    )
                    exp_qty_raw = (
                        row.get(qty_col) if qty_col else _row_val(row, _CANTIDAD_COLS)
                    )
                    # Costo unitario: solo de una columna inequívoca y DISTINTA
                    # de la del monto — "costo" suele ser el total de la línea y
                    # escribirlo como unit_cost corrompería el margen.
                    exp_cost_col = (
                        target_to_col.get("unit_cost_ars")
                        if column_mappings
                        else _find_col(headers, _COSTO_UNITARIO_COLS)
                    )
                    exp_unit_cost = (
                        _parse_amount(row.get(exp_cost_col))
                        if exp_cost_col and exp_cost_col != gasto_col
                        else None
                    )
                    await _apply_purchase_to_stock(
                        session,
                        tenant_id,
                        expense,
                        exp_qty_raw,
                        exp_unit_cost,
                        balance_index=_balance_index,
                    )
                    session.add(expense)
                    counts["gastos"] += 1

        if wants_productos:
            assert nombre_col is not None  # wants_productos implica nombre_col presente
            for row in rows:
                name = str(row.get(nombre_col, "")).strip()[:299]
                if not name or name.lower() in {"none", "nan", ""}:
                    continue
                price = _parse_amount(row.get(precio_col)) if precio_col else None
                cost = _parse_amount(row.get(costo_col)) if costo_col else None
                try:
                    stock_raw = row.get(stock_col) if stock_col else None
                    stock_val = (
                        int(float(str(stock_raw)))
                        if stock_raw not in (None, "", "None", "nan")
                        else 0
                    )
                except (ValueError, TypeError):
                    stock_val = 0
                sku_raw = row.get(sku_col) if sku_col else None
                sku = (
                    str(sku_raw).strip()[:99]
                    if sku_raw and str(sku_raw).strip() not in {"", "None", "nan"}
                    else None
                )
                # FASE E: categoría canónica del vertical (antes se ignoraba en
                # este path). Sin columna de categoría → None (sin categoría).
                prod_cat_raw = _clean_str(
                    row.get(category_col) if category_col else _row_val_categoria(row),
                    99,
                )
                prod_cat: str | None = None
                prod_cat_label: str | None = None
                if prod_cat_raw:
                    prod_cat, prod_cat_label = normalize_product_category(
                        prod_cat_raw, _business_type
                    )

                # Buscar por nombre normalizado: primero exacto case-insensitive,
                # después normalización Python completa (cubre "Coca-Cola" vs "Coca Cola"
                # en cualquier dirección: importado con guión, existente sin guión, o viceversa).
                result = await session.execute(
                    select(Product).where(
                        Product.tenant_id == tenant_id,
                        func.lower(func.trim(Product.name)) == name.lower(),
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    # Fallback: normalizar ambos lados en Python para capturar variantes
                    # con guiones, underscores o espacios múltiples en cualquier sentido.
                    all_result = await session.execute(
                        select(Product).where(Product.tenant_id == tenant_id)
                    )
                    normalized_input = _normalize_name(name)
                    for prod in all_result.scalars().all():
                        if _normalize_name(prod.name) == normalized_input:
                            existing = prod
                            break
                if existing:
                    before_snap: dict[str, Any] | None = None
                    if return_details:
                        before_snap = {
                            "sale_price_ars": str(existing.sale_price_ars),
                            "stock_units": existing.stock_units,
                        }
                    if price:
                        existing.sale_price_ars = price
                    if cost:
                        existing.unit_cost_ars = cost
                    if stock_val > 0:
                        _delta = stock_val - existing.stock_units
                        existing.stock_units = stock_val
                        # FASE 3: audit del cambio de stock + sync de balance.
                        await _record_stock_movement(
                            session,
                            tenant_id,
                            existing.id,
                            _delta,
                            cost,
                            "purchase" if _delta > 0 else "adjustment",
                            stock_val,
                            balance_index=_balance_index,
                        )
                    if sku:
                        existing.sku = sku
                    if prod_cat and not existing.category:
                        existing.category = prod_cat
                    if return_details:
                        product_details.append(
                            {
                                "action": "UPDATED",
                                "product_id": str(existing.id),
                                "name": name,
                                "before": before_snap,
                                "after": {
                                    "sale_price_ars": str(price or existing.sale_price_ars),
                                    "stock_units": stock_val or existing.stock_units,
                                },
                            }
                        )
                else:
                    new_product_id = uuid.uuid4()
                    cf_product: dict[str, str] = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }
                    if prod_cat_label:
                        cf_product = {**cf_product, "category_label": prod_cat_label}
                    new_product = Product(
                        id=new_product_id,
                        tenant_id=tenant_id,
                        name=name,
                        # FASE 3 (B2): precio default 0 explícito para auto-creados incompletos.
                        sale_price_ars=price or Decimal("0"),
                        sku=sku,
                        unit_cost_ars=cost,
                        stock_units=stock_val,
                        category=prod_cat,
                        # NULL = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS del servidor
                        low_stock_threshold_units=None,
                        provenance="REAL",
                        # FASE 3 (B2): falta precio o costo → el usuario debe completarlo.
                        requires_completion=not price or not cost,
                        custom_fields=cf_product if cf_product else None,
                    )
                    session.add(new_product)
                    # FASE 3: audit del ingreso inicial de stock + balance (si trae stock).
                    await _record_stock_movement(
                        session,
                        tenant_id,
                        new_product_id,
                        stock_val,
                        cost,
                        "purchase",
                        stock_val,
                        balance_index=_balance_index,
                    )
                    if return_details:
                        product_details.append(
                            {
                                "action": "CREATED",
                                "product_id": str(new_product_id),
                                "name": name,
                                "before": None,
                                "after": {
                                    "sale_price_ars": str(price or Decimal("0")),
                                    "stock_units": stock_val,
                                },
                            }
                        )
                counts["productos"] += 1

        # FASE F: filas ambiguas (otros_detectados) que el usuario NO reasignó a
        # ningún tipo importable → bandeja "Otros" en vez de descartarse.
        if rows_from_otros and not (wants_ventas or wants_gastos or wants_productos):
            counts["otros"] += _capture_unclassified(
                session,
                tenant_id,
                rows,
                headers,
                source,
                uploaded_file_id,
                context_label="Tabla sin clasificar",
            )

    else:
        # ── Documentos de texto/imagen: inserción por línea (montos detectados) ──
        def _add_text_sale(entry: dict[str, Any]) -> None:
            for m in entry.get("montos", []):
                amount = _parse_amount(m)
                if not amount:
                    continue
                session.add(
                    SaleEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        quantity=1,
                        transaction_date=today,
                        payment_method="cash",
                        notes=str(entry.get("linea", ""))[:499] or "Importado desde archivo",
                        provenance="REAL",
                    )
                )
                counts["ventas"] += 1

        def _add_text_expense(entry: dict[str, Any]) -> None:
            for m in entry.get("montos", []):
                amount = _parse_amount(m)
                if not amount:
                    continue
                session.add(
                    ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category="OTHER",
                        transaction_date=today,
                        description=str(entry.get("linea", ""))[:499] or "Gasto importado",
                        payment_method="transfer",
                        provenance="REAL",
                    )
                )
                counts["gastos"] += 1

        text_contexts = summary.get("mapping_contexts")
        if text_contexts:
            # Path por contexto: cada grupo detectado se incluye/excluye y puede
            # reasignarse a otro entity_type (context_entity).
            override = context_entity or {}
            text_bucket = {
                "sale": "ventas_detectadas",
                "expense": "gastos_detectados",
                "product": "stock_detectado",
            }
            for ctx in text_contexts:
                ctx_id = ctx.get("context_id")
                base_entity = ctx.get("entity_type")
                entity = override.get(ctx_id or "") or base_entity
                if entity not in ("sale", "expense"):
                    continue  # producto desde una línea de texto no es insertable
                # Inclusión: por contexto, o legacy por tipo según el entity efectivo.
                if context_confirmed:
                    if not context_confirmed.get(ctx_id):
                        continue
                elif not confirmed_fields.get(
                    "ventas" if entity == "sale" else "gastos"
                ):
                    continue
                rows = [
                    r
                    for r in summary.get(text_bucket.get(base_entity or "", ""), [])
                    if r.get("__context__") == ctx_id
                ]
                for text_row in rows:
                    if entity == "sale":
                        _add_text_sale(text_row)
                    else:
                        _add_text_expense(text_row)
        else:
            # Legacy: documentos viejos sin mapping_contexts.
            if confirmed_fields.get("ventas"):
                for text_row in summary.get("ventas_detectadas", []):
                    _add_text_sale(text_row)
            if confirmed_fields.get("gastos"):
                for text_row in summary.get("gastos_detectados", []):
                    _add_text_expense(text_row)

    await session.flush()
    if return_details:
        counts["product_details"] = product_details
    return counts


_PAGO_COLS: set[str] = {"forma_pago", "metodo_pago", "payment_method", "medio_pago", "pago"}
# Tupla = prioridad: "categoria" debe ganar siempre sobre "tipo" (un CSV con
# columnas `categoria` Y `tipo` debe tomar la categoría de `categoria`).
_CATEGORIA_COLS: tuple[str, ...] = ("categoria", "category", "rubro", "tipo")
_RECURRENTE_COLS: set[str] = {"recurrente", "recurring", "es_fijo", "frecuencia"}

_TRUTHY_ES = {"si", "sí", "true", "1", "x", "verdadero", "fijo", "yes"}
_FALSY_ES = {"no", "false", "0", "variable", ""}


def _parse_bool_es(raw: Any) -> bool | None:
    """Parsea booleanos es-AR de archivos ('Sí'/'fijo'/'True'/'1'). None si es ambiguo."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUTHY_ES:
        return True
    if s in _FALSY_ES or s in {"none", "nan"}:
        return False
    return None
_CANTIDAD_COLS: set[str] = {"cantidad", "qty", "units", "unidades", "cant"}

# Monto de venta: preferimos "total" (precio_unitario × cantidad) sobre "precio_unitario"
_VENTA_TOTAL_COLS: set[str] = {"total", "importe_total", "total_venta", "monto_total"}


def _clean_str(val: Any, max_len: int = 99) -> str | None:
    """Convierte a string limpio o None si es nulo/nan."""
    if val is None:
        return None
    s = str(val).strip()
    return s[:max_len] if s and s.lower() not in {"none", "nan", ""} else None


async def _insert_multisheet_data(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None,
    today: datetime,
    return_details: bool,
    product_details: list[dict[str, Any]],
    counts: dict[str, Any],
    column_mappings: dict[str, str] | None,
    context_mappings: dict[str, dict[str, str]] | None = None,
    context_confirmed: dict[str, bool] | None = None,
    context_entity: dict[str, str] | None = None,
    source: str = "ingestion",
    uploaded_file_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Importa datos de un archivo multi-contexto (multi-hoja) por contexto.

    Si el summary tiene `mapping_contexts`, itera por contexto: filtra filas por el
    marcador `__context__`, respeta la inclusión (`context_confirmed` o, en su
    defecto, `confirmed_fields` por tipo), y aplica el mapeo explícito de ese
    contexto (`context_mappings[context_id]`) con fallback a detección por keyword
    (`_row_val`). Si no hay `mapping_contexts` (summaries viejos), cae al path
    legacy por tipo con detección por keyword. Sin límite de filas.
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or {}
    context_mappings = context_mappings or {}
    _flush_every = 500  # enviar a DB en batches para no acumular en memoria

    # FASE 3: índice de catálogo para vincular ventas/gastos a producto (en memoria).
    _by_sku, _by_name = await _load_product_index(session, tenant_id)
    # FASE E: vertical del tenant para normalizar categorías de producto.
    _business_type = await _load_business_type(session, tenant_id)
    # Batch: balances en una query (evita un SELECT por fila en movimientos).
    _balance_index = await _load_balance_index(session, tenant_id)

    def _val(row: dict[str, Any], col: str | None, keywords: set[str] | tuple[str, ...]) -> Any:
        # Columna explícita (mapeo) si existe; si no, detección por keyword.
        if col:
            return row.get(col)
        return _row_val(row, keywords)

    def _custom_fields(row: dict[str, Any], cf_cols: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for cf_key, src in cf_cols.items():
            v = row.get(src)
            if v is not None and str(v).strip().lower() not in {"", "none", "nan"}:
                out[cf_key] = str(v)
        return out

    def _add_sale(row: dict[str, Any], cols: dict[str, str], cf_cols: dict[str, str]) -> None:
        amount_col = cols.get("amount")
        amount = (
            _parse_amount(row.get(amount_col))
            if amount_col
            else _parse_amount(_row_val(row, _VENTA_TOTAL_COLS))
            or _parse_amount(_row_val(row, _VENTA_AMOUNT_COLS))
        )
        if not amount:
            return
        raw_date = _val(row, cols.get("transaction_date") or cols.get("expense_date"), _FECHA_COLS)
        tx_date = _parse_date(raw_date) if raw_date is not None else today
        if tx_date is None:
            tx_date = today
        qty: int = 1
        qty_raw = _val(row, cols.get("quantity"), _CANTIDAD_COLS)
        if qty_raw not in (None, "", "None", "nan"):
            try:
                qty = max(1, int(float(str(qty_raw))))
            except (ValueError, TypeError):
                qty = 1
        _name_col = cols.get("notes") or cols.get("product_name") or cols.get("name")
        notes = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 499)
        # Canónico: antes se guardaba el texto crudo del archivo ("efectivo",
        # "mercadopago") y filtros/arqueo quedaban inconsistentes.
        pay_raw = _clean_str(_val(row, cols.get("payment_method"), _PAGO_COLS), 30)
        pay = normalize_payment_method(pay_raw) if pay_raw else "cash"
        entry = SaleEntry(
            tenant_id=tenant_id,
            amount=amount,
            quantity=qty,
            transaction_date=tx_date,
            payment_method=pay,
            notes=notes or "Importado desde archivo",
            provenance="REAL",
        )
        cf = _custom_fields(row, cf_cols)
        if cf:
            entry.custom_fields = cf
        # FASE 3: vincular al producto del catálogo.
        entry.product_id = _resolve_product(
            _by_sku,
            _by_name,
            _val(row, cols.get("product_name") or cols.get("name"), _NOMBRE_COLS),
            _val(row, cols.get("sku"), _SKU_COLS),
        )
        session.add(entry)
        counts["ventas"] += 1

    async def _add_expense(
        row: dict[str, Any], cols: dict[str, str], cf_cols: dict[str, str]
    ) -> None:
        amount_col = cols.get("amount")
        amount = (
            _parse_amount(row.get(amount_col))
            if amount_col
            else _parse_amount(_row_val(row, _VENTA_TOTAL_COLS))  # "total" también en compras
            or _parse_amount(_row_val(row, _GASTO_AMOUNT_COLS))
        )
        if not amount:
            return
        raw_date = _val(row, cols.get("expense_date") or cols.get("transaction_date"), _FECHA_COLS)
        tx_date = _parse_date(raw_date) if raw_date is not None else today
        if tx_date is None:
            tx_date = today
        _name_col = cols.get("notes") or cols.get("product_name") or cols.get("name")
        desc = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 499)
        pay_raw = _clean_str(_val(row, cols.get("payment_method"), _PAGO_COLS), 30)
        pay = normalize_payment_method(pay_raw) if pay_raw else "transfer"
        # _row_val_categoria saltea columnas de pago (tipo_pago ≠ categoría).
        cat_raw = _clean_str(
            row.get(cols["category"]) if cols.get("category") else _row_val_categoria(row),
            99,
        )
        # Categoría de producto del vertical ("Bebidas") = compra de mercadería
        # → INVENTORY/COGS, preservando el texto original como label.
        cat_code, cat_label, _ = classify_expense_with_vertical(cat_raw, _business_type)
        recurring = _parse_bool_es(_val(row, cols.get("is_recurring"), _RECURRENTE_COLS))
        expense = ExpenseEntry(
            tenant_id=tenant_id,
            amount=amount,
            category=cat_code,
            transaction_date=tx_date,
            description=desc or "Gasto importado",
            is_recurring=recurring if recurring is not None else False,
            payment_method=pay,
            provenance="REAL",
        )
        cf = _custom_fields(row, cf_cols)
        if cat_label:
            cf = {**cf, "category_label": cat_label}
        if cf:
            expense.custom_fields = cf
        # FASE 3 (B1): vincular el gasto al producto del catálogo (compra de mercadería).
        expense.product_id = _resolve_product(
            _by_sku,
            _by_name,
            _val(row, cols.get("product_name") or cols.get("name"), _NOMBRE_COLS),
            _val(row, cols.get("sku"), _SKU_COLS),
        )
        # FASE D: discriminador COGS/OPEX + stock desde compras con cantidad.
        expense.expense_type = infer_expense_type(cat_code, product_id=expense.product_id)
        # Costo unitario: columna inequívoca y DISTINTA de la del monto ("costo"
        # a secas suele ser el total de la línea — no es unit_cost).
        _amount_src = (
            amount_col
            or _row_col(row, _VENTA_TOTAL_COLS)
            or _row_col(row, _GASTO_AMOUNT_COLS)
        )
        _uc_col = cols.get("unit_cost_ars") or _row_col(row, _COSTO_UNITARIO_COLS)
        unit_cost = (
            _parse_amount(row.get(_uc_col)) if _uc_col and _uc_col != _amount_src else None
        )
        await _apply_purchase_to_stock(
            session,
            tenant_id,
            expense,
            _val(row, cols.get("quantity"), _CANTIDAD_COLS),
            unit_cost,
            balance_index=_balance_index,
        )
        session.add(expense)
        counts["gastos"] += 1

    async def _add_product(
        row: dict[str, Any], cols: dict[str, str], cf_cols: dict[str, str]
    ) -> None:
        _name_col = cols.get("name") or cols.get("product_name")
        name = _clean_str(_val(row, _name_col, _NOMBRE_COLS), 299)
        if not name:
            return
        price = _parse_amount(_val(row, cols.get("sale_price_ars"), _PRECIO_VENTA_COLS))
        cost = _parse_amount(_val(row, cols.get("unit_cost_ars"), _COSTO_COLS))
        try:
            stock_raw = _val(row, cols.get("stock_units"), _STOCK_COLS)
            stock_val = (
                int(float(str(stock_raw)))
                if stock_raw not in (None, "", "None", "nan")
                else 0
            )
        except (ValueError, TypeError):
            stock_val = 0
        sku = _clean_str(_val(row, cols.get("sku"), _SKU_COLS), 99)
        # FASE E: categoría canónica del vertical; sin columna → None.
        cat_raw = _clean_str(
            row.get(cols["category"]) if cols.get("category") else _row_val_categoria(row),
            99,
        )
        cat: str | None = None
        cat_label: str | None = None
        if cat_raw:
            cat, cat_label = normalize_product_category(cat_raw, _business_type)
        result = await session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                func.lower(func.trim(Product.name)) == name.lower(),
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if price:
                existing.sale_price_ars = price
            if cost:
                existing.unit_cost_ars = cost
            if stock_val > 0:
                _delta = stock_val - existing.stock_units
                existing.stock_units = stock_val
                await _record_stock_movement(
                    session,
                    tenant_id,
                    existing.id,
                    _delta,
                    cost,
                    "purchase" if _delta > 0 else "adjustment",
                    stock_val,
                    balance_index=_balance_index,
                )
            if sku:
                existing.sku = sku
            if cat and not existing.category:
                existing.category = cat
        else:
            cf = _custom_fields(row, cf_cols)
            if cat_label:
                cf = {**cf, "category_label": cat_label}
            _new_id = uuid.uuid4()
            session.add(
                Product(
                    id=_new_id,
                    tenant_id=tenant_id,
                    name=name,
                    sku=sku,
                    # FASE 3 (B2): precio default 0 explícito para auto-creados incompletos.
                    sale_price_ars=price or Decimal("0"),
                    unit_cost_ars=cost,
                    stock_units=stock_val,
                    category=cat,
                    low_stock_threshold_units=None,
                    provenance="REAL",
                    # FASE 3 (B2): falta precio o costo → el usuario debe completarlo.
                    requires_completion=not price or not cost,
                    custom_fields=cf or None,
                )
            )
            await _record_stock_movement(
                session,
                tenant_id,
                _new_id,
                stock_val,
                cost,
                "purchase",
                stock_val,
                balance_index=_balance_index,
            )
        counts["productos"] += 1

    entity_bucket = {
        "sale": "ventas_detectadas",
        "expense": "gastos_detectados",
        "product": "stock_detectado",
    }
    entity_confirm_key = {"sale": "ventas", "expense": "gastos", "product": "productos"}

    contexts = summary.get("mapping_contexts")
    if contexts:
        # ── Path por contexto (hoja/grupo) ──────────────────────────────────────
        for ctx in contexts:
            ctx_id = ctx.get("context_id")
            base_entity = ctx.get("entity_type")
            # FASE F: el usuario puede reasignar una hoja no clasificada a un
            # tipo importable (context_entity), igual que en documentos de texto.
            entity = (context_entity or {}).get(ctx_id or "") or base_entity
            if entity not in entity_bucket:
                # Hoja no clasificada y no reasignada → bandeja "Otros".
                otros_rows = [
                    r
                    for r in summary.get("otros_detectados", [])
                    if r.get("__context__") == ctx_id
                ]
                if otros_rows:
                    counts["otros"] += _capture_unclassified(
                        session,
                        tenant_id,
                        otros_rows,
                        ctx.get("headers"),
                        source,
                        uploaded_file_id,
                        context_label=str(ctx.get("label") or ctx_id or ""),
                    )
                continue
            # Inclusión: por contexto si vino context_confirmed; si no, por tipo (legacy)
            if context_confirmed:
                if not context_confirmed.get(ctx_id):
                    continue
            elif not confirmed_fields.get(entity_confirm_key[entity]):
                continue
            # Las filas viven en el bucket del tipo ORIGINAL de la hoja (o en
            # otros_detectados si era no clasificada y fue reasignada).
            bucket_key = entity_bucket.get(base_entity or "", "otros_detectados")
            bucket = summary.get(bucket_key, [])
            rows = [r for r in bucket if r.get("__context__") == ctx_id]
            if not rows:
                continue
            mapping = context_mappings.get(ctx_id or "", {})
            cols, cf_cols = _resolve_target_cols(mapping) if mapping else ({}, {})
            for _i, row in enumerate(rows):
                if entity == "sale":
                    _add_sale(row, cols, cf_cols)
                elif entity == "expense":
                    await _add_expense(row, cols, cf_cols)
                else:
                    await _add_product(row, cols, cf_cols)
                if (_i + 1) % _flush_every == 0:
                    await session.flush()
    else:
        # ── Legacy: summaries sin mapping_contexts. Detección por keyword por tipo. ──
        if confirmed_fields.get("ventas"):
            for _i, row in enumerate(summary.get("ventas_detectadas", [])):
                _add_sale(row, {}, {})
                if (_i + 1) % _flush_every == 0:
                    await session.flush()
        if confirmed_fields.get("gastos"):
            for row in summary.get("gastos_detectados", []):
                await _add_expense(row, {}, {})
        if confirmed_fields.get("productos"):
            for row in summary.get("stock_detectado", []):
                await _add_product(row, {}, {})

    await session.flush()
    if return_details:
        counts["product_details"] = product_details
    return counts


def default_confirmed_fields(summary: dict[str, Any]) -> dict[str, bool]:
    inferred = summary.get("inferred_type")
    return {
        "ventas": bool(summary.get("ventas_detectadas")) or inferred == "ventas",
        "gastos": bool(summary.get("gastos_detectados")) or inferred == "gastos",
        "productos": bool(summary.get("stock_detectado")) or inferred == "stock",
    }
