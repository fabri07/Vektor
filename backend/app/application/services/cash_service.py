"""cash_service — persiste ventas, cobros y gastos."""

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.respaldo_service import exigir_respaldo
from app.domain.business_time import (
    a_hora_de_negocio,
    fecha_de_negocio,
    now_ar_naive,
    today_ar,
)
from app.domain.date_parsing import parse_business_datetime
from app.domain.expense_categories import (
    infer_expense_type,
    normalize_expense_category,
    resolve_tax_kind,
    strip_accents,
)
from app.observability.logger import get_logger
from app.persistence.costeo_de_ventas import marcar_unidad_no_confirmada
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

    Tarjetas (G-2, D-3): si la clave no está en el mapa, una palabra «credito»
    la hace crédito («Tarjeta de crédito», «Crédito 3 cuotas», «Visa crédito») y
    una «debito», débito («Tarjeta de débito», «Visa débito»). «Tarjeta» o «Visa»
    solos son ambiguos y quedan en 'other'; con las dos palabras, o «nota de
    crédito» (un comprobante, no un medio de pago), también. Sólo es el camino
    de entrada: lo importado antes con estas frases sigue guardado como 'other'.
    """
    if not value:
        return "other"
    key = re.sub(r"[.\s\-_/]+", " ", strip_accents(str(value).strip().lower())).strip()
    canonico = _PAYMENT_METHOD_MAP.get(key)
    if canonico is not None:
        return canonico
    palabras = set(key.split())
    credito, debito = "credito" in palabras, "debito" in palabras
    if "nota" in palabras or credito == debito:
        return "other"
    if debito and "automatico" in palabras:
        return "transfer"  # «débito automático de la tarjeta»: sale del banco
    return "credit_card" if credito else "debit_card"


# Alias interno legacy.
_normalize_payment_method = normalize_payment_method




class FechaFuturaError(ValueError):
    """La carga del chat trae una fecha de negocio posterior a hoy (día AR)."""

    def __init__(self, fecha: date) -> None:
        super().__init__(f"Fecha futura: {fecha.isoformat()}")
        self.user_message = (
            f"La fecha {fecha.strftime('%d/%m/%Y')} es posterior a hoy y no la puedo "
            "registrar. Decime la fecha real de la operación."
        )


def _coerce_transaction_date(value: object) -> datetime:
    """Fecha de una carga MANUAL: si el usuario no informó ninguna, "ahora" es la
    respuesta correcta (está registrando algo que pasa en este momento).

    Lo que NO es correcto es caer a "ahora" porque el valor no se pudo leer: eso
    inventa una fecha de negocio (F6). Con el parser compartido, "12/03/2024" ahora
    se entiende en vez de convertirse silenciosamente en hoy.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return now_ar_naive()
    parsed = parse_business_datetime(value)
    if parsed is None:
        raise ValueError(f"Fecha no reconocida: {value!r}")
    # Una fecha futura nunca se persiste (E0-A-1/E0-A-2): de 21 a 24 ART el
    # día UTC ya es mañana, y una compra fechada mañana bloquea la venta de hoy
    # de ese producto (B·5e). Se compara contra el día AR, no el del servidor.
    if fecha_de_negocio(parsed) > today_ar():
        raise FechaFuturaError(fecha_de_negocio(parsed))
    return a_hora_de_negocio(parsed)


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
    transaction_date = _coerce_transaction_date(
        entities.get("transaction_date") or entities.get("date")
    )
    # B·5e: «no se puede vender algo que no se compró». Las dos excepciones traen
    # `user_message`, que el chat muestra tal cual.
    await exigir_respaldo(db, tenant_id, [(product_id, transaction_date.date())])
    sale = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=qty,
        product_id=product_id,
        transaction_date=transaction_date,
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
    # M·2a: el chat guarda la cantidad tal cual: costo pendiente si es fraccionado.
    marcar_unidad_no_confirmada(db.info, [sale.id])
    logger.info("sale_registered", tenant_id=str(tenant_id), amount=str(entities["amount"]))
    return sale


async def save_cash_inflow(
    entities: dict[str, Any],
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> SaleEntry:
    """Registra cobro de una venta en cuenta corriente como inflow en sales_entries.

    Si ``entities`` trae ``customer_id``, se vincula el cobro al cliente (Fase 2).
    Si no viene, ``customer_id`` queda NULL (comportamiento anterior).
    """
    # Vínculo al cliente (Fase 2): coercionar a UUID si viene como string.
    raw_customer_id = entities.get("customer_id")
    customer_id: uuid.UUID | None = None
    if raw_customer_id is not None:
        try:
            customer_id = uuid.UUID(str(raw_customer_id))
        except (ValueError, AttributeError):
            customer_id = None

    entry = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(str(entities["amount"])),
        quantity=1,
        transaction_date=now_ar_naive(),
        payment_method="inflow",
        notes=entities.get("notes") or entities.get("linked_sale_id"),
        provenance="REAL",
        customer_id=customer_id,
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
        # M·3-3: categoría «ingresos brutos» → TAXES/iibb; «impuestos» → sin tipo.
        # Nunca por la descripción (ver ``resolve_tax_kind``).
        tax_kind=resolve_tax_kind(
            category, explicit=entities.get("tax_kind"), textos=(entities.get("category"),)
        ),
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
