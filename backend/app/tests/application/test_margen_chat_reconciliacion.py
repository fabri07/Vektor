"""M·2b-2 — el chat dice el MISMO margen que FactsService (invariante 2c).

Contra la base real (SQLite): el prompt del chat, la consulta de caja y el
punto de equilibrio leen ``margenes_del_tenant``, que arma FactsService con el
rubro y la procedencia del tenant (``sample_tenant`` ya trae su perfil). Acá se
verifica que el número que llega al prompt es el del hecho, con su rótulo.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.chat.agent import AgentChat
from app.application.agents.shared.margen_texto import (
    describir_margen_bruto,
    describir_margen_neto,
    nombre_del_mes,
)
from app.application.services.facts_provider import margenes_del_tenant
from app.domain.business_time import today_ar
from app.persistence.models.transaction import SaleEntry

#: Los lectores del margen exigen un score con datos (compuerta de completitud).
pytestmark = pytest.mark.usefixtures("score_suficiente")


def _venta(tid, dias: int, monto: str, costo: str | None, confirmado: bool,
           metodo: str = "cash", provenance: str = "REAL") -> SaleEntry:
    snapshot = (
        {"cost_unit_ars": Decimal(costo), "cost_ars": Decimal(costo),
         "cost_confirmed": confirmado, "cost_origin": "compra" if confirmado else "catalogo",
         "cost_snapshot_version": 1}
        if costo is not None else {}
    )
    return SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=1, payment_method=metodo,
        transaction_date=datetime.combine(today_ar() - timedelta(days=dias), time(12)),
        provenance=provenance, **snapshot,
    )


async def test_chat_y_facts_dan_el_mismo_margen_estimado(
    db_session: AsyncSession, sample_tenant
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add_all([
        _venta(tid, 1, "6000", "3000", confirmado=False),   # estimada
        _venta(tid, 2, "2000", None, confirmado=False),     # sin costo
        _venta(tid, 3, "5000", None, False, metodo="inflow"),  # cobro: no es venta
        _venta(tid, 3, "9999", "1", True, provenance="DEMO"),  # tenant real: fuera
    ])
    await db_session.commit()

    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    bruto = margenes.bruto
    # 6000 de 8000 con costo (75%), todo estimado → 50% estimado.
    assert bruto.value == 50.0
    assert bruto.detail is not None
    assert bruto.detail["estado"] == "estimado"
    assert bruto.detail["cobertura_total_pct"] == 75.0

    contexto = await AgentChat()._load_financial_context(tid, db_session)
    # M·3-1: el chat dice el bruto del MES EN CURSO y el neto del mes cerrado,
    # los mismos hechos de FactsService (comparados contra el hecho y no contra
    # un número fijo: en los primeros días del mes estas ventas son del anterior).
    en_curso = margenes.bruto_mes_en_curso
    assert en_curso is not None
    assert (
        "Margen bruto del mes en curso (ventas − costo de lo vendido): "
        f"{describir_margen_bruto(en_curso)}"
    ) in contexto
    assert (
        f"Margen neto de {nombre_del_mes(margenes.neto)} (mes cerrado; "
        "ventas − costo de lo vendido − gastos operativos): "
        f"{describir_margen_neto(margenes.neto)}"
    ) in contexto
    assert "Resultado (ventas − todos los gastos)" in contexto


async def test_chat_dice_sin_medir_con_el_motivo(
    db_session: AsyncSession, sample_tenant
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add_all([
        _venta(tid, 1, "1000", "500", confirmado=True),
        _venta(tid, 1, "9000", None, confirmado=False),
    ])
    await db_session.commit()

    margenes = await margenes_del_tenant(db_session, tid)
    assert margenes is not None
    assert margenes.bruto.detail is not None
    assert margenes.bruto.detail["motivo"] == "cobertura de costo 10,0%"
    contexto = await AgentChat()._load_financial_context(tid, db_session)
    assert margenes.bruto_mes_en_curso is not None
    assert "Margen bruto del mes en curso (ventas − costo de lo vendido): sin medir (" in (
        contexto
    )
    assert describir_margen_bruto(margenes.bruto_mes_en_curso) in contexto
    # Nunca un 0 inventado ni el margen viejo (ventas − gastos).
    assert "Margen bruto: 100" not in contexto


async def test_un_error_de_facts_no_tumba_la_respuesta(
    db_session: AsyncSession, sample_tenant, monkeypatch
) -> None:
    """Revisión de M·2b-2: si FactsService falla, el margen dice «sin medir»
    y el resto del contexto (ventas, gastos) sigue."""
    from app.application.services import facts_provider

    async def _explota(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(facts_provider, "margenes_del_tenant", _explota)
    tid = sample_tenant.tenant_id
    db_session.add(_venta(tid, 1, "1000", "500", confirmado=True))
    await db_session.commit()

    lectura = await facts_provider.leer_margenes(db_session, tid)
    assert (lectura.margenes, lectura.motivo) == (None, facts_provider.MOTIVO_ERROR)
    contexto = await AgentChat()._load_financial_context(tid, db_session)
    assert "Ventas: $1000" in contexto
    assert "Margen neto: sin medir (no se pudo medir ahora)" in contexto
