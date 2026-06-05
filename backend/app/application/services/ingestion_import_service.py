"""Shared insertion logic for confirmed parsed ingestion summaries."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import FECHA_COLS as _FECHA_COLS
from app.application.services.file_parsing import GASTO_COLS as _GASTO_COLS
from app.application.services.file_parsing import VENTA_COLS as _VENTA_COLS
from app.observability.logger import get_logger

logger = get_logger(__name__)


def _normalize_name(name: str) -> str:
    """Normaliza nombre de producto para comparación: lower, sin guiones, espacios únicos."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", name.strip().lower()))


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


def _parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _find_col(headers: list[str], keywords: set[str]) -> str | None:
    for h in headers:
        norm = h.lower().strip().replace(" ", "_")
        if any(k in norm for k in keywords):
            return h
    return None


async def insert_confirmed_data(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
    confirmed_fields: dict[str, bool] | None = None,
    return_details: bool = False,
    column_mappings: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse parsed_summary_json and insert rows into sales/expense/product tables.

    When return_details=True, also returns product_details list with per-row
    action ('CREATED'|'UPDATED'), product_id, name, before/after snapshots.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or _default_confirmed_fields(summary)
    today = date.today()
    counts: dict[str, Any] = {"ventas": 0, "gastos": 0, "productos": 0}
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
            )

        rows: list[dict[str, Any]]
        if inferred_type == "stock":
            rows = summary.get("stock_detectado", [])
        else:
            rows = summary.get("ventas_detectadas", []) or summary.get("gastos_detectados", [])
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

        wants_ventas = bool(
            inferred_type != "stock"
            and confirmed_fields.get("ventas")
            and (summary.get("has_venta") or inferred_type == "ventas")
            and venta_col
        )
        wants_gastos = bool(
            inferred_type != "stock"
            and confirmed_fields.get("gastos")
            and (summary.get("has_gasto") or inferred_type == "gastos")
            and gasto_col
        )
        wants_productos = bool(
            confirmed_fields.get("productos")
            and (summary.get("has_producto") or inferred_type == "stock")
            and nombre_col
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

                    # Método de pago
                    pay_raw = row.get(payment_col) if payment_col else None
                    pay_str = (
                        str(pay_raw).strip()[:50]
                        if pay_raw and str(pay_raw).strip() not in {"None", "nan", ""}
                        else "cash"
                    )

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
                    # Categoría
                    cat_raw = row.get(category_col) if category_col else None
                    cat_str = (
                        str(cat_raw).strip()[:99]
                        if cat_raw and str(cat_raw).strip() not in {"None", "nan", ""}
                        else "importado"
                    )

                    # Custom fields
                    cf = {
                        k: str(row.get(v, ""))
                        for k, v in custom_field_cols.items()
                        if row.get(v) is not None
                    }

                    expense = ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category=cat_str,
                        transaction_date=tx_date,
                        description=desc,
                        payment_method="transfer",
                        provenance="REAL",
                    )
                    if cf:
                        expense.custom_fields = cf
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
                        existing.stock_units = stock_val
                    if sku:
                        existing.sku = sku
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
                    new_product = Product(
                        id=new_product_id,
                        tenant_id=tenant_id,
                        name=name,
                        sku=sku,
                        sale_price_ars=price or Decimal("0"),
                        unit_cost_ars=cost,
                        stock_units=stock_val,
                        # NULL = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS del servidor
                        low_stock_threshold_units=None,
                        provenance="REAL",
                        custom_fields=cf_product if cf_product else None,
                    )
                    session.add(new_product)
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

    else:
        if confirmed_fields.get("ventas"):
            for entry in summary.get("ventas_detectadas", []):
                for m in entry.get("montos", []):
                    amount = _parse_amount(m)
                    if amount:
                        session.add(
                            SaleEntry(
                                tenant_id=tenant_id,
                                amount=amount,
                                quantity=1,
                                transaction_date=today,
                                payment_method="cash",
                                notes=str(entry.get("linea", ""))[:499],
                                provenance="REAL",
                            )
                        )
                        counts["ventas"] += 1

        if confirmed_fields.get("gastos"):
            for entry in summary.get("gastos_detectados", []):
                for m in entry.get("montos", []):
                    amount = _parse_amount(m)
                    if amount:
                        session.add(
                            ExpenseEntry(
                                tenant_id=tenant_id,
                                amount=amount,
                                category="importado",
                                transaction_date=today,
                                description=str(entry.get("linea", ""))[:499] or "Gasto importado",
                                payment_method="transfer",
                                provenance="REAL",
                            )
                        )
                        counts["gastos"] += 1

    await session.flush()
    if return_details:
        counts["product_details"] = product_details
    return counts


