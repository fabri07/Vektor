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

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.cash_service as cash_service
import app.application.services.stock_service as stock_service
from app.application.agents.shared.schemas import ActionType
from app.application.services.automation_service import determine_external_system
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction

logger = get_logger(__name__)


def _external_idempotency_key(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    action_type: str,
    payload: dict,
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
    external_system = (
        determine_external_system(action_type, payload)
        if payload.get("mode") == "mcp"
        else None
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


async def execute_pending_action(
    action: PendingAction,
    db: AsyncSession,
    redis: Redis | None = None,
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

                from app.application.services.business_memory_service import (
                    BusinessMemoryService,  # noqa: PLC0415
                )
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
        purchase_payload = {**payload, "category": "compra_proveedor"}
        await cash_service.save_expense(purchase_payload, action.tenant_id, db)

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


    elif action.action_type == ActionType.CREATE_SUPPLIER_DRAFT:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.config.settings import get_settings  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(settings=settings, user_id=str(action.user_id))
            svc = GoogleMcpService(
                gateway=gateway,
                agent_name="agent_supplier",
                tenant_id=str(action.tenant_id),
                settings=settings,
            )
            email_mode = str(payload.get("email_mode") or "draft").lower()
            if email_mode == "send":
                await svc.send_gmail_message(
                    to=[payload.get("to", "")],
                    subject=payload.get("subject", "Consulta de proveedor"),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
            elif email_mode == "reply":
                await svc.reply_gmail_message(
                    message_id=payload.get("message_id", ""),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
            else:
                await svc.create_gmail_draft(
                    to=[payload.get("to", "")],
                    subject=payload.get("subject", "Consulta de proveedor"),
                    body=payload.get("body", payload.get("message", "")),
                    cc=payload.get("cc") or [],
                )
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.CLASSIFY_GMAIL_MESSAGE:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.config.settings import get_settings  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(settings=settings, user_id=str(action.user_id))
            svc = GoogleMcpService(
                gateway=gateway,
                agent_name="agent_supplier",
                tenant_id=str(action.tenant_id),
                settings=settings,
            )
            await svc.get_gmail_message(
                message_id=payload.get("message_id", ""),
            )
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.SYNC_TO_GOOGLE:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            sync_type = payload.get("sync_type", "")
            from app.config.settings import get_settings  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(settings=settings, user_id=str(action.user_id))
            svc = GoogleMcpService(
                gateway=gateway,
                agent_name="agent_sync",
                tenant_id=str(action.tenant_id),
                settings=settings,
            )
            if sync_type == "export_sales_to_sheets":
                await svc.append_sheet_rows(
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                    rows=payload.get("values", []),
                )
            elif sync_type == "export_report_to_docs":
                doc = await svc.create_doc(
                    title=payload.get("title", "Reporte Véktor"),
                )
                content = payload.get("content", "")
                document_id = doc.get("document_id") or doc.get("id")
                if content and document_id:
                    await svc.append_doc_content(document_id=document_id, content=content)
            elif sync_type == "import_from_sheets":
                await svc.read_sheet_range(
                    spreadsheet_id=payload.get("spreadsheet_id", ""),
                    range_name=payload.get("range_name", "Sheet1"),
                )
            elif sync_type == "import_from_drive":
                files = await svc.list_drive_files(
                    query=payload.get("query") or payload.get("raw_message", ""),
                    max_results=int(payload.get("max_results", 5)),
                )
                if files:
                    first_file_id = str(files[0].get("id", "")).strip()
                    if first_file_id:
                        await svc.read_drive_file(file_id=first_file_id)
        action.external_system = determine_external_system(action.action_type, payload)

    elif action.action_type == ActionType.CREATE_CALENDAR_EVENT:
        mcp_enabled = payload.get("mode") == "mcp"
        if mcp_enabled:
            from app.config.settings import get_settings  # noqa: PLC0415
            from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415
            settings = get_settings()
            gateway = HttpMcpGateway(settings=settings, user_id=str(action.user_id))
            svc = GoogleMcpService(
                gateway=gateway,
                agent_name="agent_calendar",
                tenant_id=str(action.tenant_id),
                settings=settings,
            )
            await svc.create_calendar_event(
                summary=payload.get("summary", "Evento"),
                start=payload.get("start_datetime", payload.get("start", "")),
                end=payload.get("end_datetime", payload.get("end", "")),
                attendees=payload.get("attendees", []),
            )
            if payload.get("send_email") or payload.get("email_mode") == "send":
                recipients = payload.get("email_recipients") or payload.get("attendees") or []
                if recipients:
                    await svc.send_gmail_message(
                        to=recipients,
                        subject=payload.get(
                            "email_subject",
                            payload.get("summary", "Recordatorio"),
                        ),
                        body=payload.get(
                            "email_body",
                            payload.get("description", "Recordatorio creado en Google Calendar."),
                        ),
                    )
        action.external_system = determine_external_system(action.action_type, payload)

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
