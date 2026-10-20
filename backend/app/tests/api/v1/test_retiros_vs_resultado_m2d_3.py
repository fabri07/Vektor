"""M·2d-3: la señal «los retiros superan el resultado» del mes cerrado.

Informativa y separada del arqueo: sale en ``GET /insights/margins`` detrás de la
compuerta de completitud, nunca en el score. Cuentan los retiros (en dinero y en
especie) del mes cerrado; las devoluciones de préstamo no.

El escenario de M·3-2 deja un neto de 25% con resultado 5.000 en el mes cerrado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_service import (
    BusinessFact,
    CapitalDeSocio,
    CapitalDeSocios,
    MontoPorForma,
    Period,
    Provenance,
    retiros_vs_resultado,
)
from app.domain.business_time import today_ar
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.score import HealthScoreSnapshot
from app.tests.api.v1.test_margen_pantalla_m32 import _escenario

pytestmark = pytest.mark.usefixtures("score_suficiente")

URL = "/api/v1/insights/margins"


# ── Función pura ──────────────────────────────────────────────────────────────


def _neto(resultado: float | None, estado: str = "confirmado") -> BusinessFact:
    return BusinessFact(
        fact_id="margen_neto_mes_2026_09", domain="rentabilidad", metric="margen_neto",
        value=None if resultado is None else 10.0, unit="%", period="mes_2026_09",
        provenance=Provenance.EMPTY if resultado is None else Provenance.REAL,
        confidence=1.0, source="t", explanation_key="margen_neto",
        detail={"estado": estado, "resultado_ars": resultado},
    )


def _capital(dinero: float, especie: float = 0.0) -> CapitalDeSocios:
    cero = MontoPorForma(dinero=0.0, especie=0.0)
    totales = CapitalDeSocio(
        socio=None, aportes=cero, retiros=MontoPorForma(dinero=dinero, especie=especie),
        prestamos_recibidos=cero, devoluciones=cero, prestamo_pendiente=0.0,
        prestamo_pendiente_inconsistente=False,
    )
    return CapitalDeSocios(
        start=date(2026, 9, 1), end=date(2026, 9, 30), has_data=True,
        hay_especie=especie > 0, socios=[totales], totales=totales, flujo_dinero=None,
    )


@pytest.mark.parametrize(
    ("resultado", "dinero", "especie", "hay"),
    [
        (5000.0, 6000.0, 0.0, True),   # retiró más de lo que dejó
        (5000.0, 5000.0, 0.0, False),  # igual no es «más»
        (5000.0, 3000.0, 0.0, False),
        (-1000.0, 100.0, 0.0, True),   # resultado negativo: cualquier retiro lo supera
        (-1000.0, 0.0, 0.0, False),    # sin retiros no hay señal
        (5000.0, 4000.0, 2000.0, True),  # la especie también es sacar valor
        (None, 9000.0, 0.0, False),    # sin resultado medido no hay con qué comparar
    ],
)
def test_retiros_vs_resultado(
    resultado: float | None, dinero: float, especie: float, hay: bool
) -> None:
    s = retiros_vs_resultado(_neto(resultado), _capital(dinero, especie))
    assert (s is not None) is hay
    if s is not None:
        assert s.retiros_ars == dinero + especie
        assert s.resultado_ars == resultado
        assert s.estimado is False


def test_sobre_un_neto_estimado_la_senal_lo_dice() -> None:
    s = retiros_vs_resultado(_neto(5000.0, "estimado"), _capital(6000.0))
    assert s is not None and s.estimado is True


# ── Endpoint ─────────────────────────────────────────────────────────────────


def _mov(tid: Any, kind: str, monto: str, d: date, *, form: str = "dinero") -> OwnerCapitalMovement:
    return OwnerCapitalMovement(
        tenant_id=tid, kind=kind, form=form, amount_ars=Decimal(monto), occurred_on=d,
        payment_method="cash" if form == "dinero" else None,
    )


async def test_margins_trae_la_senal_cuando_los_retiros_superan_el_resultado(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    mc = Period.mes_cerrado(today=today_ar())
    db_session.add_all([
        _mov(tid, "retiro", "4000", mc.start + timedelta(days=3)),
        _mov(tid, "retiro", "1500", mc.start + timedelta(days=5), form="especie"),
        # No cuentan: una devolución de préstamo y un retiro de otro mes.
        _mov(tid, "devolucion_prestamo", "9000", mc.start + timedelta(days=6)),
        _mov(tid, "retiro", "9000", mc.start - timedelta(days=3)),
    ])
    await db_session.commit()

    body = (await client.get(URL, headers=auth_headers)).json()

    s = body["retiros_vs_resultado"]
    assert s == {
        "periodo": mc.label, "desde": mc.start.isoformat(), "hasta": mc.end.isoformat(),
        "retiros_ars": 5500.0, "resultado_ars": 5000.0, "estimado": False,
    }
    assert body["neto_mes_cerrado"]["resultado_ars"] == 5000.0


async def test_margins_sin_senal_si_los_retiros_no_superan_el_resultado(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    mc = Period.mes_cerrado(today=today_ar())
    db_session.add(_mov(tid, "retiro", "4000", mc.start + timedelta(days=3)))
    await db_session.commit()

    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["retiros_vs_resultado"] is None


async def test_margins_sin_completitud_no_trae_la_senal(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    mc = Period.mes_cerrado(today=today_ar())
    db_session.add(_mov(tid, "retiro", "90000", mc.start + timedelta(days=3)))
    ahora = datetime.now()
    db_session.add(HealthScoreSnapshot(
        tenant_id=tid, total_score=40, level="WARNING", dimensions=[],
        triggered_by="test", snapshot_date=ahora, created_at=ahora,
        data_completeness_score=Decimal("40"), confidence_level="LOW",
    ))
    await db_session.commit()

    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["sin_datos"] is True
    assert body.get("retiros_vs_resultado") is None


async def test_un_visor_no_ve_los_retiros_del_socio(
    client: AsyncClient, viewer_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Any,
) -> None:
    """Los retiros son capital: los ve OWNER/ADMIN, como en `/capital-movements`."""
    tid = sample_tenant.tenant_id
    await _escenario(db_session, tid)
    mc = Period.mes_cerrado(today=today_ar())
    db_session.add(_mov(tid, "retiro", "9000", mc.start + timedelta(days=3)))
    await db_session.commit()

    body = (await client.get(URL, headers=viewer_headers)).json()
    assert body["sin_datos"] is False
    assert body["retiros_vs_resultado"] is None