_PAGO_COLS: set[str] = {"forma_pago", "metodo_pago", "payment_method", "medio_pago", "pago"}
_CATEGORIA_COLS: set[str] = {"categoria", "category", "rubro", "tipo"}
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
    today: date,
    return_details: bool,
    product_details: list[dict[str, Any]],
    counts: dict[str, Any],
    column_mappings: dict[str, str] | None,
) -> dict[str, Any]:
    """Importa datos de un archivo multi-hoja procesando cada tipo por separado.

    Sin límite de filas: importa todo lo que hay en cada array del summary.
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    confirmed_fields = confirmed_fields or {}

    # ── Ventas ────────────────────────────────────────────────────────────────
    if confirmed_fields.get("ventas"):
        ventas_rows: list[dict[str, Any]] = summary.get("ventas_detectadas", [])
        if ventas_rows:
            headers = list(ventas_rows[0].keys())
            fecha_col = _find_col(headers, _FECHA_COLS)
            # Preferencia: "total" (suma real) > "precio_unitario" (precio por unidad)
            amount_col = (
                _find_col(headers, _VENTA_TOTAL_COLS)
                or _find_col(headers, _VENTA_AMOUNT_COLS)
            )
            nombre_col = _find_col(headers, _NOMBRE_COLS)
            pago_col = _find_col(headers, _PAGO_COLS)
            qty_col = _find_col(headers, _CANTIDAD_COLS)
            for row in ventas_rows:
                raw_date = row.get(fecha_col) if fecha_col else None
                tx_date = _parse_date(raw_date) if fecha_col else today
                if tx_date is None:
                    tx_date = today
                if not amount_col:
                    continue
                amount = _parse_amount(row.get(amount_col))
                if not amount:
                    continue
                # Cantidad vendida
                qty: int = 1
                qty_raw = row.get(qty_col) if qty_col else None
                if qty_raw not in (None, "", "None", "nan"):
                    try:
                        qty = max(1, int(float(str(qty_raw))))
                    except (ValueError, TypeError):
                        qty = 1
                # Descripción desde nombre del producto
                notes = _clean_str(row.get(nombre_col) if nombre_col else None, 499)
                # Método de pago
                pay = _clean_str(row.get(pago_col) if pago_col else None, 30)
                session.add(
                    SaleEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        quantity=qty,
                        transaction_date=tx_date,
                        payment_method=pay or "cash",
                        notes=notes or "Importado desde archivo",
                        provenance="REAL",
                    )
                )
                counts["ventas"] += 1

    # ── Gastos ────────────────────────────────────────────────────────────────
    if confirmed_fields.get("gastos"):
        gastos_rows: list[dict[str, Any]] = summary.get("gastos_detectados", [])
        if gastos_rows:
            headers = list(gastos_rows[0].keys())
            fecha_col = _find_col(headers, _FECHA_COLS)
            amount_col = (
                _find_col(headers, _VENTA_TOTAL_COLS)  # "total" también en compras
                or _find_col(headers, _GASTO_AMOUNT_COLS)
            )
            nombre_col = _find_col(headers, _NOMBRE_COLS)
            pago_col = _find_col(headers, _PAGO_COLS)
            cat_col = _find_col(headers, _CATEGORIA_COLS)
            for row in gastos_rows:
                raw_date = row.get(fecha_col) if fecha_col else None
                tx_date = _parse_date(raw_date) if fecha_col else today
                if tx_date is None:
                    tx_date = today
                if not amount_col:
                    continue
                amount = _parse_amount(row.get(amount_col))
                if not amount:
                    continue
                desc = _clean_str(row.get(nombre_col) if nombre_col else None, 499)
                pay = _clean_str(row.get(pago_col) if pago_col else None, 30)
                cat = _clean_str(row.get(cat_col) if cat_col else None, 50)
                session.add(
                    ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category=cat or "importado",
                        transaction_date=tx_date,
                        description=desc or "Gasto importado",
                        payment_method=pay or "transfer",
                        provenance="REAL",
                    )
                )
                counts["gastos"] += 1

    # ── Productos / Stock ─────────────────────────────────────────────────────
    if confirmed_fields.get("productos"):
        stock_rows: list[dict[str, Any]] = summary.get("stock_detectado", [])
        if stock_rows:
            headers = list(stock_rows[0].keys())
            nombre_col = _find_col(headers, _NOMBRE_COLS)
            precio_col = _find_col(headers, _PRECIO_VENTA_COLS)
            costo_col = _find_col(headers, _COSTO_COLS)
            stock_col = _find_col(headers, _STOCK_COLS)
            sku_col = _find_col(headers, _SKU_COLS)
            cat_col = _find_col(headers, _CATEGORIA_COLS)
            if nombre_col:
                for row in stock_rows:
                    name = _clean_str(row.get(nombre_col), 299)
                    if not name:
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
                    sku = _clean_str(row.get(sku_col) if sku_col else None, 99)
                    cat = _clean_str(row.get(cat_col) if cat_col else None, 99)
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
                            existing.stock_units = stock_val
                        if sku:
                            existing.sku = sku
                        if cat and not existing.category:
                            existing.category = cat
                    else:
                        session.add(
                            Product(
                                id=uuid.uuid4(),
                                tenant_id=tenant_id,
                                name=name,
                                sku=sku,
                                sale_price_ars=price or Decimal("0"),
                                unit_cost_ars=cost,
                                stock_units=stock_val,
                                category=cat,
                                low_stock_threshold_units=None,
                                provenance="REAL",
                            )
                        )
                    counts["productos"] += 1

    await session.flush()
    if return_details:
        counts["product_details"] = product_details
    return counts


def _default_confirmed_fields(summary: dict[str, Any]) -> dict[str, bool]:
    inferred = summary.get("inferred_type")
    return {
        "ventas": bool(summary.get("ventas_detectadas")) or inferred == "ventas",
        "gastos": bool(summary.get("gastos_detectados")) or inferred == "gastos",
        "productos": bool(summary.get("stock_detectado")) or inferred == "stock",
    }
