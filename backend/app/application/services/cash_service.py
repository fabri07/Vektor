"""cash_service — persiste ventas, cobros y gastos."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)


async def save_sale(
    entities: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> SaleEntry:
    """Crea registro en sales_entries. payment_status paid→payment_method, credit→'credit'."""
    payment_method = entities.get("payment_method") or (
        "cash" if entities.get("payment_status") == "paid" else "credit"
    )
    sale = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=1,
        transaction_date=date.today(),
        payment_method=payment_method,
        notes=entities.get("product_description"),
    )
    db.add(sale)
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        decision_type="SALE_REGISTERED",
        decision_data={
            "amount": str(entities["amount"]),
            "payment_method": payment_method,
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
        category=entities.get("category", "otros"),
        transaction_date=date.today(),
        description=entities.get("description") or entities.get("category", "gasto"),
        is_recurring=False,
        payment_method="cash",
    )
    db.add(expense)
    await db.flush()
    logger.info(
        "expense_registered",
        tenant_id=str(tenant_id),
        amount=str(entities["amount"]),
    )
    return expense
