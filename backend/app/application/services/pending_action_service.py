"""Servicio de pending_actions — create / execute / cancel.

Flujo de aprobación (dos fases):
  Fase 1 — POST /agent/chat: si riesgo MEDIUM/HIGH → create_pending_action()
  Fase 2 — POST /agent/confirm/{id}: execute_pending_action() + marcar APPROVED
            POST /agent/cancel/{id}: cancel_pending_action() + marcar REJECTED
            POST /agent/retry/{id}:  re-ejecutar cuando execution_status=FAILED|REQUIRES_RECONNECT

Regla crítica: execute_pending_action() no toca execution_status.
El endpoint (confirm / retry) es responsable del ciclo de vida completo:
  IN_PROGRESS → SUCCEEDED | FAILED | REQUIRES_RECONNECT.

"""

import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.cash_service as cash_service
import app.application.services.stock_service as stock_service
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import ActionType
from app.application.services.automation_service import determine_external_system
from app.application.services.chat_memory_service import ChatMemoryService
from app.application.services.ingestion_import_service import (
    check_nonempty_import,
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.pending_action import PendingAction

if TYPE_CHECKING:
    from app.application.agents.google.tool_broker import GoogleToolBroker

logger = get_logger(__name__)


def _external_idempotency_key(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    action_type: str,
    payload: dict[str, Any],
) -> str:
    raw = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "action_type": action_type,
            "payload": payload,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _summary_from_tabular_records(records: list[Any], record_type: str) -> dict[str, Any]:
    rows = [row for row in records if isinstance(row, dict)]
    normalized_type = record_type.lower()
    if normalized_type in {"sales", "sale", "venta", "ventas"}:
        return {
            "file_type": "spreadsheet",
            "source_format": "google_sheets",
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "has_venta": True,
            "has_gasto": False,
            "has_producto": False,
            "ventas_detectadas": rows,
            "gastos_detectados": [],
            "stock_detectado": [],
            "rows_processed": len(rows),
        }
    if normalized_type in {"expenses", "expense", "gasto", "gastos"}:
        return {
            "file_type": "spreadsheet",
            "source_format": "google_sheets",
            "inferred_type": "gastos",
            "confidence": "HIGH",
            "has_venta": False,
            "has_gasto": True,
            "has_producto": False,
            "ventas_detectadas": [],
            "gastos_detectados": rows,
            "stock_detectado": [],
            "rows_processed": len(rows),
        }
    if normalized_type in {"stock", "product", "products", "producto", "productos"}:
        return {
            "file_type": "spreadsheet",
            "source_format": "google_sheets",
            "inferred_type": "stock",
            "confidence": "HIGH",
            "has_venta": False,
            "has_gasto": False,
            "has_producto": True,
            "ventas_detectadas": [],
            "gastos_detectados": [],
            "stock_detectado": rows,
            "rows_processed": len(rows),
        }
    return {
        "file_type": "spreadsheet",
        "source_format": "google_sheets",
        "inferred_type": "general",
        "confidence": "MEDIUM",
        "has_venta": False,
        "has_gasto": False,
        "has_producto": False,
        "ventas_detectadas": rows,
        "gastos_detectados": [],
        "stock_detectado": [],
        "rows_processed": len(rows),
    }


def _confirmed_fields_for_record_type(record_type: str) -> dict[str, bool]:
    normalized_type = record_type.lower()
    return {
        "ventas": normalized_type in {"sales", "sale", "venta", "ventas"},
        "gastos": normalized_type in {"expenses", "expense", "gasto", "gastos"},
        "productos": normalized_type in {"stock", "product", "products", "producto", "productos"},
    }


async def create_pending_action(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    action_type: str,
    payload: dict[str, Any],
    risk_level: str,
) -> PendingAction:
    """Crea un PendingAction con TTL de 10 minutos. Hace flush para obtener el id."""
    external_system = (
        determine_external_system(action_type, payload) if payload.get("mode") == "mcp" else None
    )
    idempotency_key = (
        _external_idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id,
            action_type=action_type,
            payload=payload,
        )
        if external_system
        else None
    )

    action = PendingAction(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type=action_type,
        payload=payload,
        risk_level=risk_level,
        status="PENDING",
        external_system=external_system,
        idempotency_key=idempotency_key,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(action)
    await db.flush()  # obtener id sin commitear — el endpoint hace commit al final
    logger.info(
        "pending_action_created",
        action_id=str(action.id),
        action_type=action_type,
        risk_level=risk_level,
        tenant_id=str(tenant_id),
    )
    return action


def _make_google_broker(action: "PendingAction") -> "GoogleToolBroker":
    """Instancia el broker Google para ejecutar una PendingAction."""
    from app.application.agents.google.tool_broker import GoogleToolBroker  # noqa: PLC0415
    from app.config.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    return GoogleToolBroker(
        user_id=str(action.user_id),
        tenant_id=str(action.tenant_id),
        settings=settings,
    )


class ReclassifyExpenseError(Exception):
    """No se pudo resolver ningún gasto para reclasificar (identificación vacía)."""

    user_message = (
        "No encontré el gasto que querés reclasificar. Probá indicando la "
        "descripción, el monto o la fecha, o pedime que detecte los registros."
    )


async def _resolve_reclassify_target_expenses(
    action: PendingAction,
    db: AsyncSession,
    payload: dict[str, Any],
) -> list[Any]:
    """Resuelve la lista de ExpenseEntry a reclasificar (masivo o singular).

    Prioridad: ``expense_ids[]`` (masivo) → ``expense_id`` (singular) →
    fallback por fecha + monto + descripción (singular). Tenant-isolated y
    excluye gastos anulados.
    """
    from decimal import Decimal  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415
    from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
        ExpenseRepository,
    )

    repo = ExpenseRepository(db)

    # ── Masivo: expense_ids[] ─────────────────────────────────────────────────
    expense_ids_raw = payload.get("expense_ids")
    if isinstance(expense_ids_raw, list) and expense_ids_raw:
        parsed_ids: list[uuid.UUID] = []
        for raw in expense_ids_raw:
            with contextlib.suppress(ValueError, TypeError):
                parsed_ids.append(uuid.UUID(str(raw)))
        if parsed_ids:
            q = select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == action.tenant_id,
                ExpenseEntry.voided_at.is_(None),
                ExpenseEntry.id.in_(parsed_ids),
            )
            result = await db.execute(q)
            return list(result.scalars().all())
        return []

    # ── Singular: expense_id ──────────────────────────────────────────────────
    expense_id_raw = payload.get("expense_id")
    if expense_id_raw:
        with contextlib.suppress(ValueError):
            expense = await repo.get_by_id(uuid.UUID(str(expense_id_raw)), action.tenant_id)
            if expense is not None:
                return [expense]

    # ── Fallback: identificación por fecha + monto + descripción ──────────────
    q = select(ExpenseEntry).where(
        ExpenseEntry.tenant_id == action.tenant_id,
        ExpenseEntry.voided_at.is_(None),
    )
    monto_raw = payload.get("monto")
    if monto_raw:
        with contextlib.suppress(Exception):
            q = q.where(ExpenseEntry.amount == Decimal(str(monto_raw)))
    fecha_raw = payload.get("fecha")
    if fecha_raw:
        from sqlalchemy import func  # noqa: PLC0415

        try:
            fecha = datetime.fromisoformat(str(fecha_raw)).date()
            q = q.where(func.date(ExpenseEntry.transaction_date) == fecha)
        except ValueError:
            pass
    q = q.order_by(ExpenseEntry.transaction_date.desc()).limit(1)
    result = await db.execute(q)
    expense = result.scalar_one_or_none()
    return [expense] if expense is not None else []


