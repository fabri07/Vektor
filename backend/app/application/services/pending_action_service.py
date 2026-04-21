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

import uuid
from datetime import UTC, datetime, timedelta
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType
import app.application.services.cash_service as cash_service
import app.application.services.stock_service as stock_service
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction

logger = get_logger(__name__)



async def create_pending_action(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    action_type: str,
    payload: dict,
    risk_level: str,
) -> PendingAction:
    """Crea un PendingAction con TTL de 10 minutos. Hace flush para obtener el id.

    """
    action = PendingAction(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type=action_type,
        payload=payload,
        risk_level=risk_level,
        status="PENDING",
        external_system=None,
        idempotency_key=None,
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


async def execute_pending_action(
    action: PendingAction,
    db: AsyncSession,
    redis: Optional[Redis] = None,
) -> None:
    """
    Ejecuta la acción de negocio y registra en audit_log.
    """
    payload = action.payload or {}

    if action.action_type == ActionType.REGISTER_SALE:
        sale = await cash_service.save_sale(payload, action.tenant_id, action.user_id, db)
        from app.application.agents.cash.agent import AgentCash  # noqa: PLC0415
        await AgentCash().on_confirmed_sale(str(sale.id), str(action.tenant_id))
        try:
            if redis is not None:
                from decimal import Decimal  # noqa: PLC0415
                from app.application.services.business_memory_service import BusinessMemoryService  # noqa: PLC0415
                amount = Decimal(str(payload.get("amount", 0)))
                bm_svc = BusinessMemoryService(db=db, redis=redis)
                await bm_svc.update_after_sale(action.tenant_id, amount)
        except Exception:
            logger.warning("execute_pending_action.biz_mem_sale_failed", action_id=str(action.id))

    elif action.action_type == ActionType.REGISTER_CASH_INFLOW:
        await cash_service.save_cash_inflow(payload, action.tenant_id, db)

    elif action.action_type == ActionType.REGISTER_EXPENSE:
        await cash_service.save_expense(payload, action.tenant_id, db)
        try:
            if redis is not None:
                from decimal import Decimal  # noqa: PLC0415
                from app.application.services.business_memory_service import BusinessMemoryService  # noqa: PLC0415
                amount = Decimal(str(payload.get("amount", 0)))
                category = payload.get("category", "")
                bm_svc = BusinessMemoryService(db=db, redis=redis)
                await bm_svc.update_after_expense(action.tenant_id, amount, category)
        except Exception:
            logger.warning("execute_pending_action.biz_mem_expense_failed", action_id=str(action.id))

    elif action.action_type == ActionType.REGISTER_PURCHASE:
        purchase_payload = {**payload, "category": "compra_proveedor"}
        await cash_service.save_expense(purchase_payload, action.tenant_id, db)

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


    elif action.action_type == ActionType.CREATE_SUPPLIER_DRAFT:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.config.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(base_url=settings.MCP_SERVER_URL, timeout=settings.MCP_HTTP_TIMEOUT)
            svc = GoogleMcpService(gateway=gateway, agent_name="agent_supplier")
            await svc.create_gmail_draft(
                tenant_id=action.tenant_id,
                to=[payload.get("to", "")],
                subject=payload.get("subject", "Consulta de proveedor"),
                body=payload.get("body", payload.get("message", "")),
            )
        action.external_system = "GOOGLE_GMAIL" if mcp_enabled else None

    elif action.action_type == ActionType.CLASSIFY_GMAIL_MESSAGE:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.config.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(base_url=settings.MCP_SERVER_URL, timeout=settings.MCP_HTTP_TIMEOUT)
            svc = GoogleMcpService(gateway=gateway, agent_name="agent_supplier")
            await svc.get_gmail_message(
                tenant_id=action.tenant_id,
                message_id=payload.get("message_id", ""),
            )
        action.external_system = "GOOGLE_GMAIL" if mcp_enabled else None

    elif action.action_type == ActionType.SYNC_TO_GOOGLE:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            sync_type = payload.get("sync_type", "")
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.config.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(base_url=settings.MCP_SERVER_URL, timeout=settings.MCP_HTTP_TIMEOUT)
            svc = GoogleMcpService(gateway=gateway, agent_name="agent_sync")
            if sync_type == "export_sales_to_sheets":
                await svc.append_sheets_rows(
                    tenant_id=action.tenant_id,
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                    values=payload.get("values", []),
                )
            elif sync_type == "export_report_to_docs":
                await svc.create_docs_document(
                    tenant_id=action.tenant_id,
                    title=payload.get("title", "Reporte Véktor"),
                    content=payload.get("content", ""),
                )
            elif sync_type == "import_from_sheets":
                await svc.read_sheets_range(
                    tenant_id=action.tenant_id,
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                )
        action.external_system = "GOOGLE_SHEETS" if mcp_enabled else None

    elif action.action_type == ActionType.CREATE_CALENDAR_EVENT:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.config.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(base_url=settings.MCP_SERVER_URL, timeout=settings.MCP_HTTP_TIMEOUT)
            svc = GoogleMcpService(gateway=gateway, agent_name="agent_calendar")
            await svc.create_calendar_event(
                tenant_id=action.tenant_id,
                summary=payload.get("summary", "Evento"),
                start_datetime=payload.get("start_datetime", ""),
                end_datetime=payload.get("end_datetime", ""),
                attendees=payload.get("attendees", []),
            )
        action.external_system = "GOOGLE_CALENDAR" if mcp_enabled else None

    else:
        logger.warning(
            "execute_pending_action: action_type has no executor yet",
            action_type=action.action_type,
            action_id=str(action.id),
        )

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
