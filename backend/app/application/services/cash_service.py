"""cash_service — persiste ventas, cobros y gastos."""

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.expense_categories import (
    infer_expense_type,
    normalize_expense_category,
    strip_accents,
)
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

# Claves en forma normalizada: lower, sin tildes, separadores (espacio/-/_//) → " ".
_PAYMENT_METHOD_MAP = {
    "cash": "cash",
    "efectivo": "cash",
    "contado": "cash",
    "transfer": "transfer",
    "transferencia": "transfer",
    # Débito automático (servicios/seguros): sale de la cuenta bancaria.
    "debito automatico": "transfer",
    "debit card": "debit_card",
    "debito": "debit_card",
    "tarjeta debito": "debit_card",
    "credit card": "credit_card",
    "credito": "credit_card",
    "tarjeta credito": "credit_card",
    "qr": "qr",
    # Billeteras virtuales: el cobro entra por QR/app.
    "mercadopago": "qr",
    "mercado pago": "qr",
    "mp": "qr",
    "billetera virtual": "qr",
    # Cuenta corriente / fiado: canónico propio (no se pierde como "other").
    "account": "account",
    "cuenta corriente": "account",
    "cta cte": "account",
    "ctacte": "account",
    "cta corriente": "account",
    "fiado": "account",
    "other": "other",
    "otro": "other",
}


def normalize_payment_method(value: str | None) -> str:
    """Texto libre → código canónico de método de pago; sin match → 'other'.

    Tolera tildes y separadores ("Cuenta_Corriente", "débito automático",
    "Mercado-Pago") normalizando la clave antes del lookup.
    """
    if not value:
        return "other"
    key = re.sub(r"[.\s\-_/]+", " ", strip_accents(str(value).strip().lower())).strip()
    return _PAYMENT_METHOD_MAP.get(key, "other")


# Alias interno legacy.
_normalize_payment_method = normalize_payment_method




def _coerce_transaction_date(value: object) -> datetime:
    # transaction_date es timestamp: si viene solo fecha, queda a medianoche; si no
    # viene nada, se usa el momento actual (captura la hora del registro en vivo).
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            try:
                return datetime.combine(date.fromisoformat(value), datetime.min.time())
            except ValueError:
                return datetime.now()
    return datetime.now()


async def save_sale(
    entities: dict[str, Any],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> SaleEntry:
    """Crea registro en sales_entries.

    Falla explícitamente si falta payment_method en lugar de defaultear a 'cash':
    AgentCash debe haber preguntado el método antes de crear la acción.
    """
    payment_method = _normalize_payment_method(entities.get("payment_method"))
    if payment_method == "other":
        raise ValueError("payment_method_required_for_sale")

    product_id_raw = entities.get("product_id")
    product_id = uuid.UUID(str(product_id_raw)) if product_id_raw else None
    try:
        qty = max(1, int(float(str(entities.get("quantity") or 1))))
    except (ValueError, TypeError):
        qty = 1
    sale = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=qty,
        product_id=product_id,
        transaction_date=_coerce_transaction_date(
            entities.get("transaction_date") or entities.get("date")
        ),
        payment_method=payment_method,
        notes=entities.get("product_description"),
        provenance="REAL",
    )
    db.add(sale)
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        decision_type="SALE_REGISTERED",
        decision_data={
            "amount": str(entities["amount"]),
            "payment_method": payment_method,
            "quantity": qty,
            "price_lookup_source": entities.get("price_lookup_source"),
        },
        triggered_by="agent:cash",
        actor_user_id=user_id,
        context={},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    await db.flush()
    logger.info("sale_registered", tenant_id=str(tenant_id), amount=str(entities["amount"]))
    return sale


async def save_cash_inflow(
    entities: dict[str, Any],
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> SaleEntry:
    """Registra cobro de una venta en cuenta corriente como inflow en sales_entries."""
    entry = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=1,
        transaction_date=datetime.now(),
        payment_method="inflow",
        notes=entities.get("notes") or entities.get("linked_sale_id"),
        provenance="REAL",
    )
    db.add(entry)
    await db.flush()
    logger.info(
        "cash_inflow_registered",
        tenant_id=str(tenant_id),
        amount=str(entities["amount"]),
    )
    return entry


async def save_expense(
    entities: dict[str, Any],
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> ExpenseEntry:
    """Registra un gasto en expense_entries."""
    category, category_label = normalize_expense_category(entities.get("category"))
    # Discriminador canónico: COGS si es mercadería (INVENTORY o producto
    # vinculado), igual que en los caminos de import — antes el mismo gasto de
    # mercadería quedaba OPEX por chat y COGS por archivo.
    expense_type = infer_expense_type(
        category,
        product_id=entities.get("product_id"),
        explicit=entities.get("expense_type"),
    )
    expense = ExpenseEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        category=category,
        expense_type=expense_type,
        transaction_date=_coerce_transaction_date(
            entities.get("transaction_date") or entities.get("date")
        ),
        description=entities.get("description") or entities.get("category", "gasto"),
        is_recurring=bool(entities.get("is_recurring", False)),
        payment_method=_normalize_payment_method(entities.get("payment_method")),
        supplier_name=entities.get("supplier_name"),
        notes=entities.get("notes"),
        provenance="REAL",
    )
    if category_label:
        expense.custom_fields = {
            **(expense.custom_fields or {}),
            "category_label": category_label,
        }
    db.add(expense)
    await db.flush()
    logger.info(
        "expense_registered",
        tenant_id=str(tenant_id),
        amount=str(entities["amount"]),
    )
    return expense
