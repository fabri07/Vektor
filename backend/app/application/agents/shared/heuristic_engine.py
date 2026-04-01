from pydantic import BaseModel
from typing import Optional


class CashHealthConfig(BaseModel):
    healthy_days_min: float = 10
    warning_days_min: float = 7
    critical_days_below: float = 5


class MarginConfig(BaseModel):
    net_expected_min: float = 0.12
    net_expected_max: float = 0.18


class InventoryConfig(BaseModel):
    rotation_days_min: int = 7
    rotation_days_max: int = 21
    overstock_tolerance: str = "baja"


class SupplierConfig(BaseModel):
    reorder_frequency: str = "semanal"
    stockout_sensitivity: str = "muy_alta"


class HeuristicConfig(BaseModel):
    business_type: str
    cash_health: CashHealthConfig = CashHealthConfig()
    margin: MarginConfig = MarginConfig()
    inventory: InventoryConfig = InventoryConfig()
    supplier: SupplierConfig = SupplierConfig()
    seasonality: str = "baja"

    def to_prompt_fragment(self) -> str:
        """Retorna string con valores NUMÉRICOS para inyectar en system prompt."""
        return (
            f"Parámetros del rubro {self.business_type}:\n"
            f"- Margen neto esperado: {self.margin.net_expected_min*100:.0f}% a {self.margin.net_expected_max*100:.0f}%\n"
            f"- Rotación de inventario: {self.inventory.rotation_days_min} a {self.inventory.rotation_days_max} días\n"
            f"- Caja saludable: mínimo {self.cash_health.healthy_days_min} días de cobertura\n"
            f"- Alerta de caja: menos de {self.cash_health.warning_days_min} días\n"
            f"- Sensibilidad a quiebre: {self.supplier.stockout_sensitivity}"
        )


class HeuristicEngine:
    @staticmethod
    def get(business_type: str, business_id: Optional[str] = None) -> HeuristicConfig:
        """Stub — implementación completa en FASE 2B."""
        return HeuristicConfig(business_type=business_type)
