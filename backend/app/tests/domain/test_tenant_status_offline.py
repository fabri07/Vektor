"""Regla pura de la caja offline con la cuenta bloqueada (F2 del panel SUPERADMIN)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.tenant_status import (
    MAX_ANTIGUEDAD_VENTA_OFFLINE,
    venta_offline_aceptable,
)

_AHORA = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def test_antes_del_corte_y_dentro_de_la_ventana_se_acepta() -> None:
    corte = _AHORA - timedelta(hours=1)
    assert venta_offline_aceptable(_AHORA - timedelta(hours=2), corte, _AHORA)


def test_despues_del_corte_no() -> None:
    corte = _AHORA - timedelta(hours=3)
    assert not venta_offline_aceptable(_AHORA - timedelta(hours=2), corte, _AHORA)


def test_justo_en_el_corte_no() -> None:
    assert not venta_offline_aceptable(_AHORA, _AHORA, _AHORA)


def test_mas_vieja_que_la_ventana_no_aunque_sea_anterior_al_corte() -> None:
    cobrada = _AHORA - MAX_ANTIGUEDAD_VENTA_OFFLINE - timedelta(minutes=1)
    assert not venta_offline_aceptable(cobrada, _AHORA, _AHORA)
    assert venta_offline_aceptable(_AHORA - MAX_ANTIGUEDAD_VENTA_OFFLINE, _AHORA, _AHORA)


def test_sin_corte_o_sin_charged_at_falla_cerrado() -> None:
    assert not venta_offline_aceptable(_AHORA - timedelta(hours=1), None, _AHORA)
    assert not venta_offline_aceptable(None, _AHORA, _AHORA)


def test_acepta_fechas_sin_zona_como_utc() -> None:
    """SQLite devuelve los DateTime(timezone=True) sin zona."""
    corte = (_AHORA - timedelta(hours=1)).replace(tzinfo=None)
    cobrada = (_AHORA - timedelta(hours=2)).replace(tzinfo=None)
    assert venta_offline_aceptable(cobrada, corte, _AHORA)
