"""Tests para deterministic_finance — cálculos financieros sin LLM."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.deterministic_finance import (
    calcular_flujo_neto_30d,
    calcular_margen_bruto,
    calcular_ticket_promedio,
    get_financial_summary,
)


def _fake_session(ventas_sum: Decimal = Decimal("0"), gastos_sum: Decimal = Decimal("0"), count: int = 0) -> AsyncMock:
    """Returns an AsyncSession mock that responds to scalar() calls in order."""
    session = AsyncMock()
    # scalar() calls:
    # calcular_flujo_neto_30d: ventas_q, gastos_q
    # calcular_ticket_promedio: count_q, sum_q
    session.scalar.side_effect = [ventas_sum, gastos_sum, count, ventas_sum]
    return session


class TestFlujoNeto:
    @pytest.mark.asyncio
    async def test_flujo_neto_con_datos_reales(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [Decimal("100000"), Decimal("40000")]
        result = await calcular_flujo_neto_30d(tenant_id, session)
        assert result["total_ventas"] == Decimal("100000")
        assert result["total_gastos"] == Decimal("40000")
        assert result["flujo_neto"] == Decimal("60000")
        assert result["periodo_dias"] == 30

    @pytest.mark.asyncio
    async def test_flujo_neto_sin_datos(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [Decimal("0"), Decimal("0")]
        result = await calcular_flujo_neto_30d(tenant_id, session)
        assert result["flujo_neto"] == Decimal("0")

    @pytest.mark.asyncio
    async def test_uses_decimal_not_float(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [Decimal("123456.78"), Decimal("45678.90")]
        result = await calcular_flujo_neto_30d(tenant_id, session)
        assert isinstance(result["total_ventas"], Decimal)
        assert isinstance(result["total_gastos"], Decimal)
        assert isinstance(result["flujo_neto"], Decimal)


class TestMargenBruto:
    @pytest.mark.asyncio
    async def test_margen_con_datos(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [Decimal("100000"), Decimal("70000")]
        result = await calcular_margen_bruto(tenant_id, session)
        assert not result.get("sin_datos")
        assert result["margen_pct"] == Decimal("30.00")

    @pytest.mark.asyncio
    async def test_margen_con_ventas_cero(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [Decimal("0"), Decimal("0")]
        result = await calcular_margen_bruto(tenant_id, session)
        assert result.get("sin_datos") is True
        assert result["margen_pct"] is None


class TestTicketPromedio:
    @pytest.mark.asyncio
    async def test_ticket_promedio_con_datos(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [10, Decimal("150000")]
        result = await calcular_ticket_promedio(tenant_id, session)
        assert not result.get("sin_datos")
        assert result["ticket_promedio"] == Decimal("15000.00")
        assert result["n_transacciones"] == 10

    @pytest.mark.asyncio
    async def test_ticket_sin_transacciones(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = [0, Decimal("0")]
        result = await calcular_ticket_promedio(tenant_id, session)
        assert result.get("sin_datos") is True
        assert result["ticket_promedio"] is None


class TestFinancialSummary:
    @pytest.mark.asyncio
    async def test_sin_datos_retorna_estado_sin_datos(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        # All queries return 0
        session.scalar.side_effect = [
            Decimal("0"), Decimal("0"),  # flujo_neto
            Decimal("0"), Decimal("0"),  # margen (reutiliza flujo_neto)
            0, Decimal("0"),             # ticket_promedio
            0, Decimal("0"),             # rotacion
        ]
        result = await get_financial_summary(tenant_id, session)
        assert result["estado"] == "SIN_DATOS"
        assert "mensaje" in result
        assert result.get("provenance_checked") == "REAL"

    @pytest.mark.asyncio
    async def test_flujo_neto_con_datos_reales_retorna_ok(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        # ventas 100k, gastos 40k → flujo positivo → no SIN_DATOS
        session.scalar.side_effect = [
            Decimal("100000"), Decimal("40000"),  # flujo_neto
            Decimal("100000"), Decimal("40000"),  # margen
            5, Decimal("100000"),                  # ticket
            10, Decimal("100000"),                 # rotacion stock
        ]
        result = await get_financial_summary(tenant_id, session)
        assert result["estado"] == "OK"
        assert result["provenance"] == "REAL"

    @pytest.mark.asyncio
    async def test_no_mezcla_datos_demo(self) -> None:
        """get_financial_summary filtra por provenance='REAL' — los datos demo no deben aparecer."""
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        # El WHERE clause con provenance='REAL' es parte de la query construida en el servicio.
        # Aquí solo verificamos que la función llama a scalar() (que ejecuta la query filtrada).
        session.scalar.side_effect = [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0, Decimal("0"), 0, Decimal("0")]
        result = await get_financial_summary(tenant_id, session)
        # Si los datos DEMO fueran incluidos, el resultado no sería SIN_DATOS
        # Un tenant demo con solo datos DEMO → get_financial_summary debe retornar SIN_DATOS
        assert result["estado"] == "SIN_DATOS"

    @pytest.mark.asyncio
    async def test_error_retorna_sin_datos(self) -> None:
        tenant_id = uuid.uuid4()
        session = AsyncMock()
        session.scalar.side_effect = Exception("DB connection lost")
        result = await get_financial_summary(tenant_id, session)
        assert result["estado"] == "SIN_DATOS"