async def _execute_reclassify_expense(
    action: PendingAction,
    db: AsyncSession,
    payload: dict[str, Any],
) -> None:
    """Reclasifica uno o varios gastos in-place (category + expense_type) y, si es
    reventa, crea/vincula un Product vendible incompleto por gasto.

    Soporta reclasificación MASIVA vía ``payload["expense_ids"]`` (Workstream C3),
    además del singular ``expense_id`` y el fallback por fecha+monto+descripción.

    0 resultados → ``ReclassifyExpenseError`` (FAILED visible, NO return silencioso).

    Reversible: el audit_log guarda el estado anterior (``before``) por cada gasto.
    """
    from decimal import Decimal  # noqa: PLC0415

    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        build_incomplete_product,
    )
    from app.domain.expense_categories import infer_expense_type  # noqa: PLC0415
    from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
        ExpenseRepository,
    )

    target = str(payload.get("target") or "categoria").lower()
    base_category = str(payload.get("category") or "OTHER")
    product_policy = str(payload.get("product_policy") or "auto").lower()
    repo = ExpenseRepository(db)

    expenses = await _resolve_reclassify_target_expenses(action, db, payload)

    if not expenses:
        logger.warning(
            "execute_pending_action: RECLASSIFY_EXPENSE expense not found",
            action_id=str(action.id),
            payload=payload,
        )
        raise ReclassifyExpenseError(ReclassifyExpenseError.user_message)

    reclassified_ids: list[str] = []
    for expense in expenses:
        category = base_category
        before = {
            "category": expense.category,
            "expense_type": expense.expense_type,
            "product_id": str(expense.product_id) if expense.product_id else None,
        }

        # ── Reventa: crear/vincular un producto vendible incompleto ────────────
        product_id: uuid.UUID | None = None
        if target == "reventa":
            product_id = expense.product_id  # reusar si ya estaba vinculado
            if product_id is None and product_policy != "skip":
                unit_cost_raw = payload.get("unit_cost")
                unit_cost = (
                    Decimal(str(unit_cost_raw)) if unit_cost_raw not in (None, "") else None
                )
                # F5-A: si el SKU ya está tomado por un producto activo, el helper
                # devuelve ESE producto en vez de romper — reclasificar un gasto a
                # reventa apuntando al producto que ya existe es exactamente lo que
                # el usuario quiere. (``_created`` no se usa: acá no se reporta
                # cuántos productos se crearon.)
                product_id, _created = await build_incomplete_product(
                    session=db,
                    tenant_id=action.tenant_id,
                    name=payload.get("product_name") or expense.description,
                    sku=payload.get("sku"),
                    unit_cost=unit_cost,
                )
            category = "INVENTORY"

        expense_type = infer_expense_type(category, product_id=product_id)
        await repo.reclassify(
            expense,
            category=category,
            expense_type=expense_type,
            product_id=product_id,
        )
        reclassified_ids.append(str(expense.id))

        audit = DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=action.tenant_id,
            decision_type="EXPENSE_RECLASSIFIED",
            decision_data={
                "pending_action_id": str(action.id),
                "expense_id": str(expense.id),
                "before": before,
                "after": {
                    "category": category,
                    "expense_type": expense_type,
                    "product_id": str(product_id) if product_id else None,
                },
                "target": target,
            },
            triggered_by="agent:confirm",
            actor_user_id=action.user_id,
            context={"action_type": str(action.action_type)},
            created_at=datetime.now(UTC),
        )
        db.add(audit)

    logger.info(
        "expense_reclassified",
        action_id=str(action.id),
        expense_ids=reclassified_ids,
        count=len(reclassified_ids),
        target=target,
        category=base_category,
    )


