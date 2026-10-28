"""Servicio de configuración de Health Score por tenant.

Permite que cada tenant (OWNER/ADMIN) ajuste:
  - target_margin_pct: objetivo de margen bruto (ej. 35.0 = 35%)
  - warning_margin_pct: umbral de alerta de margen (ej. 20.0 = 20%)

Los valores se persisten en business_heuristic_overrides.
HealthScoreService (Celery recalculation) y AgentHealth (chat) consumen el
override vía `get_margin_benchmark(tenant_id, session)` que arma un MarginBenchmark
con la escala decimal del motor. Si no hay override, ambos paths caen al
benchmark del vertical (load_vertical_heuristics).

Validaciones:
  - Ambos valores deben estar en [0.0, 80.0].
  - target_margin_pct >= warning_margin_pct.

D-3/R-10 (decisión del dueño 2026-10-28): también guarda a cuántos días corridos
de la venta la proyección de caja cuenta lo vendido con tarjeta de crédito
(`credit_card_settlement_days`, 0–60, por defecto 14 ≈ 10 días hábiles, el plazo
legal de liquidación). Lo lee `forecast_service` vía
`get_credit_card_settlement_days(tenant_id, session)`.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.heuristics.verticals import BenchmarkProvenance, MarginBenchmark
from app.observability.logger import get_logger
from app.persistence.models.heuristic_override import BusinessHeuristicOverride

logger = get_logger(__name__)

_KEY_TARGET = "target_margin_pct"
_KEY_WARNING = "warning_margin_pct"
_DEFAULT_TARGET = 35.0
_DEFAULT_WARNING = 20.0
_MARGIN_KEYS = [_KEY_TARGET, _KEY_WARNING]
_KEY_CC_DAYS = "credit_card_settlement_days"
#: Días corridos ≈ 10 hábiles: el plazo legal de liquidación de la tarjeta de crédito.
DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS = 14


class HealthConfigRequest(BaseModel):
    """PATCH parcial: se aplica sólo lo que viene. Los márgenes van juntos."""

    target_margin_pct: float | None = Field(None, ge=0.0, le=80.0)
    warning_margin_pct: float | None = Field(None, ge=0.0, le=80.0)
    # 0 = el día de la venta.
    credit_card_settlement_days: int | None = Field(None, ge=0, le=60)

    @model_validator(mode="after")
    def _target_gte_warning(self) -> HealthConfigRequest:
        if (self.target_margin_pct is None) != (self.warning_margin_pct is None):
            raise ValueError("target_margin_pct y warning_margin_pct van juntos")
        if self.target_margin_pct is None and self.credit_card_settlement_days is None:
            raise ValueError("No hay nada para actualizar")
        if (
            self.target_margin_pct is not None
            and self.warning_margin_pct is not None
            and self.target_margin_pct < self.warning_margin_pct
        ):
            raise ValueError("target_margin_pct debe ser >= warning_margin_pct")
        return self


class HealthConfigResponse(BaseModel):
    target_margin_pct: float
    warning_margin_pct: float
    is_custom: bool  # True si el tenant ha personalizado los márgenes
    credit_card_settlement_days: int
    credit_card_settlement_days_is_custom: bool


def _benchmark_from_pcts(target_pct: float, warning_pct: float) -> MarginBenchmark:
    """Convierte porcentajes de tenant (ej. 35.0) a la escala decimal del motor (0.35).

    Procedencia `TENANT_OVERRIDE`: el número lo puso el dueño del negocio, así
    que es el objetivo que él declara y no una estimación nuestra. Por eso aporta
    confianza alta aunque no tenga respaldo sectorial.
    """
    warning = warning_pct / 100.0
    target = target_pct / 100.0
    return MarginBenchmark(
        critical_below=warning * 0.5,  # la mitad del umbral de warning
        warning_below=warning,
        healthy_min=warning,  # diseño canónico: warning_below == healthy_min
        healthy_max=target,
        provenance=BenchmarkProvenance.TENANT_OVERRIDE,
    )


async def get_margin_benchmark(
    tenant_id: UUID,
    session: AsyncSession,
) -> MarginBenchmark | None:
    """Devuelve MarginBenchmark con los overrides del tenant, o None si no hay overrides."""
    rows = await session.execute(
        select(BusinessHeuristicOverride).where(
            BusinessHeuristicOverride.tenant_id == tenant_id,
            BusinessHeuristicOverride.param_key.in_([_KEY_TARGET, _KEY_WARNING]),
        )
    )
    overrides = {row.param_key: row.param_value for row in rows.scalars()}
    if _KEY_TARGET not in overrides and _KEY_WARNING not in overrides:
        return None
    target = float(overrides[_KEY_TARGET]["value"]) if _KEY_TARGET in overrides else _DEFAULT_TARGET
    warning = (
        float(overrides[_KEY_WARNING]["value"]) if _KEY_WARNING in overrides else _DEFAULT_WARNING
    )
    return _benchmark_from_pcts(target, warning)


async def get_credit_card_settlement_days(tenant_id: UUID, session: AsyncSession) -> int:
    """A cuántos días corridos de la venta entra a caja lo vendido con crédito."""
    row = (
        await session.execute(
            select(BusinessHeuristicOverride.param_value).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key == _KEY_CC_DAYS,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS
    return int(row["value"])


class HealthConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: UUID) -> HealthConfigResponse:
        rows = await self._session.execute(
            select(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([*_MARGIN_KEYS, _KEY_CC_DAYS]),
            )
        )
        overrides = {row.param_key: row.param_value for row in rows.scalars()}
        cc_days = (
            int(overrides[_KEY_CC_DAYS]["value"])
            if _KEY_CC_DAYS in overrides
            else DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS
        )
        target = (
            float(overrides[_KEY_TARGET]["value"]) if _KEY_TARGET in overrides else _DEFAULT_TARGET
        )
        warning = (
            float(overrides[_KEY_WARNING]["value"])
            if _KEY_WARNING in overrides
            else _DEFAULT_WARNING
        )
        return HealthConfigResponse(
            target_margin_pct=target,
            warning_margin_pct=warning,
            is_custom=_KEY_TARGET in overrides or _KEY_WARNING in overrides,
            credit_card_settlement_days=cc_days,
            credit_card_settlement_days_is_custom=_KEY_CC_DAYS in overrides,
        )

    async def update(self, tenant_id: UUID, config: HealthConfigRequest) -> HealthConfigResponse:
        # Upsert manual de lo que viene: borrar y volver a escribir esas claves.
        nuevos: dict[str, float | int] = {}
        if config.target_margin_pct is not None and config.warning_margin_pct is not None:
            nuevos[_KEY_TARGET] = config.target_margin_pct
            nuevos[_KEY_WARNING] = config.warning_margin_pct
        if config.credit_card_settlement_days is not None:
            nuevos[_KEY_CC_DAYS] = config.credit_card_settlement_days
        await self._session.execute(
            delete(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_(list(nuevos)),
            )
        )
        for key, value in nuevos.items():
            self._session.add(
                BusinessHeuristicOverride(
                    tenant_id=tenant_id, param_key=key, param_value={"value": value}
                )
            )
        await self._session.commit()
        logger.info(
            "health_config.updated",
            tenant_id=str(tenant_id),
            target_margin_pct=config.target_margin_pct,
            warning_margin_pct=config.warning_margin_pct,
            credit_card_settlement_days=config.credit_card_settlement_days,
        )
        return await self.get(tenant_id)

    async def reset(self, tenant_id: UUID) -> HealthConfigResponse:
        """Elimina los overrides y vuelve a los valores por vertical."""
        await self._session.execute(
            delete(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([*_MARGIN_KEYS, _KEY_CC_DAYS]),
            )
        )
        await self._session.commit()
        return HealthConfigResponse(
            target_margin_pct=_DEFAULT_TARGET,
            warning_margin_pct=_DEFAULT_WARNING,
            is_custom=False,
            credit_card_settlement_days=DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS,
            credit_card_settlement_days_is_custom=False,
        )
