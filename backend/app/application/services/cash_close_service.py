"""Servicio de cierre de caja diario (arqueo). Sprint 20.

El "esperado" se calcula server-side desde las ventas del día por método de pago,
con NETEO de efectivo (efectivo esperado = ventas efectivo − gastos pagados en
efectivo). El método `inflow` (cobros de cuenta corriente) se mantiene como
categoría propia, no se mezcla con efectivo.

El usuario nunca envía el esperado: lo recomputamos siempre al crear el cierre.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.work_schedule_service import resolve_schedule
from app.domain.business_time import AR_TZ
from app.domain.fiscal_condition import (
    FiscalCondition,
    normalize_fiscal_condition,
)
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.cash_close import CashClose
from app.persistence.repositories.cash_close_repository import CashCloseRepository
from app.persistence.repositories.transaction_repository import (
    ExpenseRepository,
    SaleRepository,
)
from app.schemas.cash_close import (
    CashClosePreviewResponse,
    CashCloseResponse,
    CashMethodBreakdown,
    CreateCashCloseRequest,
)

logger = get_logger(__name__)

_ART = AR_TZ
_CASH_METHOD = "cash"


class CashCloseAlreadyExistsError(Exception):
    """Ya existe un cierre para ese (tenant, fecha)."""


class CashCloseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = CashCloseRepository(session)
        self._sales = SaleRepository(session)
        self._expenses = ExpenseRepository(session)

    async def _compute_expected(
        self, tenant_id: uuid.UUID, close_date: date
    ) -> tuple[float, dict[str, float]]:
        """Esperado por método del día, con neteo de efectivo. Devuelve (total, by_method)."""
        sales_rows = await self._sales.cash_breakdown_by_method(tenant_id, close_date, close_date)
        by_method: dict[str, float] = {}
        for row in sales_rows:
            method = row["payment_method"]
            by_method[method] = by_method.get(method, 0.0) + float(row["total"])

        # Neteo de efectivo: restar gastos pagados en efectivo del esperado de caja.
        expense_rows = await self._expenses.cash_breakdown_by_method(
            tenant_id, close_date, close_date
        )
        cash_expenses = sum(
            float(r["total"]) for r in expense_rows if r["payment_method"] == _CASH_METHOD
        )
        if cash_expenses:
            by_method[_CASH_METHOD] = by_method.get(_CASH_METHOD, 0.0) - cash_expenses

        total = round(sum(by_method.values()), 2)
        return total, {m: round(v, 2) for m, v in by_method.items()}

    async def _is_past_close_now(self, tenant_id: uuid.UUID) -> bool:
        """True si hoy es día laboral y la hora ART ya superó el cierre configurado."""
        bp = (
            await self._session.execute(
                select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        work_days, _open_hour, close_hour = resolve_schedule(bp)
        now = datetime.now(_ART)
        # Python weekday(): lunes=0 … domingo=6 (misma convención que work_days)
        if now.weekday() not in work_days:
            return False
        return now.hour >= close_hour

    async def _suggested_opening_float(self, tenant_id: uuid.UUID) -> float | None:
        """Fondo del último cierre con arqueo — el fondo es fijo día a día."""
        row = (
            await self._session.execute(
                select(CashClose.opening_float_ars)
                .where(
                    CashClose.tenant_id == tenant_id,
                    CashClose.opening_float_ars.is_not(None),
                )
                .order_by(CashClose.close_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return float(row) if row is not None else None

    async def _fiscal_condition(self, tenant_id: uuid.UUID) -> FiscalCondition | None:
        """Condición fiscal canónica del tenant.

        Normaliza el valor crudo (incluido el legacy 'registered') con el mismo
        normalizador que usa ``GET /settings/fiscal-condition``, para que el modal
        de arqueo reciba siempre un código canónico y el frontend lo clasifique
        igual en todos lados.
        """
        raw = (
            await self._session.execute(
                select(BusinessProfile.fiscal_condition).where(
                    BusinessProfile.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        return normalize_fiscal_condition(raw)

    async def preview(
        self, tenant_id: uuid.UUID, close_date: date
    ) -> CashClosePreviewResponse:
        expected_total, by_method = await self._compute_expected(tenant_id, close_date)
        already = await self._repo.get_by_date(tenant_id, close_date)
        is_past_close = await self._is_past_close_now(tenant_id)
        return CashClosePreviewResponse(
            close_date=close_date,
            expected_total_ars=expected_total,
            breakdown=[
                CashMethodBreakdown(payment_method=m, expected_ars=v)
                for m, v in sorted(by_method.items())
            ],
            already_closed=already is not None,
            is_past_close_now=is_past_close and already is None,
            suggested_opening_float_ars=await self._suggested_opening_float(tenant_id),
            fiscal_condition=await self._fiscal_condition(tenant_id),
        )

    async def create(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        body: CreateCashCloseRequest,
    ) -> CashCloseResponse:
        existing = await self._repo.get_by_date(tenant_id, body.close_date)
        if existing is not None:
            raise CashCloseAlreadyExistsError()

        # Recomputar esperado server-side — nunca confiar en el cliente.
        expected_total, expected_by_method = await self._compute_expected(
            tenant_id, body.close_date
        )

        counted_by_method = {
            m: float(v) for m, v in (body.counted_by_method or {}).items()
        }
        # Arqueo estructurado: el contado de efectivo se computa server-side.
        #   efectivo neto = físico (por denominación) − fondo inicial + vales
        # El fondo no es ganancia del día; los vales justifican salidas de caja
        # con comprobante casero que NO están registradas como gasto (las
        # registradas ya se netean en el esperado).
        is_arqueo = (
            body.opening_float_ars is not None
            or body.cash_denominations is not None
            or body.voucher_expenses_ars is not None
        )
        if is_arqueo:
            if body.cash_denominations is not None:
                physical_cash = sum(
                    float(denom) * qty for denom, qty in body.cash_denominations.items()
                )
            else:
                physical_cash = counted_by_method.get(_CASH_METHOD, 0.0)
            opening_float = float(body.opening_float_ars or 0)
            vouchers = float(body.voucher_expenses_ars or 0)
            counted_by_method[_CASH_METHOD] = round(
                physical_cash - opening_float + vouchers, 2
            )
            counted_total = round(sum(counted_by_method.values()), 2)
        else:
            counted_total = float(body.counted_total_ars)

        difference = round(counted_total - expected_total, 2)
        result_code = (
            "BALANCED" if difference == 0 else "SURPLUS" if difference > 0 else "SHORTAGE"
        )

        breakdown: dict[str, dict[str, float | None]] = {}
        all_methods = set(expected_by_method) | set(counted_by_method)
        for method in all_methods:
            counted_val = counted_by_method.get(method)
            breakdown[method] = {
                "expected": expected_by_method.get(method, 0.0),
                "counted": float(counted_val) if counted_val is not None else None,
            }

        entry = CashClose(
            tenant_id=tenant_id,
            close_date=body.close_date,
            expected_total_ars=Decimal(str(expected_total)),
            counted_total_ars=Decimal(str(counted_total)),
            difference_ars=Decimal(str(difference)),
            breakdown_by_method=breakdown,
            notes=body.notes,
            closed_by_user_id=user_id,
            opening_float_ars=(
                Decimal(str(float(body.opening_float_ars)))
                if body.opening_float_ars is not None
                else None
            ),
            cash_denominations=body.cash_denominations,
            voucher_expenses_ars=(
                Decimal(str(float(body.voucher_expenses_ars)))
                if body.voucher_expenses_ars is not None
                else None
            ),
            result_code=result_code,
        )
        await self._repo.save(entry)

        # Sobrante → otros ingresos (inflow): trazable y no contamina el
        # esperado de efectivo de mañana.
        surplus_registered = False
        if body.register_surplus_as_income and difference > 0:
            from app.application.services.cash_service import (  # noqa: PLC0415
                save_cash_inflow,
            )

            await save_cash_inflow(
                {
                    "amount": difference,
                    "notes": f"Sobrante de caja (arqueo {body.close_date})",
                },
                tenant_id,
                self._session,
            )
            surplus_registered = True

        self._session.add(
            DecisionAuditLog(
                tenant_id=tenant_id,
                decision_type="CASH_CLOSE_CREATED",
                decision_data={
                    "close_date": str(body.close_date),
                    "expected_total_ars": expected_total,
                    "counted_total_ars": counted_total,
                    "difference_ars": difference,
                    "breakdown_by_method": breakdown,
                    "result_code": result_code,
                    "opening_float_ars": (
                        float(body.opening_float_ars)
                        if body.opening_float_ars is not None
                        else None
                    ),
                    "voucher_expenses_ars": (
                        float(body.voucher_expenses_ars)
                        if body.voucher_expenses_ars is not None
                        else None
                    ),
                    "surplus_registered": surplus_registered,
                },
                triggered_by="ui:cash_close",
                actor_user_id=user_id,
                context={"endpoint": "cash-closes"},
                created_at=datetime.now(UTC),
            )
        )
        await self._session.commit()
        await self._session.refresh(entry)

        # Recalc async por consistencia de caja (cash close es independiente del
        # health score, pero dispara recalc para refrescar el estado).
        try:
            from app.application.services.score_trigger_service import (  # noqa: PLC0415
                trigger_score_recalculation,
            )

            trigger_score_recalculation.delay(str(tenant_id), "cash_close_created")
        except Exception as exc:  # fail-silent: el cierre ya se guardó
            logger.warning("cash_close.recalc_enqueue_failed", error=str(exc))

        logger.info(
            "cash_close.created",
            tenant_id=str(tenant_id),
            close_date=str(body.close_date),
            difference=difference,
        )
        return self._to_response(entry, surplus_registered=surplus_registered)

    @staticmethod
    def _to_response(
        entry: CashClose, *, surplus_registered: bool = False
    ) -> CashCloseResponse:
        return CashCloseResponse(
            id=entry.id,
            close_date=entry.close_date,
            expected_total_ars=float(entry.expected_total_ars),
            counted_total_ars=float(entry.counted_total_ars),
            difference_ars=float(entry.difference_ars),
            breakdown_by_method=entry.breakdown_by_method,
            notes=entry.notes,
            closed_by_user_id=entry.closed_by_user_id,
            created_at=entry.created_at,
            opening_float_ars=(
                float(entry.opening_float_ars)
                if entry.opening_float_ars is not None
                else None
            ),
            cash_denominations=entry.cash_denominations,
            voucher_expenses_ars=(
                float(entry.voucher_expenses_ars)
                if entry.voucher_expenses_ars is not None
                else None
            ),
            result_code=entry.result_code,
            surplus_registered=surplus_registered,
        )

    async def list_closes(
        self, tenant_id: uuid.UUID, from_date: date, to_date: date
    ) -> list[CashCloseResponse]:
        rows = await self._repo.list_by_range(tenant_id, from_date, to_date)
        return [self._to_response(r) for r in rows]
