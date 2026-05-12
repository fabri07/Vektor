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
from app.application.services.chat_memory_service import ChatMemoryService
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
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


def _summary_from_tabular_records(records: list, record_type: str) -> dict:
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
    conversation_id = str(payload.get("conversation_id") or "direct")
    mem = ChatMemoryService()

    if action.action_type == ActionType.REGISTER_SALE:
        sale = await cash_service.save_sale(payload, action.tenant_id, action.user_id, db)
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

    elif action.action_type == ActionType.UPDATE_PRODUCT:
        product_id_str = payload.get("product_id")
        _UPDATABLE_FIELDS = {
            "sale_price_ars", "unit_cost_ars", "low_stock_threshold_units",
            "is_active", "name", "category", "stock_units", "sku",
        }
        update_fields = {
            k: v for k, v in payload.items()
            if k in _UPDATABLE_FIELDS and v is not None
        }
        if product_id_str and update_fields:
            from decimal import Decimal  # noqa: PLC0415
            from app.persistence.repositories.product_repository import ProductRepository  # noqa: PLC0415
            repo = ProductRepository(db)
            product = await repo.get_by_id(uuid.UUID(product_id_str), action.tenant_id)
            if product:
                for field, value in update_fields.items():
                    if field in ("sale_price_ars", "unit_cost_ars") and value is not None:
                        setattr(product, field, Decimal(str(value)))
                    else:
                        setattr(product, field, value)
                await repo.save(product)
                # Solo recalcular score cuando cambian campos que afectan métricas financieras
                _SCORE_RELEVANT = {"sale_price_ars", "unit_cost_ars", "stock_units"}
                if update_fields.keys() & _SCORE_RELEVANT:
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

            confirmed_fields = payload.get("confirmed_fields")
            if not isinstance(confirmed_fields, dict):
                confirmed_fields = {}

            summary = uploaded_file.parsed_summary_json
            counts = await insert_confirmed_data(
                session=db,
                tenant_id=action.tenant_id,
                summary=summary,
                confirmed_fields=confirmed_fields,
            )
            uploaded_file.processing_status = PROCESSING_STATUS_DONE
            uploaded_file.parsed_summary_json = {
                **summary,
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
            )
            source = payload.get("source") or "tabular"
            description = f"Importado desde {source}: {counts}"
            entity_type = summary.get("inferred_type")
            uploaded_file_id = None

        else:
            raise ValueError("IMPORT_TABULAR_FILE sin file_id ni parsed_records")

        await mem.record(
            db=db,
            redis=redis,
            tenant_id=action.tenant_id,
            session_id=conversation_id,
            entry_type="DATA_LOADED",
            description=description,
            entity_type=entity_type,
            entity_count=sum(counts.values()),
            uploaded_file_id=uploaded_file_id,
            pending_action_id=action.id,
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
