"""HeuristicEngine — parámetros numéricos por rubro para los agentes.

REGLA CRÍTICA: los agentes inyectan heurística como valores NUMÉRICOS en el system prompt.
NUNCA como texto narrativo.
  BIEN:  "Margen neto esperado: 12% a 18%"
  MAL:   "El margen de este rubro es bueno si está en un rango saludable"
"""

import json
import uuid
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import Vertical
from app.persistence.models.heuristic_override import BusinessHeuristicOverride

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "heuristics"

# Los sub-configs NO tienen defaults a propósito: los valores que había acá eran
# los de kiosco, así que a un JSON con una clave faltante se le inyectaban los
# umbrales de kiosco en silencio. Cada JSON de rubro trae las 16 claves-hoja; si
# alguna falta, pydantic levanta y el bug de datos se ve.
#
# `margin.net_expected_{min,max}` DUPLICA a `margin.healthy_{min,max}`, que es lo
# que usa el health engine: son la misma decisión escrita dos veces, y nada en el
# runtime las compara. Las mantiene sincronizadas un test
# (`test_benchmark_provenance.py`), no la estructura.


class CashHealthConfig(BaseModel):
    healthy_days_min: float
    warning_days_min: float
    critical_days_below: float


class MarginConfig(BaseModel):
    net_expected_min: float
    net_expected_max: float


class InventoryConfig(BaseModel):
    rotation_days_min: int
    rotation_days_max: int
    overstock_tolerance: str


class SupplierConfig(BaseModel):
    reorder_frequency: str
    stockout_sensitivity: str


class HeuristicConfig(BaseModel):
    business_type: str
    cash_health: CashHealthConfig
    margin: MarginConfig
    inventory: InventoryConfig
    supplier: SupplierConfig
    seasonality: str

    def to_prompt_fragment(self) -> str:
        """
        Retorna string con valores NUMÉRICOS para inyectar en system prompts.
        NUNCA texto narrativo — siempre números concretos.
        """
        return (
            f"Parámetros del negocio ({self.business_type}):\n"
            f"- Margen neto esperado: {self.margin.net_expected_min * 100:.0f}%"
            f" a {self.margin.net_expected_max * 100:.0f}%\n"
            f"- Rotación de inventario: {self.inventory.rotation_days_min}"
            f" a {self.inventory.rotation_days_max} días\n"
            f"- Caja OK: mínimo {self.cash_health.healthy_days_min} días de cobertura\n"
            f"- Alerta de caja: menos de {self.cash_health.warning_days_min} días\n"
            f"- Crítico de caja: menos de {self.cash_health.critical_days_below} días\n"
            f"- Sensibilidad a quiebre de stock: {self.supplier.stockout_sensitivity}\n"
            f"- Frecuencia de reposición: {self.supplier.reorder_frequency}\n"
            f"- Estacionalidad: {self.seasonality}"
        )

    def is_margin_healthy(self, actual_margin: float) -> bool:
        return self.margin.net_expected_min <= actual_margin <= self.margin.net_expected_max

    def is_cash_critical(self, coverage_days: float) -> bool:
        return coverage_days < self.cash_health.critical_days_below

    def is_cash_warning(self, coverage_days: float) -> bool:
        return coverage_days < self.cash_health.warning_days_min

    def is_stockout_risk(self, current_stock: int, min_threshold: int) -> bool:
        return current_stock <= min_threshold

    def is_overstock(self, actual_rotation_days: float) -> bool:
        return actual_rotation_days > (self.inventory.rotation_days_max * 2)


class HeuristicEngine:
    @staticmethod
    def _load_default(vertical: Vertical) -> dict[str, Any]:
        path = DATA_DIR / f"{vertical.value}.json"
        if not path.exists():
            raise FileNotFoundError(f"Archivo de heurística no encontrado: {path}")
        return cast("dict[str, Any]", json.loads(path.read_text()))

    @staticmethod
    def get(vertical: Vertical, business_id: str | None = None) -> HeuristicConfig:
        """
        Versión síncrona — usa solo los defaults del rubro.
        Para aplicar overrides por negocio usar get_async().
        """
        defaults = HeuristicEngine._load_default(vertical)
        return HeuristicConfig(**defaults)

    @staticmethod
    async def get_async(
        vertical: Vertical,
        business_id: str,
        db: AsyncSession,
    ) -> HeuristicConfig:
        """
        Versión asíncrona — aplica overrides del negocio sobre los defaults.
        El parámetro business_id corresponde al tenant_id del negocio.
        """
        config_dict = HeuristicEngine._load_default(vertical)

        # Buscar overrides del negocio en la BD (tenant_id = business_id)
        stmt = select(BusinessHeuristicOverride).where(
            BusinessHeuristicOverride.tenant_id == uuid.UUID(business_id)
        )
        result = await db.execute(stmt)
        overrides = result.scalars().all()

        # Aplicar overrides: param_key puede ser "margin.net_expected_min" etc.
        for override in overrides:
            keys = override.param_key.split(".")
            target = config_dict
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = override.param_value

        return HeuristicConfig(**config_dict)
