"""Servicio de configuración del objetivo de margen BRUTO por tenant.

Permite que cada tenant (dueño o quien tenga permiso de modificación) ajuste:
  - target_margin_pct: objetivo de margen bruto por producto (ej. 35.0 = 35%)
  - warning_margin_pct: umbral de alerta de margen bruto (ej. 20.0 = 20%)

Los valores se persisten en business_heuristic_overrides.

D-4: es margen BRUTO (precio contra costo), que es lo que la pantalla promete y
lo que el dueño declaró al guardarlo. Lo consumen los análisis por producto del
chat (precios sugeridos, productos sensibles, «estrellas vs. problema»). El
health score NO lo usa: su dimensión de margen mide el margen NETO (M·3) contra
el benchmark NETO del rubro, el mismo que usan FactsService y el texto de
MARGIN_LOW. Antes `get_margin_benchmark` convertía estos porcentajes brutos en
la vara del neto: un kiosco con neto 12% (sano) y 35/20 guardado puntuaba
MARGIN_LOW. Un número bruto no se compara con un ratio neto.

Validaciones:
  - Ambos valores deben estar en [0.0, 80.0].
  - target_margin_pct >= warning_margin_pct.

Cada cambio (guardar o restablecer) queda en decision_audit_log con el antes y
el después.

D-3/R-10 (decisión del dueño 2026-10-28): también guarda a cuántos días corridos
de la venta la proyección de caja cuenta lo vendido con tarjeta de crédito
(`credit_card_settlement_days`, 0–60, por defecto 14 ≈ 10 días hábiles, el plazo
legal de liquidación). Lo lee `forecast_service` vía
`get_credit_card_settlement_days(tenant_id, session)`. El PATCH es parcial: N
solo no toca los márgenes, y los márgenes van juntos.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.heuristic_override import BusinessHeuristicOverride

logger = get_logger(__name__)

_KEY_TARGET = "target_margin_pct"
_KEY_WARNING = "warning_margin_pct"
# Predeterminados GENÉRICOS de Véktor para el margen bruto, iguales para todos
# los rubros (los JSON del rubro sólo traen el benchmark neto).
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
    credit_card_settlement_days: int = DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS
    credit_card_settlement_days_is_custom: bool = False
    # D-4: qué margen es. Siempre bruto; el score mide el neto contra el rubro.
    margin_kind: Literal["bruto"] = "bruto"


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

    async def _overrides(self, tenant_id: UUID) -> dict[str, Any]:
        rows = await self._session.execute(
            select(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([*_MARGIN_KEYS, _KEY_CC_DAYS]),
            )
        )
        return {row.param_key: row.param_value for row in rows.scalars()}

    async def get(self, tenant_id: UUID) -> HealthConfigResponse:
        overrides = await self._overrides(tenant_id)
        return _respuesta(overrides)

    def _auditar(
        self,
        tenant_id: UUID,
        user_id: UUID,
        decision_type: str,
        antes: HealthConfigResponse,
        despues: HealthConfigResponse,
    ) -> None:
        self._session.add(
            DecisionAuditLog(
                tenant_id=tenant_id,
                decision_type=decision_type,
                decision_data={
                    "before": antes.model_dump(),
                    "after": despues.model_dump(),
                    "source": "ui",
                },
                triggered_by="ui:settings",
                actor_user_id=user_id,
                context={"endpoint": "settings/health-config"},
                created_at=datetime.now(UTC),
            )
        )

    async def update(
        self, tenant_id: UUID, user_id: UUID, config: HealthConfigRequest
    ) -> HealthConfigResponse:
        antes = _respuesta(await self._overrides(tenant_id))
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
        await self._session.flush()
        despues = _respuesta(await self._overrides(tenant_id))
        decision_type = (
            "MARGIN_CONFIG_UPDATED"
            if _KEY_TARGET in nuevos
            else "CREDIT_CARD_SETTLEMENT_DAYS_UPDATED"
        )
        self._auditar(tenant_id, user_id, decision_type, antes, despues)
        await self._session.commit()
        logger.info(
            "health_config.updated",
            tenant_id=str(tenant_id),
            target_margin_pct=config.target_margin_pct,
            warning_margin_pct=config.warning_margin_pct,
            credit_card_settlement_days=config.credit_card_settlement_days,
        )
        return despues

    async def reset(self, tenant_id: UUID, user_id: UUID) -> HealthConfigResponse:
        """Elimina los overrides y vuelve a los predeterminados de Véktor."""
        antes = _respuesta(await self._overrides(tenant_id))
        await self._session.execute(
            delete(BusinessHeuristicOverride).where(
                BusinessHeuristicOverride.tenant_id == tenant_id,
                BusinessHeuristicOverride.param_key.in_([*_MARGIN_KEYS, _KEY_CC_DAYS]),
            )
        )
        despues = HealthConfigResponse(
            target_margin_pct=_DEFAULT_TARGET,
            warning_margin_pct=_DEFAULT_WARNING,
            is_custom=False,
            credit_card_settlement_days=DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS,
            credit_card_settlement_days_is_custom=False,
        )
        self._auditar(tenant_id, user_id, "MARGIN_CONFIG_RESET", antes, despues)
        await self._session.commit()
        return despues


def _respuesta(overrides: dict[str, Any]) -> HealthConfigResponse:
    target = float(overrides[_KEY_TARGET]["value"]) if _KEY_TARGET in overrides else _DEFAULT_TARGET
    warning = (
        float(overrides[_KEY_WARNING]["value"]) if _KEY_WARNING in overrides else _DEFAULT_WARNING
    )
    cc_days = (
        int(overrides[_KEY_CC_DAYS]["value"])
        if _KEY_CC_DAYS in overrides
        else DEFAULT_CREDIT_CARD_SETTLEMENT_DAYS
    )
    return HealthConfigResponse(
        target_margin_pct=target,
        warning_margin_pct=warning,
        is_custom=_KEY_TARGET in overrides or _KEY_WARNING in overrides,
        credit_card_settlement_days=cc_days,
        credit_card_settlement_days_is_custom=_KEY_CC_DAYS in overrides,
    )