async def _execute_prepare_whatsapp(
    action: PendingAction,
    db: AsyncSession,
    payload: dict[str, Any],
) -> None:
    """Construye el link wa.me y registra CommunicationLog + ExternalOperationLog.

    Gate de aislamiento: valida que el recipient_id pertenece al tenant antes de
    continuar. Si no pertenece → ValueError("recipient_not_in_tenant") → FAILED.
    No hace HTTP a Meta. El link wa.me lo abre el frontend.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.application.services.external_operation_service import (  # noqa: PLC0415
        record_external_operation,
    )
    from app.integrations.communication.whatsapp_channel import (  # noqa: PLC0415
        WhatsAppChannel,
    )
    from app.persistence.models.communication_log import CommunicationLog  # noqa: PLC0415
    from app.persistence.models.customer import Customer  # noqa: PLC0415

    recipient_id = uuid.UUID(payload["recipient_id"])

    # ── Gate de aislamiento: validar que el customer pertenece al tenant ──────
    result = await db.execute(
        select(Customer).where(
            Customer.id == recipient_id,
            Customer.tenant_id == action.tenant_id,
        )
    )
    customer = result.scalar_one_or_none()
    if customer is None:
        logger.warning(
            "execute_prepare_whatsapp: recipient_not_in_tenant",
            action_id=str(action.id),
            recipient_id=str(recipient_id),
            tenant_id=str(action.tenant_id),
        )
        raise ValueError("recipient_not_in_tenant")

    to = payload.get("to") or (customer.phone or "")
    body = str(payload.get("body") or "")

    # ── Construir link wa.me (sin HTTP) ──────────────────────────────────────
    url = WhatsAppChannel.build_click_to_chat_link(to, body)

    # ── Registrar en CommunicationLog ────────────────────────────────────────
    comm_log = CommunicationLog(
        tenant_id=action.tenant_id,
        recipient_type="customer",
        recipient_id=recipient_id,
        channel="whatsapp",
        body=body,
        status="sent",
    )
    db.add(comm_log)

    # ── Registrar en ExternalOperationLog ────────────────────────────────────
    await record_external_operation(
        db,
        tenant_id=action.tenant_id,
        operation_type="whatsapp_clicklink",
        provider="whatsapp",
        status="sent",
        user_id=action.user_id,
        request_payload={"to": to},
        response_payload={"url": url},
    )

    # ── Stash del resultado para el frontend ─────────────────────────────────
    action.payload = {
        **(action.payload or {}),
        "result": {"url": url, "body": body, "to": to, "channel": "whatsapp"},
    }

    logger.info(
        "prepare_whatsapp_link_built",
        action_id=str(action.id),
        recipient_id=str(recipient_id),
        tenant_id=str(action.tenant_id),
    )


async def execute_pending_action(
    action: PendingAction,
    db: AsyncSession,
    redis: Redis | None = None,
) -> None:
    """
    Ejecuta la acción de negocio y registra en audit_log.
    """
    payload = action.payload or {}
    conversation_id = str(payload.get("conversation_id") or "direct")
    mem = ChatMemoryService()

    if action.action_type == ActionType.REGISTER_SALE:
        sale = await cash_service.save_sale(payload, action.tenant_id, action.user_id, db)
        # Descuento de stock SÍNCRONO (primario), en la misma transacción que la venta.
        # Idempotente por source_event_id="sale:{id}": el evento SALE_RECORDED (backstop)
        # pasa por el mismo helper y no puede doble-descontar.
        await stock_service.decrement_for_sale(sale, db)
        if payload.get("conversation_id"):
            await mem.record(
                db=db,
                redis=redis,
                tenant_id=action.tenant_id,
                session_id=conversation_id,
                entry_type="DATA_LOADED",
                description=f"Venta registrada por chat: ${sale.amount}",
                entity_type="sale",
                entity_count=1,
                pending_action_id=action.id,
            )
        # Backstop del descuento: emitir DESPUÉS del commit (no antes) — si el task
        # async corriera antes, no encontraría el SaleEntry y terminaría sin reintentar.
        EventBus.emit_after_commit(
            db, "SALE_RECORDED", {"sale_id": str(sale.id), "tenant_id": str(action.tenant_id)}
        )
        try:
            if redis is not None:
                from decimal import Decimal  # noqa: PLC0415

                from app.application.services.business_memory_service import (
                    BusinessMemoryService,  # noqa: PLC0415
                )

                amount = Decimal(str(payload.get("amount", 0)))
                bm_svc = BusinessMemoryService(db=db, redis=redis)
                await bm_svc.update_after_sale(action.tenant_id, amount)
        except Exception:
            logger.warning("execute_pending_action.biz_mem_sale_failed", action_id=str(action.id))

    elif action.action_type == ActionType.REGISTER_CASH_INFLOW:
        inflow = await cash_service.save_cash_inflow(payload, action.tenant_id, db)
        if payload.get("conversation_id"):
            await mem.record(
                db=db,
                redis=redis,
                tenant_id=action.tenant_id,
                session_id=conversation_id,
                entry_type="DATA_LOADED",
                description=f"Cobro registrado por chat: ${inflow.amount}",
                entity_type="cash_inflow",
                entity_count=1,
                pending_action_id=action.id,
            )

    elif action.action_type == ActionType.REGISTER_EXPENSE:
        expense = await cash_service.save_expense(payload, action.tenant_id, db)
        if payload.get("conversation_id"):
            await mem.record(
                db=db,
                redis=redis,
                tenant_id=action.tenant_id,
                session_id=conversation_id,
                entry_type="DATA_LOADED",
                description=f"Gasto registrado por chat: ${expense.amount}",
                entity_type="expense",
                entity_count=1,
                pending_action_id=action.id,
            )
        try:
            if redis is not None:
                from decimal import Decimal  # noqa: PLC0415

                from app.application.services.business_memory_service import (
                    BusinessMemoryService,  # noqa: PLC0415
                )

                amount = Decimal(str(payload.get("amount", 0)))
                category = payload.get("category", "")
                bm_svc = BusinessMemoryService(db=db, redis=redis)
                await bm_svc.update_after_expense(action.tenant_id, amount, category)
        except Exception:
            logger.warning(
                "execute_pending_action.biz_mem_expense_failed",
                action_id=str(action.id),
            )

    elif action.action_type == ActionType.REGISTER_PURCHASE:
        # Compra de mercadería: categoría canónica + COGS (muere el literal
        # "compra_proveedor", que quedaba fuera del catálogo).
        purchase_payload = {**payload, "category": "INVENTORY", "expense_type": "COGS"}
        purchase_expense = await cash_service.save_expense(
            purchase_payload, action.tenant_id, db
        )

        product_id_str = payload.get("product_id")
        qty = int(payload.get("qty") or 0)
        if product_id_str and qty > 0:
            from decimal import Decimal  # noqa: PLC0415

            product_uuid = uuid.UUID(product_id_str)
            unit_cost_raw = payload.get("unit_cost")
            unit_cost = Decimal(str(unit_cost_raw)) if unit_cost_raw is not None else None
            await stock_service.increment_stock(
                product_id=product_uuid,
                tenant_id=action.tenant_id,
                qty=qty,
                unit_cost=unit_cost,
                source_event_id=str(action.id),
                db=db,
                # Fecha de negocio de la compra: la misma que quedó en el gasto
                # COGS recién creado (ya coercionada por cash_service).
                occurred_at=purchase_expense.transaction_date,
            )

    elif action.action_type == ActionType.REGISTER_CASH_OUTFLOW:
        outflow_payload = {**payload, "category": payload.get("category", "salida_caja")}
        await cash_service.save_expense(outflow_payload, action.tenant_id, db)

    elif action.action_type == ActionType.UPDATE_STOCK:
        product_id_str = payload.get("product_id")
        qty_change: int = int(payload.get("qty_change") or 0)
        if product_id_str and qty_change != 0:
            product_uuid = uuid.UUID(product_id_str)
            if qty_change < 0:
                await stock_service.decrement_stock(
                    product_id=product_uuid,
                    tenant_id=action.tenant_id,
                    qty=abs(qty_change),
                    source_event_id=str(action.id),
                    db=db,
                )
            else:
                unit_cost = payload.get("unit_cost")
                from decimal import Decimal  # noqa: PLC0415

                await stock_service.increment_stock(
                    product_id=product_uuid,
                    tenant_id=action.tenant_id,
                    qty=qty_change,
                    unit_cost=Decimal(str(unit_cost)) if unit_cost is not None else None,
                    source_event_id=str(action.id),
                    db=db,
                )
        else:
            logger.warning(
                "execute_pending_action: UPDATE_STOCK missing product_id or qty_change",
                action_id=str(action.id),
                payload=payload,
            )

    elif action.action_type == ActionType.REGISTER_STOCK_LOSS:
        product_id_str = payload.get("product_id")
        qty = abs(int(payload.get("qty_change") or 0))
        reason = payload.get("reason", "merma")
        if product_id_str and qty > 0:
            await stock_service.register_stock_loss(
                product_id=uuid.UUID(product_id_str),
                tenant_id=action.tenant_id,
                qty=qty,
                reason=reason,
                actor_user_id=action.user_id,
                db=db,
            )
        else:
            logger.warning(
                "execute_pending_action: REGISTER_STOCK_LOSS missing product_id or qty",
                action_id=str(action.id),
                payload=payload,
            )

    elif action.action_type == ActionType.UPDATE_PRODUCT:
        product_id_str = payload.get("product_id")
        _updatable_fields = {
            "sale_price_ars",
            "unit_cost_ars",
            "low_stock_threshold_units",
            "is_active",
            "name",
            "category",
            "stock_units",
            "sku",
        }
        update_fields = {
            k: v for k, v in payload.items() if k in _updatable_fields and v is not None
        }
        if product_id_str and update_fields:
            from decimal import Decimal  # noqa: PLC0415

            from app.persistence.repositories.product_repository import (
                ProductRepository,  # noqa: PLC0415
            )

            try:
                product_uuid = uuid.UUID(product_id_str)
            except ValueError:
                logger.warning(
                    "execute_pending_action: UPDATE_PRODUCT invalid product_id UUID",
                    action_id=str(action.id),
                    product_id=product_id_str,
                )
                return
            repo = ProductRepository(db)
            product = await repo.get_by_id(product_uuid, action.tenant_id)
            if product:
                for field, value in update_fields.items():
                    if field in ("sale_price_ars", "unit_cost_ars") and value is not None:
                        setattr(product, field, Decimal(str(value)))
                    else:
                        setattr(product, field, value)
                await repo.save(product)
                # Solo recalcular score cuando cambian campos que afectan métricas financieras
                _score_relevant = {"sale_price_ars", "unit_cost_ars", "stock_units"}
                if update_fields.keys() & _score_relevant:
                    from app.jobs.score_trigger import trigger_score_recalculation  # noqa: PLC0415

                    trigger_score_recalculation.delay(str(action.tenant_id), "product_updated")
            else:
                logger.warning(
                    "execute_pending_action: UPDATE_PRODUCT product not found",
                    action_id=str(action.id),
                    product_id=product_id_str,
                )
        else:
            logger.warning(
                "execute_pending_action: UPDATE_PRODUCT missing product_id or fields",
                action_id=str(action.id),
                payload=payload,
            )

    elif action.action_type == ActionType.IMPORT_TABULAR_FILE:
        file_id = payload.get("file_id")
        parsed_records = payload.get("parsed_records")

        if file_id:
            from sqlalchemy import select  # noqa: PLC0415

            result = await db.execute(
                select(UploadedFile).where(
                    UploadedFile.id == uuid.UUID(str(file_id)),
                    UploadedFile.tenant_id == action.tenant_id,
                )
            )
            uploaded_file = result.scalar_one_or_none()
            if uploaded_file is None:
                raise ValueError("IMPORT_TABULAR_FILE archivo no encontrado")
            if not isinstance(uploaded_file.parsed_summary_json, dict):
                raise ValueError("IMPORT_TABULAR_FILE archivo sin parsed_summary_json")

            summary = uploaded_file.parsed_summary_json
            confirmed_fields = payload.get("confirmed_fields")
            if not isinstance(confirmed_fields, dict) or not confirmed_fields:
                # Sin confirmación explícita en el payload: usar los MISMOS
                # defaults que aplica insert_confirmed_data, para que el check
                # anti-vacío evalúe lo que realmente se intentó importar (con {}
                # el check quedaba muerto y un import de 0 filas pasaba como OK).
                confirmed_fields = default_confirmed_fields(summary)

            counts = await insert_confirmed_data(
                session=db,
                tenant_id=action.tenant_id,
                summary=summary,
                confirmed_fields=confirmed_fields,
                source="chat",
                uploaded_file_id=uploaded_file.id,
            )
            # Import vacío con datos presentes → falla visible (la pending action
            # queda FAILED y el archivo NO se marca DONE).
            check_nonempty_import(counts, summary, confirmed_fields)
            uploaded_file.processing_status = PROCESSING_STATUS_DONE
            # F9a (hallazgo post-review): este es el confirm vía CHAT — un path
            # de escritura directa separado del confirm HTTP normal, que NO pasa
            # por `finalize_import_lease()` (la función que stampea
            # `ingestion_version`). Sin esto, todo archivo confirmado por este
            # camino quedaba en el `ingestion_version` default (1) para siempre,
            # aunque se haya procesado con la lógica F8+ actual — el batch de
            # `scripts/reanalyze_ingestion.py` lo re-seleccionaría como candidato
            # "pre-F8" en cada corrida, re-descargando de S3 innecesariamente.
            uploaded_file.ingestion_version = INGESTION_VERSION
            # Compactar antes de escribir de vuelta: los buckets de filas pueden
            # pesar varios MB de JSONB (mismo criterio que el confirm de la API).
            uploaded_file.parsed_summary_json = {
                **{
                    k: v
                    for k, v in summary.items()
                    if k
                    not in (
                        "ventas_detectadas",
                        "gastos_detectados",
                        "stock_detectado",
                        "otros_detectados",
                        "preview_rows",
                        "mapping_contexts",
                        # F7d review (Important): mismo criterio que el confirm de
                        # la API — estos buckets traen PII cruda (nombre/DNI/CUIT/
                        # email/teléfono) y no hace falta que sobrevivan al import.
                        "clientes_detectados",
                        "proveedores_detectados",
                    )
                },
                "confirmed_fields": confirmed_fields,
                "imported_counts": counts,
            }
            description = f"Importado: {uploaded_file.original_filename} — {counts}"
            entity_type = summary.get("inferred_type")
            uploaded_file_id = uploaded_file.id

        elif isinstance(parsed_records, list):
            record_type = str(payload.get("record_type") or "sales")
            summary = _summary_from_tabular_records(parsed_records, record_type)
            confirmed_fields = payload.get("confirmed_fields")
            if not isinstance(confirmed_fields, dict):
                confirmed_fields = _confirmed_fields_for_record_type(record_type)

            counts = await insert_confirmed_data(
                session=db,
                tenant_id=action.tenant_id,
                summary=summary,
                confirmed_fields=confirmed_fields,
                source="chat",
            )
            check_nonempty_import(counts, summary, confirmed_fields)
            source = payload.get("source") or "tabular"
            description = f"Importado desde {source}: {counts}"
            entity_type = summary.get("inferred_type")
            uploaded_file_id = None

        else:
            raise ValueError("IMPORT_TABULAR_FILE sin file_id ni parsed_records")

        # F7d: `counts` ganó desgloses de la resolución de referencia por fila
        # (ventas_cliente_*/compras_proveedor_*) y del import de maestro
        # (clientes/proveedores_creados|actualizados|needs_review|invalidos) —
        # son sub-clasificaciones de "ventas"/"gastos"/"clientes"/"proveedores",
        # no registros adicionales. Sumar TODO el dict inflaba el conteo (cada
        # venta se cuenta una vez en "ventas" y otra vez en su bucket de
        # resolución). Acá contamos registros efectivamente cargados.
        entity_count = (
            counts.get("ventas", 0)
            + counts.get("gastos", 0)
            + counts.get("productos", 0)
            + counts.get("clientes", 0)
            + counts.get("proveedores", 0)
            + counts.get("otros", 0)
        )
        await mem.record(
            db=db,
            redis=redis,
            tenant_id=action.tenant_id,
            session_id=conversation_id,
            entry_type="DATA_LOADED",
            description=description,
            entity_type=entity_type,
            entity_count=entity_count,
            uploaded_file_id=uploaded_file_id,
            pending_action_id=action.id,
        )

    elif action.action_type == ActionType.CREATE_SUPPLIER_DRAFT:
        if payload.get("mode") == "mcp":
            broker = _make_google_broker(action)
            email_mode = str(payload.get("email_mode") or "draft").lower()
            if email_mode == "send":
                await broker.send_gmail_message(
                    to=[payload.get("to", "")],
                    subject=payload.get("subject", "Consulta de proveedor"),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
            elif email_mode == "reply":
                await broker.reply_gmail_message(
                    message_id=payload.get("message_id", ""),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
            else:
                await broker.create_gmail_draft(
                    to=[payload.get("to", "")],
                    subject=payload.get("subject", "Consulta de proveedor"),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.CLASSIFY_GMAIL_MESSAGE:
        if payload.get("mode") == "mcp":
            broker = _make_google_broker(action)
            await broker.get_gmail_message(message_id=payload.get("message_id", ""))
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.SYNC_TO_GOOGLE:
        if payload.get("mode") == "mcp":
            sync_type = payload.get("sync_type", "")
            broker = _make_google_broker(action)
            if sync_type == "export_sales_to_sheets":
                await broker.append_sheet_rows(
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                    rows=payload.get("values", []),
                )
            elif sync_type == "export_report_to_docs":
                doc = await broker.create_doc(title=payload.get("title", "Reporte Véktor"))
                content = payload.get("content", "")
                document_id = doc.get("document_id") or doc.get("id")
                if content and document_id:
                    await broker.append_doc_content(document_id=document_id, content=content)
            elif sync_type == "import_from_sheets":
                await broker.read_sheet_range(
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                )
            elif sync_type == "import_from_drive":
                files = await broker.list_drive_files(
                    query=payload.get("query") or payload.get("raw_message", ""),
                    max_results=int(payload.get("max_results", 5)),
                )
                if files:
                    first_file_id = str(files[0].get("id", "")).strip()
                    if first_file_id:
                        await broker.read_drive_file(file_id=first_file_id)
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.CREATE_CALENDAR_EVENT:
        if payload.get("mode") == "mcp":
            broker = _make_google_broker(action)
            await broker.create_calendar_event(
                summary=payload.get("summary", "Evento"),
                start=payload.get("start_datetime", payload.get("start", "")),
                end=payload.get("end_datetime", payload.get("end", "")),
                attendees=payload.get("attendees", []),
            )
            if payload.get("send_email") or payload.get("email_mode") == "send":
                recipients = payload.get("email_recipients") or payload.get("attendees") or []
                if recipients:
                    await broker.send_gmail_message(
                        to=recipients,
                        subject=payload.get(
                            "email_subject", payload.get("summary", "Recordatorio")
                        ),
                        body=payload.get(
                            "email_body",
                            payload.get("description", "Recordatorio creado en Google Calendar."),
                        ),
                    )
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.RECLASSIFY_EXPENSE:
        await _execute_reclassify_expense(action, db, payload)

    elif action.action_type == ActionType.PREPARE_WHATSAPP_MESSAGE:
        await _execute_prepare_whatsapp(action, db, payload)

    elif action.action_type == ActionType.UPLOAD_TO_DRIVE:
        broker = _make_google_broker(action)
        await broker.upload_drive_file(
            name=payload.get("filename", payload.get("name", "archivo.bin")),
            content_base64=payload.get("content_base64", ""),
            mime_type=payload.get("mime_type", "application/octet-stream"),
            folder_id=payload.get("folder_id"),
        )
        action.external_system = "GOOGLE_DRIVE"

    elif action.action_type == ActionType.CREATE_GOOGLE_DOC:
        broker = _make_google_broker(action)
        doc = await broker.create_doc(title=payload.get("title", "Documento Véktor"))
        content = payload.get("content", "")
        document_id = doc.get("document_id") or doc.get("id")
        if content and document_id:
            await broker.append_doc_content(document_id=document_id, content=content)
        action.external_system = "GOOGLE_DOCS"

    elif action.action_type == ActionType.APPEND_TO_SHEET:
        broker = _make_google_broker(action)
        await broker.append_sheet_rows(
            spreadsheet_id=payload.get("spreadsheet_id", ""),
            range_name=payload.get("range_name", "Sheet1"),
            rows=payload.get("rows", []),
        )
        action.external_system = "GOOGLE_SHEETS"

    else:
        logger.warning(
            "execute_pending_action: action_type has no executor yet",
            action_type=action.action_type,
            action_id=str(action.id),
        )

    # Registrar acción en AgentMemory (fail-silencioso)
    try:
        if redis is not None:
            from app.application.services.agent_memory_service import (
                AgentMemoryService,  # noqa: PLC0415
            )

            am_svc = AgentMemoryService(db=db, redis=redis)
            await am_svc.record_action(action.tenant_id, action.action_type, payload)
    except Exception:
        logger.warning("execute_pending_action.agent_mem_failed", action_id=str(action.id))

    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=action.tenant_id,
        decision_type="AGENT_ACTION_EXECUTED",
        decision_data={
            "pending_action_id": str(action.id),
            "action_type": action.action_type,
            "payload": action.payload,
            "risk_level": action.risk_level,
        },
        triggered_by="agent:confirm",
        actor_user_id=action.user_id,
        context={"status_before": action.status},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    logger.info(
        "pending_action_executed",
        action_id=str(action.id),
        action_type=action.action_type,
        tenant_id=str(action.tenant_id),
    )


async def cancel_pending_action(
    action: PendingAction,
    db: AsyncSession,
) -> None:
    """Registra el rechazo en audit_log."""
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=action.tenant_id,
        decision_type="AGENT_ACTION_REJECTED",
        decision_data={
            "pending_action_id": str(action.id),
            "action_type": action.action_type,
            "payload": action.payload,
            "risk_level": action.risk_level,
        },
        triggered_by="agent:cancel",
        actor_user_id=action.user_id,
        context={"status_before": action.status},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    logger.info(
        "pending_action_cancelled",
        action_id=str(action.id),
        action_type=action.action_type,
        tenant_id=str(action.tenant_id),
    )
