"""cash_service — persiste ventas, cobros y gastos."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

_EXPENSE_CATEGORY_MAP = {
    "rent": "RENT",
    "alquiler": "RENT",
    "utilities": "UTILITIES",
    "servicios": "UTILITIES",
    "service": "UTILITIES",
    "payroll": "PAYROLL",
    "sueldos": "PAYROLL",
    "inventario": "INVENTORY",
    "inventory": "INVENTORY",
    "mercaderia": "INVENTORY",
    "mercadería": "INVENTORY",
    "stock": "INVENTORY",
    "marketing": "MARKETING",
    "publicidad": "MARKETING",
    "other": "OTHER",
    "otros": "OTHER",
}

_PAYMENT_METHOD_MAP = {
    "cash": "cash",
    "efectivo": "cash",
    "transfer": "transfer",
    "transferencia": "transfer",
    "debit_card": "debit_card",
    "debito": "debit_card",
    "débito": "debit_card",
    "credit_card": "credit_card",
    "credito": "credit_card",
    "crédito": "credit_card",
    "qr": "qr",
}


def _normalize_payment_method(value: str | None) -> str:
    if not value:
        return "other"
    return _PAYMENT_METHOD_MAP.get(str(value).strip().lower(), "other")


def _normalize_expense_category(value: str | None) -> str:
    if not value:
        return "OTHER"
    normalized = str(value).strip().lower()
    return _EXPENSE_CATEGORY_MAP.get(normalized, str(value).strip().upper())


def _coerce_transaction_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return date.today()
    return date.today()


async def save_sale(
    entities: dict,
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
    entities: dict,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> SaleEntry:
    """Registra cobro de una venta en cuenta corriente como inflow en sales_entries."""
    entry = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=1,
        transaction_date=date.today(),
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
    entities: dict,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> ExpenseEntry:
    """Registra un gasto en expense_entries."""
    expense = ExpenseEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        category=_normalize_expense_category(entities.get("category")),
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
    db.add(expense)
    await db.flush()
    logger.info(
        "expense_registered",
        tenant_id=str(tenant_id),
        amount=str(entities["amount"]),
    )
    return expense
