"""AgentExpense — motor de egresos.

Responsabilidades:
- Registrar gastos operativos (REGISTER_EXPENSE)
- Registrar pagos y salidas de caja (REGISTER_CASH_OUTFLOW)
- Importar archivos de gastos (IMPORT_TABULAR_FILE)

Stage 2b: lógica real de egresos extraída de AgentCash.
AgentCash queda como shim delegante para PendingActions históricas.
"""

from __future__ import annotations

import re
import uuid
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.domain.expense_categories import (
    EXPENSE_CATEGORY_LABELS_ES,
    normalize_expense_category,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession


class AgentExpense(BaseAgent):
    agent_name = "agent_expense"

    def __init__(
        self,
        db: AsyncSession | None = None,
        redis: Redis | None = None,
        gateway: Any | None = None,
    ) -> None:
        self._db = db
        self._redis = redis
        self._gateway = gateway

    def _extract_amount(self, message: str) -> Decimal | None:
        match = re.search(r"(?<!\d)(?:\$?\s*)?(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?", message)
        if match is None:
            return None
        normalized = match.group(1).replace(".", "").replace(" ", "")
        try:
            return Decimal(normalized)
        except Exception:
            return None

    def _extract_payment_method(self, message: str) -> str:
        message_lower = message.lower()
        if "transfer" in message_lower:
            return "transfer"
        if "debito" in message_lower:
            return "debit_card"
        if "credito" in message_lower:
            return "credit_card"
        if "qr" in message_lower:
            return "qr"
        if "efectivo" in message_lower:
            return "cash"
        return "other"

    def _extract_expense_category(self, message: str) -> tuple[str, str]:
        # Primero el normalizador canónico (aliases es-AR del catálogo completo,
        # matching por palabra completa sobre el mensaje).
        code, _ = normalize_expense_category(message)
        if code != "OTHER":
            return code, EXPENSE_CATEGORY_LABELS_ES[code]
        # Red de keywords coloquiales que no son nombres de categoría.
        message_lower = message.lower()
        category_rules = (
            ("RENT", ("renta",), ),
            ("UTILITIES", ("servicio",), ),
            ("PAYROLL", ("empleado", "personal", "nomina", "nómina"), ),
            ("INVENTORY", ("proveedor", "compra"), ),
            ("MARKETING", ("anuncio", "ads"), ),
        )
        for canonical, keywords in category_rules:
            if any(keyword in message_lower for keyword in keywords):
                return canonical, EXPENSE_CATEGORY_LABELS_ES[canonical]
        return "OTHER", "Gasto"

    def _extract_transaction_date(self, message: str) -> str:
        message_lower = message.lower()
        today = date.today()

        iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", message_lower)
        if iso_match is not None:
            return iso_match.group(0)

        day_month_year = re.search(
            r"\b(\d{1,2})\s+de\s+"
            r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"\s+de\s+(20\d{2})\b",
            message_lower,
        )
        if day_month_year is not None:
            day = int(day_month_year.group(1))
            month = self._month_from_spanish(day_month_year.group(2))
            year = int(day_month_year.group(3))
            return date(year, month, day).isoformat()

        month_year = re.search(
            r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"\s+de\s+(20\d{2})\b",
            message_lower,
        )
        if month_year is not None:
            month = self._month_from_spanish(month_year.group(1))
            year = int(month_year.group(2))
            last_day = monthrange(year, month)[1]
            return date(year, month, min(today.day, last_day)).isoformat()

        # Sin fecha explícita en el mensaje: ahora (captura la hora del registro).
        return datetime.now().isoformat()

    def _month_from_spanish(self, value: str) -> int:
        month_map = {
            "enero": 1,
            "febrero": 2,
            "marzo": 3,
            "abril": 4,
            "mayo": 5,
            "junio": 6,
            "julio": 7,
            "agosto": 8,
            "septiembre": 9,
            "setiembre": 9,
            "octubre": 10,
            "noviembre": 11,
            "diciembre": 12,
        }
        return month_map[value]

    def _extract_expense_entities(self, message: str) -> dict[str, Any]:
        amount = self._extract_amount(message)
        if amount is None:
            return {
                "error": (
                    "No pude identificar el monto del gasto. Probá con algo como 'Pagué alquiler "
                    "$450.000'."
                )
            }
        category, label = self._extract_expense_category(message)
        return {
            "amount": str(amount),
            "category": category,
            "description": label,
            "transaction_date": self._extract_transaction_date(message),
            "payment_method": self._extract_payment_method(message),
            "is_recurring": any(
                token in message.lower() for token in ("mensual", "cada mes", "alquiler")
            ),
            "notes": message.strip(),
            "confidence": "HIGH",
        }

    async def _maybe_build_uploaded_file_import(
        self, request: AgentRequest
    ) -> AgentResponse | None:
        if self._db is None or not request.attachments:
            return None

        from sqlalchemy import select  # noqa: PLC0415

        from app.application.services.data_intent_extractor import (
            DataIntentExtractor,  # noqa: PLC0415
        )
        from app.persistence.models.file import UploadedFile  # noqa: PLC0415

        for attachment in request.attachments:
            file_id = attachment.get("file_id") if isinstance(attachment, dict) else None
            if not file_id:
                continue
            try:
                file_uuid = uuid.UUID(str(file_id))
                tenant_uuid = uuid.UUID(str(request.business_id))
            except ValueError:
                continue
            result = await self._db.execute(
                select(UploadedFile).where(
                    UploadedFile.id == file_uuid,
                    UploadedFile.tenant_id == tenant_uuid,
                )
            )
            uploaded_file = result.scalar_one_or_none()
            if uploaded_file is None or not isinstance(uploaded_file.parsed_summary_json, dict):
                continue
            pre_check = DataIntentExtractor().check_file_summary(uploaded_file.parsed_summary_json)
            if not pre_check.has_data_intent:
                continue
            rows = uploaded_file.parsed_summary_json.get("rows_processed", 0)
            # FASE F: archivo ambiguo ("unclassified") ruteado a este agente →
            # el usuario habló de gastos: se importa como gastos, con
            # consentimiento explícito en el summary de la aprobación.
            is_unclassified = pre_check.intent_type == "unclassified"
            summary_text = (
                f"Importar {rows} registros desde {uploaded_file.original_filename}."
            )
            if is_unclassified:
                summary_text = (
                    f"Importar {rows} registros desde {uploaded_file.original_filename} "
                    "(tipo no detectado automáticamente — se cargarán como GASTOS)."
                )
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                confidence=Confidence(pre_check.confidence),
                result={
                    "summary": summary_text,
                    "action_type": ActionType.IMPORT_TABULAR_FILE,
                    "structured_data": {
                        "source": "uploaded_file",
                        "file_id": str(uploaded_file.id),
                        "confirmed_fields": {
                            "ventas": pre_check.intent_type in ("sale", "mixed"),
                            "gastos": pre_check.intent_type in ("expense", "mixed")
                            or is_unclassified,
                            "productos": pre_check.intent_type == "product",
                        },
                    },
                    "alerts": [],
                },
            )
        return None

    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")

        # ── Sprint 17: análisis de gastos read-only ──────────────────────────
        if action_type == ActionType.ANALYZE_EXPENSE_DATA:
            return await self._handle_expense_analysis(request, analysis_intent)

        # ── Nivel 2: reclasificar gasto (asesoría read-only o escritura MEDIUM) ──
        if action_type == ActionType.RECLASSIFY_EXPENSE:
            return await self._handle_reclassify_expense(request, task)

        file_import = await self._maybe_build_uploaded_file_import(request)
        if file_import is not None:
            return file_import

        # Branch: pago de deuda / salida de caja (REGISTER_CASH_OUTFLOW)
        if action_type == ActionType.REGISTER_CASH_OUTFLOW:
            return await self._handle_cash_outflow(request, task)

        # Branch: gasto operativo (REGISTER_EXPENSE o default)
        # Usar entities pre-extraídas del CEO si están disponibles
        pre_entities = getattr(task, "entities", {}) or {}
        if pre_entities.get("amount"):
            entities: dict[str, Any] = {
                "amount": str(pre_entities["amount"]),
                "category": pre_entities.get("category", "OTHER"),
                "description": pre_entities.get("description", "Gasto"),
                "transaction_date": pre_entities.get("transaction_date", date.today().isoformat()),
                "payment_method": pre_entities.get("payment_method", "other"),
                "is_recurring": pre_entities.get("is_recurring", False),
                "notes": request.message.strip(),
                "confidence": "HIGH",
            }
        else:
            entities = self._extract_expense_entities(request.message)
        if "error" in entities:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=entities["error"],
                result={"summary": "Faltan datos para registrar el gasto."},
            )

        amount = entities["amount"]
        description = entities.get("description", "gasto")
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=False,
            confidence=Confidence.HIGH,
            result={
                "summary": f"Gasto registrado: {description} por ${amount}",
                "action_type": ActionType.REGISTER_EXPENSE,
                "structured_data": entities,
                "alerts": [],
                "auto_execute": True,
            },
        )

    # ── Sprint 17: handlers analíticos read-only (sin LLM, determinísticos) ────

    def _analysis_response(
        self,
        request: AgentRequest,
        summary: str,
        message: str,
        structured: dict[str, Any] | None = None,
        confidence: Confidence = Confidence.HIGH,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=confidence,
            message=message,
            result={"summary": summary, "structured_data": structured or {}, "analysis": True},
        )

    async def _handle_expense_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        if self._db is None:
            return self._analysis_response(
                request,
                "gastos_sin_datos",
                "Necesito acceso a tus gastos para analizarlos.",
            )
        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return self._analysis_response(
                request,
                "gastos_sin_datos",
                "No pude identificar tu negocio.",
            )

        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            ExpenseRepository,
        )

        repo = ExpenseRepository(self._db)

        # ── detectar_gastos_anomalos ──────────────────────────────────────────
        if intent == "detectar_gastos_anomalos":
            from app.application.agents.shared import analytics  # noqa: PLC0415

            stats = await repo.get_expense_stats_by_category(tenant_id, days=90)
            if not stats:
                return self._analysis_response(
                    request,
                    "gastos_sin_historial",
                    "Todavía no tengo suficientes gastos cargados (últimos 90 días) para "
                    "detectar valores fuera de lo normal.",
                )
            anomalies = analytics.detect_expense_anomalies(stats, sigma=2.0)
            if not anomalies:
                return self._analysis_response(
                    request,
                    "gastos_anomalos_ninguno",
                    "No detecté gastos fuera de lo normal en los últimos 90 días. "
                    "Tus gastos por categoría se mantienen consistentes.",
                )
            lines = ["Detecté gastos inusualmente altos (más de 2 desvíos sobre el promedio):"]
            for a in anomalies[:6]:
                top = a["outliers"][0]
                lines.append(
                    f"- {a['category']}: ${top:,.0f} (promedio ${a['mean']:,.0f}, "
                    f"{a['n_outliers']} caso/s)"
                )
            return self._analysis_response(
                request,
                "detectar_gastos_anomalos",
                "\n".join(lines),
                {"anomalies": anomalies},
            )

        # ── detectar_gastos_recurrentes ───────────────────────────────────────
        if intent == "detectar_gastos_recurrentes":
            from datetime import timedelta  # noqa: PLC0415

            from sqlalchemy import select  # noqa: PLC0415

            from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

            cutoff = date.today() - timedelta(days=90)
            rows = await self._db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.is_recurring.is_(True),
                    ExpenseEntry.transaction_date >= cutoff,
                )
            )
            recurring = list(rows.scalars().all())
            if not recurring:
                return self._analysis_response(
                    request,
                    "recurrentes_ninguno",
                    "No tenés gastos marcados como recurrentes en los últimos 90 días. "
                    "Los gastos fijos como alquiler o sueldos conviene marcarlos como "
                    "recurrentes para seguirlos mejor.",
                )
            by_supplier: dict[str, dict[str, Any]] = {}
            for e in recurring:
                key = (e.supplier_name or e.description or e.category or "Gasto fijo").strip()
                slot = by_supplier.setdefault(key, {"total": Decimal(0), "count": 0})
                slot["total"] += e.amount
                slot["count"] += 1
            ordered = sorted(by_supplier.items(), key=lambda kv: kv[1]["total"], reverse=True)
            total = float(sum(s["total"] for _, s in ordered))
            lines = [
                f"Tenés {len(recurring)} gastos recurrentes (90 días), ${total:,.0f} en total:"
            ]
            for name, slot in ordered[:8]:
                lines.append(f"- {name}: ${float(slot['total']):,.0f} ({slot['count']} veces)")
            return self._analysis_response(
                request,
                "detectar_gastos_recurrentes",
                "\n".join(lines),
                {
                    "recurring": [
                        {"name": n, "total": float(s["total"]), "count": s["count"]}
                        for n, s in ordered
                    ]
                },
            )

        # ── analizar_costos_fijos_variables ───────────────────────────────────
        if intent == "analizar_costos_fijos_variables":
            from datetime import timedelta  # noqa: PLC0415

            from sqlalchemy import func, select  # noqa: PLC0415

            from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

            cutoff = date.today() - timedelta(days=90)
            rows = await self._db.execute(
                select(
                    ExpenseEntry.is_recurring,
                    func.sum(ExpenseEntry.amount).label("total"),
                )
                .where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.transaction_date >= cutoff,
                )
                .group_by(ExpenseEntry.is_recurring)
            )
            fijos = 0.0
            variables = 0.0
            for r in rows.all():
                if r.is_recurring:
                    fijos = float(r.total or 0)
                else:
                    variables = float(r.total or 0)
            total = fijos + variables
            if total <= 0:
                return self._analysis_response(
                    request,
                    "costos_sin_datos",
                    "No tengo gastos cargados en los últimos 90 días para separar fijos de "
                    "variables.",
                )
            pct_fijos = round(fijos / total * 100, 1)
            lines = [
                "Estructura de costos (últimos 90 días, usando 'recurrente' como proxy de fijo):",
                f"- Fijos: ${fijos:,.0f} ({pct_fijos:.0f}%)",
                f"- Variables: ${variables:,.0f} ({100 - pct_fijos:.0f}%)",
            ]
            return self._analysis_response(
                request,
                "analizar_costos_fijos_variables",
                "\n".join(lines),
                {"fijos": fijos, "variables": variables, "pct_fijos": pct_fijos},
            )

        # ── calcular_punto_equilibrio ─────────────────────────────────────────
        if intent == "calcular_punto_equilibrio":
            from datetime import timedelta  # noqa: PLC0415

            from sqlalchemy import func, select  # noqa: PLC0415

            from app.application.agents.shared import analytics  # noqa: PLC0415
            from app.application.services.deterministic_finance import (  # noqa: PLC0415
                calcular_margen_bruto,
            )
            from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

            cutoff = date.today() - timedelta(days=30)
            fixed_q = await self._db.execute(
                select(func.coalesce(func.sum(ExpenseEntry.amount), 0)).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                    ExpenseEntry.is_recurring.is_(True),
                    ExpenseEntry.transaction_date >= cutoff,
                )
            )
            fixed_costs = float(fixed_q.scalar_one() or 0)
            margen = await calcular_margen_bruto(tenant_id, self._db)
            if margen.get("sin_datos") or not margen.get("margen_pct"):
                return self._analysis_response(
                    request,
                    "equilibrio_sin_margen",
                    "Para calcular tu punto de equilibrio necesito tu margen bruto, y todavía "
                    "no tengo ventas suficientes para calcularlo. Cargá ventas y gastos del mes.",
                )
            margen_pct = float(margen["margen_pct"])
            be = analytics.breakeven_point(fixed_costs, margen_pct)
            if be is None:
                return self._analysis_response(
                    request,
                    "equilibrio_sin_margen",
                    "No puedo calcular el punto de equilibrio sin un margen bruto positivo.",
                )
            lines = [
                f"Tu punto de equilibrio mensual es ${be:,.0f} en ventas.",
                f"Se calcula con tus costos fijos (${fixed_costs:,.0f}/mes) y tu margen "
                f"bruto ({margen_pct:.0f}%). Por encima de ese nivel, empezás a ganar.",
            ]
            return self._analysis_response(
                request,
                "calcular_punto_equilibrio",
                "\n".join(lines),
                {"breakeven": be, "fixed_costs": fixed_costs, "margin_pct": margen_pct},
            )

        # ── clasificar_gastos (default) ───────────────────────────────────────
        from datetime import timedelta  # noqa: PLC0415

        cutoff = date.today() - timedelta(days=90)
        by_cat = await repo.expenses_by_category(tenant_id, from_date=cutoff)
        if not by_cat:
            return self._analysis_response(
                request,
                "gastos_sin_categorias",
                "No tengo gastos cargados en los últimos 90 días para clasificar.",
            )
        total = sum(c["total"] for c in by_cat)
        lines = [f"Tus gastos de los últimos 90 días (${total:,.0f}) por categoría:"]
        for c in by_cat[:8]:
            lines.append(f"- {c['category']}: ${c['total']:,.0f} ({c['pct']:.0f}%)")
        return self._analysis_response(
            request,
            "clasificar_gastos",
            "\n".join(lines),
            {"by_category": by_cat},
        )

    # ── Nivel 2: reclasificación de gastos (asesoría + escritura) ──────────────

    async def _business_vertical(self, tenant_id: uuid.UUID) -> str:
        """Devuelve el vertical_code del tenant o fallback a kiosco_almacen."""
        if self._db is None:
            return "kiosco_almacen"
        from sqlalchemy import select  # noqa: PLC0415

        from app.persistence.models.business import BusinessProfile  # noqa: PLC0415

        result = await self._db.execute(
            select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
        )
        code = result.scalar_one_or_none()
        return code or "kiosco_almacen"

    def _advise_reventa_vs_insumo(
        self, text: str, business_type: str
    ) -> tuple[str, str, bool]:
        """Recomienda reventa (COGS/INVENTORY) vs insumo (OPEX/SUPPLIES) vs otra
        categoría según el VERTICAL, de forma determinística (sin LLM).

        Devuelve ``(category_code, target, es_reventa)``. Reusa el catálogo
        canónico de gastos + el catálogo de productos del vertical vía
        ``classify_expense_with_vertical``: si el texto matchea un producto
        vendible del rubro (golosinas, bebidas, cigarrillos...) → reventa
        (INVENTORY). Si matchea una categoría de gasto operativo (bolsas,
        librería, limpieza...) → insumo/otra categoría (OPEX).
        """
        from app.domain.expense_categories import (  # noqa: PLC0415
            classify_expense_with_vertical,
        )

        code, _label, es_mercaderia = classify_expense_with_vertical(text, business_type)
        if es_mercaderia:
            return "INVENTORY", "reventa", True
        if code in ("SUPPLIES", "MAINTENANCE"):
            return code, "insumo", False
        return code, "categoria", False

    async def _handle_reclassify_expense(
        self, request: AgentRequest, task: Any | None
    ) -> AgentResponse:
        """Asesora o reclasifica un gasto entre reventa / insumo / otra categoría.

        - ASESORÍA (read-only): si el usuario no identificó un gasto concreto ni
          dio un ``target`` explícito, recomienda determinísticamente reventa vs
          insumo según el vertical. status="success", LOW risk.
        - ESCRITURA: si hay identificación (expense_id o fecha+monto+descripción)
          y un ``target``, construye un pending action MEDIUM (requires_approval).
        """
        entities: dict[str, Any] = getattr(task, "entities", {}) or {}
        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            tenant_id = None

        target = entities.get("target")
        expense_id = entities.get("expense_id")
        has_identification = bool(
            expense_id
            or (entities.get("monto") or entities.get("amount"))
            or entities.get("descripcion")
            or entities.get("description")
        )

        business_type = (
            await self._business_vertical(tenant_id)
            if tenant_id is not None
            else "kiosco_almacen"
        )

        # ── ASESORÍA: sin target o sin identificación → recomendamos ──────────
        if not target or not has_identification:
            advice_text = (
                str(entities.get("descripcion") or entities.get("description") or "")
                or request.message
            )
            rec_category, rec_target, es_reventa = self._advise_reventa_vs_insumo(
                advice_text, business_type
            )
            label = EXPENSE_CATEGORY_LABELS_ES.get(rec_category, rec_category)
            if es_reventa:
                msg = (
                    "Para tu rubro, eso es mercadería de reventa: se carga como compra "
                    "de inventario (categoría Mercadería, costo COGS) y conviene vincularlo "
                    "a un producto vendible para seguir su stock y margen. "
                    "Si querés, decime el gasto puntual y lo reclasifico."
                )
            elif rec_target == "insumo":
                msg = (
                    f"Eso suena a insumo de uso interno (no se revende): se carga como "
                    f"gasto operativo, categoría {label} (OPEX). "
                    "Si querés, indicame el gasto y lo reclasifico."
                )
            else:
                msg = (
                    f"Eso no es mercadería de reventa: lo cargaría como gasto operativo "
                    f"en la categoría {label} (OPEX). "
                    "Decime el gasto puntual si querés que lo reclasifique."
                )
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence=Confidence.HIGH,
                message=msg,
                result={
                    "summary": "Asesoría de clasificación de gasto",
                    "structured_data": {
                        "recommended_category": rec_category,
                        "recommended_target": rec_target,
                        "es_reventa": es_reventa,
                    },
                    "analysis": True,
                },
            )

        # ── ESCRITURA: identificación + target → pending action MEDIUM ────────
        normalized_target = str(target).strip().lower()
        if normalized_target in ("reventa", "mercaderia", "mercadería"):
            normalized_target = "reventa"
            final_category = "INVENTORY"
        elif normalized_target in ("insumo", "insumos"):
            normalized_target = "insumo"
            final_category = "SUPPLIES"
        else:
            normalized_target = "categoria"
            raw_cat = entities.get("category")
            final_category, _ = (
                normalize_expense_category(str(raw_cat)) if raw_cat else ("OTHER", None)
            )

        structured: dict[str, Any] = {
            "expense_id": str(expense_id) if expense_id else None,
            "fecha": entities.get("fecha") or entities.get("transaction_date"),
            "monto": str(entities.get("monto") or entities.get("amount") or "")
            or None,
            "descripcion": entities.get("descripcion") or entities.get("description"),
            "target": normalized_target,
            "category": final_category,
            "sku": entities.get("sku"),
            "product_name": entities.get("product_name") or entities.get("nombre"),
        }
        label = EXPENSE_CATEGORY_LABELS_ES.get(final_category, final_category)
        ident = (
            f"el gasto {structured['expense_id']}"
            if structured["expense_id"]
            else (structured["descripcion"] or "el gasto indicado")
        )
        if normalized_target == "reventa":
            summary = (
                f"Reclasificar {ident} como mercadería de reventa "
                f"(Mercadería/COGS) y vincularlo a un producto vendible."
            )
        else:
            summary = f"Reclasificar {ident} como {label} ({normalized_target})."
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": summary,
                "action_type": ActionType.RECLASSIFY_EXPENSE,
                "structured_data": structured,
                "alerts": [],
            },
        )

    async def _handle_cash_outflow(self, request: AgentRequest, task: Any | None) -> AgentResponse:
        """Maneja pagos de deudas / salidas de caja (REGISTER_CASH_OUTFLOW).

        Distinto de REGISTER_EXPENSE (gastos operativos recurrentes).
        Registra una salida de dinero puntual a proveedor, acreedor, etc.
        """
        pre_entities = getattr(task, "entities", {}) or {}
        amount = self._extract_amount(request.message)
        if amount is None and pre_entities.get("amount"):
            try:
                amount = Decimal(str(pre_entities["amount"]))
            except Exception:
                amount = None

        if amount is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question="¿Cuánto fue el pago? Indicá el monto.",
                result={"summary": "Falta el monto para registrar la salida de caja."},
            )
        entities: dict[str, Any] = {
            "amount": str(amount),
            "transaction_date": pre_entities.get("transaction_date", date.today().isoformat()),
            "description": pre_entities.get("description", "Pago / salida de caja"),
            "payment_method": pre_entities.get("payment_method")
            or self._extract_payment_method(request.message),
            "notes": request.message.strip(),
        }
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": f"Registrar pago / salida de caja por ${amount}",
                "action_type": ActionType.REGISTER_CASH_OUTFLOW,
                "structured_data": entities,
                "alerts": [],
            },
        )
