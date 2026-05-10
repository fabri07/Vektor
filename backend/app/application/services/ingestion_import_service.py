"""Shared insertion logic for confirmed parsed ingestion summaries."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func

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
_PRECIO_VENTA_COLS: set[str] = {"precio_venta", "precio", "price", "p_venta"}
_COSTO_COLS: set[str] = {"costo", "cost", "precio_costo", "p_costo"}
_STOCK_COLS: set[str] = {"stock", "cantidad", "inventario", "units", "qty", "existencia"}
_SKU_COLS: set[str] = {"sku", "codigo", "código", "code", "ref", "id_producto"}


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
        rows: list[dict[str, Any]]
        if inferred_type == "stock":
            rows = summary.get("stock_detectado", [])
        else:
            rows = summary.get("ventas_detectadas", []) or summary.get("gastos_detectados", [])
        if not rows:
            return counts

        headers = list(rows[0].keys())
        fecha_col = _find_col(headers, _FECHA_COLS)
        venta_col = _find_col(headers, _VENTA_COLS)
        gasto_col = _find_col(headers, _GASTO_COLS)
        nombre_col = _find_col(headers, _NOMBRE_COLS)
        precio_col = _find_col(headers, _PRECIO_VENTA_COLS)
        costo_col = _find_col(headers, _COSTO_COLS)
        stock_col = _find_col(headers, _STOCK_COLS)
        sku_col = _find_col(headers, _SKU_COLS)

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
                amount = _parse_amount(row.get(venta_col))
                if amount:
                    session.add(SaleEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        quantity=1,
                        transaction_date=tx_date,
                        payment_method="cash",
                        notes="Importado desde archivo",
                        provenance="REAL",
                    ))
                    counts["ventas"] += 1

            if wants_gastos:
                amount = _parse_amount(row.get(gasto_col))
                if amount:
                    desc_raw = row.get(nombre_col) if nombre_col else None
                    desc = (
                        str(desc_raw).strip()[:499]
                        if desc_raw and str(desc_raw).strip() not in {"None", "nan", ""}
                        else "Gasto importado"
                    )
                    session.add(ExpenseEntry(
                        tenant_id=tenant_id,
                        amount=amount,
                        category="importado",
                        transaction_date=tx_date,
                        description=desc,
                        payment_method="transfer",
                        provenance="REAL",
                    ))
                    counts["gastos"] += 1

        if wants_productos:
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
                        product_details.append({
                            "action": "UPDATED",
                            "product_id": str(existing.id),
                            "name": name,
                            "before": before_snap,
                            "after": {"sale_price_ars": str(price or existing.sale_price_ars), "stock_units": stock_val or existing.stock_units},
                        })
                else:
                    new_product_id = uuid.uuid4()
                    new_product = Product(
                        id=new_product_id,
                        tenant_id=tenant_id,
                        name=name,
                        sku=sku,
                        sale_price_ars=price or Decimal("0"),
                        unit_cost_ars=cost,
                        stock_units=stock_val,
                        provenance="REAL",
                    )
                    session.add(new_product)
                    if return_details:
                        product_details.append({
                            "action": "CREATED",
                            "product_id": str(new_product_id),
                            "name": name,
                            "before": None,
                            "after": {"sale_price_ars": str(price or Decimal("0")), "stock_units": stock_val},
                        })
                counts["productos"] += 1

    else:
        if confirmed_fields.get("ventas"):
            for entry in summary.get("ventas_detectadas", []):
                for m in entry.get("montos", []):
                    amount = _parse_amount(m)
                    if amount:
                        session.add(SaleEntry(
                            tenant_id=tenant_id,
                            amount=amount,
                            quantity=1,
                            transaction_date=today,
                            payment_method="cash",
                            notes=str(entry.get("linea", ""))[:499],
                            provenance="REAL",
                        ))
                        counts["ventas"] += 1

        if confirmed_fields.get("gastos"):
            for entry in summary.get("gastos_detectados", []):
                for m in entry.get("montos", []):
                    amount = _parse_amount(m)
                    if amount:
                        session.add(ExpenseEntry(
                            tenant_id=tenant_id,
                            amount=amount,
                            category="importado",
                            transaction_date=today,
                            description=str(entry.get("linea", ""))[:499] or "Gasto importado",
                            payment_method="transfer",
                            provenance="REAL",
                        ))
                        counts["gastos"] += 1

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
