"""Un instante CON zona se lleva a hora argentina antes de tomar la fecha."""

from datetime import UTC, datetime, timedelta

from app.domain.business_time import AR_TZ, fecha_de_negocio


def test_sin_zona_es_la_fecha_tipeada() -> None:
    assert fecha_de_negocio(datetime(2026, 9, 24, 23, 30)).isoformat() == "2026-09-24"


def test_las_2130_de_argentina_en_utc_siguen_siendo_hoy() -> None:
    # 21:30 AR = 00:30 UTC del día siguiente: su `.date()` crudo sería mañana.
    instante = datetime(2026, 9, 25, 0, 30, tzinfo=UTC)
    assert instante.date().isoformat() == "2026-09-25"
    assert fecha_de_negocio(instante).isoformat() == "2026-09-24"


def test_cualquier_zona() -> None:
    instante = datetime(2026, 9, 24, 12, 0, tzinfo=AR_TZ) + timedelta(hours=0)
    assert fecha_de_negocio(instante).isoformat() == "2026-09-24"
