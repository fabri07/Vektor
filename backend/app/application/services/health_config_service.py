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


class HealthConfigRequest(BaseModel):
    target_margin_pct: float = Field(..., ge=0.0, le=80.0)
    warning_margin_pct: float = Field(..., ge=0.0, le=80.0)

    @model_validator(mode="after")
    def _target_gte_warning(self) -> HealthConfigRequest:
        if self.target_margin_pct < self.warning_margin_pct:
            raise ValueError("target_margin_pct debe ser >= warning_margin_pct")
        return self


class HealthConfigResponse(BaseModel):
    target_margin_pct: float
    warning_margin_pct: float
    is_custom: bool  # True si el tenant ha personalizado los valores


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


class HealthConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: UUID) -> HealthConfigResponse:
        rows = await self._session.execute(
            select(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([_KEY_TARGET, _KEY_WARNING]),
            )
        )
        overrides = {row.param_key: row.param_value for row in rows.scalars()}
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
            is_custom=bool(overrides),
        )

    async def update(self, tenant_id: UUID, config: HealthConfigRequest) -> HealthConfigResponse:
        # Eliminar overrides existentes para este tenant (upsert manual)
        await self._session.execute(
            delete(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([_KEY_TARGET, _KEY_WARNING]),
            )
        )
        self._session.add(
            BusinessHeuristicOverride(
                tenant_id=tenant_id,
                param_key=_KEY_TARGET,
                param_value={"value": config.target_margin_pct},
            )
        )
        self._session.add(
            BusinessHeuristicOverride(
                tenant_id=tenant_id,
                param_key=_KEY_WARNING,
                param_value={"value": config.warning_margin_pct},
            )
        )
        await self._session.commit()
        logger.info(
            "health_config.updated",
            tenant_id=str(tenant_id),
            target_margin_pct=config.target_margin_pct,
            warning_margin_pct=config.warning_margin_pct,
        )
        return HealthConfigResponse(
            target_margin_pct=config.target_margin_pct,
            warning_margin_pct=config.warning_margin_pct,
            is_custom=True,
        )

    async def reset(self, tenant_id: UUID) -> HealthConfigResponse:
        """Elimina los overrides y vuelve a los valores por vertical."""
        await self._session.execute(
            delete(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([_KEY_TARGET, _KEY_WARNING]),
            )
        )
        await self._session.commit()
        return HealthConfigResponse(
            target_margin_pct=_DEFAULT_TARGET,
            warning_margin_pct=_DEFAULT_WARNING,
            is_custom=False,
        )
