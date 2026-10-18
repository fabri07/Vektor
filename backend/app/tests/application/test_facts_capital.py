"""D·4 — capital por socio en FactsService (función pura sobre el frame).

El frame es el que arma ``facts_provider.load_capital_frame``: movimientos
VIVOS del tenant hasta el fin del período (los anulados y otros tenants ya
quedaron afuera en la query; eso lo fija ``test_owner_capital.py``).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.application.services.facts_service import Period, capital_de_socios
from app.domain.business_time import today_ar

COLS = ["occurred_on", "kind", "form", "amount_ars", "partner_name"]
HOY = today_ar()
PERIODO = Period(start=HOY - timedelta(days=29), end=HOY, label="t")
ANTES = PERIODO.start - timedelta(days=10)


def _frame(*rows: tuple[date, str, str, float, str | None]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=COLS)


def test_aporte_y_retiro_dan_flujo_de_90000() -> None:
    r = capital_de_socios(
        _frame((HOY, "aporte", "dinero", 100000.0, "Juan"),
               (HOY, "retiro", "dinero", 10000.0, "Juan")),
        PERIODO,
    )
    assert r.has_data
    assert r.flujo_dinero is not None
    assert (r.flujo_dinero.entradas, r.flujo_dinero.salidas, r.flujo_dinero.neto) == (
        100000.0, 10000.0, 90000.0)
    (juan,) = r.socios
    assert juan.socio == "Juan"
    assert juan.aportes.dinero == 100000.0
    assert juan.retiros.dinero == 10000.0
    assert juan.prestamo_pendiente == 0.0


def test_prestamo_anterior_y_devolucion_actual_dejan_30000_pendiente() -> None:
    r = capital_de_socios(
        _frame((ANTES, "prestamo_socio", "dinero", 50000.0, "Ana"),
               (HOY, "devolucion_prestamo", "dinero", 20000.0, "Ana")),
        PERIODO,
    )
    (ana,) = r.socios
    # El préstamo es de otro período: no figura como recibido en éste...
    assert ana.prestamos_recibidos.dinero == 0.0
    assert ana.devoluciones.dinero == 20000.0
    # ...pero el pendiente es acumulado a la fecha de fin.
    assert ana.prestamo_pendiente == 30000.0
    assert not ana.prestamo_pendiente_inconsistente
    assert r.totales is not None and r.totales.prestamo_pendiente == 30000.0
    assert r.flujo_dinero is not None and r.flujo_dinero.neto == -20000.0


def test_socio_con_prestamo_viejo_aparece_aunque_no_se_mueva_en_el_periodo() -> None:
    r = capital_de_socios(_frame((ANTES, "prestamo_socio", "dinero", 5000.0, "Ana")), PERIODO)
    assert r.has_data
    (ana,) = r.socios
    assert ana.prestamo_pendiente == 5000.0
    assert ana.aportes.dinero == 0.0


def test_movimiento_viejo_sin_pendiente_no_hace_datos() -> None:
    r = capital_de_socios(_frame((ANTES, "aporte", "dinero", 5000.0, "Ana")), PERIODO)
    assert not r.has_data
    assert r.socios == [] and r.totales is None and r.flujo_dinero is None


def test_movimientos_posteriores_al_fin_no_cuentan() -> None:
    p = Period(start=ANTES - timedelta(days=5), end=ANTES, label="t")
    r = capital_de_socios(
        _frame((ANTES, "prestamo_socio", "dinero", 50000.0, "Ana"),
               (HOY, "devolucion_prestamo", "dinero", 50000.0, "Ana")),
        p,
    )
    (ana,) = r.socios
    assert ana.prestamo_pendiente == 50000.0


def test_devolver_de_mas_no_se_recorta_y_se_marca() -> None:
    r = capital_de_socios(
        _frame((HOY, "prestamo_socio", "dinero", 1000.0, "Ana"),
               (HOY, "devolucion_prestamo", "dinero", 1500.0, "Ana")),
        PERIODO,
    )
    (ana,) = r.socios
    assert ana.prestamo_pendiente == -500.0
    assert ana.prestamo_pendiente_inconsistente
    assert r.totales is not None and r.totales.prestamo_pendiente_inconsistente


def test_sin_socio_se_agrupa_aparte_y_al_final() -> None:
    r = capital_de_socios(
        _frame((HOY, "aporte", "dinero", 100.0, None),
               (HOY, "aporte", "dinero", 50.0, "  "),
               (HOY, "aporte", "dinero", 300.0, "Zoe"),
               (HOY, "aporte", "dinero", 200.0, "ana")),
        PERIODO,
    )
    assert [s.socio for s in r.socios] == ["ana", "Zoe", None]
    assert r.socios[-1].aportes.dinero == 150.0
    assert r.totales is not None and r.totales.aportes.dinero == 650.0


def test_especie_va_aparte_y_no_entra_al_flujo_en_dinero() -> None:
    r = capital_de_socios(
        _frame((HOY, "aporte", "dinero", 1000.0, "Ana"),
               (HOY, "aporte", "especie", 400.0, "Ana")),
        PERIODO,
    )
    (ana,) = r.socios
    assert (ana.aportes.dinero, ana.aportes.especie) == (1000.0, 400.0)
    assert r.hay_especie
    assert r.flujo_dinero is not None and r.flujo_dinero.entradas == 1000.0


def test_solo_dinero_no_marca_especie() -> None:
    r = capital_de_socios(_frame((HOY, "aporte", "dinero", 1.0, "Ana")), PERIODO)
    assert not r.hay_especie


def test_sin_movimientos_es_vacio_explicito() -> None:
    r = capital_de_socios(_frame(), PERIODO)
    assert not r.has_data
    assert r.socios == []
    assert r.totales is None
    assert r.flujo_dinero is None
    assert (r.start, r.end) == (PERIODO.start, PERIODO.end)


def test_el_mismo_socio_con_otra_grafia_es_uno_solo() -> None:
    # Texto libre: «Ana» y « ana » son la misma persona. Si se separaran, «Ana»
    # quedaría debiendo 50.000 y «ana» con −50.000 marcado como inconsistente.
    r = capital_de_socios(
        _frame((ANTES, "prestamo_socio", "dinero", 50000.0, "Ana"),
               (HOY, "devolucion_prestamo", "dinero", 50000.0, " ana ")),
        PERIODO,
    )
    (ana,) = r.socios
    assert ana.socio == "Ana"  # la grafía del primer movimiento
    assert ana.devoluciones.dinero == 50000.0
    assert ana.prestamo_pendiente == 0.0
    assert not ana.prestamo_pendiente_inconsistente


# ── D·5: capital inicial declarado en el onboarding ──────────────────────────

COLS_D5 = [*COLS, "source_type"]


def test_el_capital_declarado_es_aporte_pero_no_flujo_en_dinero() -> None:
    frame = pd.DataFrame(
        [(HOY - timedelta(days=3), "aporte", "dinero", 500000.0, None, "onboarding_declarado"),
         (HOY, "aporte", "dinero", 700.0, None, "manual")],
        columns=COLS_D5,
    )
    r = capital_de_socios(frame, PERIODO)
    (sin_socio,) = r.socios
    assert sin_socio.aportes.dinero == 500700.0
    assert r.flujo_dinero is not None
    assert (r.flujo_dinero.entradas, r.flujo_dinero.neto) == (700.0, 700.0)
    assert r.capital_inicial_declarado is not None
    assert r.capital_inicial_declarado.monto == 500000.0
    assert r.capital_inicial_declarado.fecha == HOY - timedelta(days=3)


def test_capital_declarado_fuera_del_periodo_no_se_informa() -> None:
    frame = pd.DataFrame(
        [(ANTES, "aporte", "dinero", 500000.0, None, "onboarding_declarado"),
         (HOY, "retiro", "dinero", 100.0, "Juan", "manual")],
        columns=COLS_D5,
    )
    r = capital_de_socios(frame, PERIODO)
    assert r.capital_inicial_declarado is None
    assert [s.socio for s in r.socios] == ["Juan"]


def test_sin_columna_de_procedencia_todo_es_manual() -> None:
    r = capital_de_socios(_frame((HOY, "aporte", "dinero", 900.0, None)), PERIODO)
    assert r.capital_inicial_declarado is None
    assert r.flujo_dinero is not None and r.flujo_dinero.entradas == 900.0
