"""I6-5 — el convenio de fecha es de la COLUMNA también cuando la fecha trae hora.

`_SEPARADOR_DE_FECHA` estaba anclado con ``$`` y no admitía la hora: en una
columna exportada en formato US con hora ninguna celda aportaba señal, la columna
caía al default dmy y cada celda se resolvía por su cuenta. ``'03/04/2026 11:00'``
quedaba como 3 de abril en una columna que dice 4 de marzo.
"""

from __future__ import annotations

from datetime import date, datetime

from app.domain.date_parsing import (
    MOTIVO_FECHA_AMBIGUA,
    ConvenioDeFecha,
    inferir_convenio_de_fecha,
    parsear_fecha_de_columna,
)

_HOY = date(2026, 10, 28)
_COLUMNA_US = ["04/13/2026 10:30", "03/04/2026 11:00", "05/20/2026 09:15"]


class TestLaHoraNoTapaLaSenalDeLaColumna:
    def test_una_columna_mm_dd_con_hora_es_inequivoca(self) -> None:
        assert inferir_convenio_de_fecha(_COLUMNA_US) == ConvenioDeFecha(
            orden="mdy", origen="inequivoco"
        )

    def test_la_ambigua_se_lee_con_el_orden_de_su_columna_y_conserva_la_hora(self) -> None:
        convenio = inferir_convenio_de_fecha(_COLUMNA_US)
        r = parsear_fecha_de_columna("03/04/2026 11:00", convenio, hoy=_HOY)
        assert r.valor == datetime(2026, 3, 4, 11, 0)

    def test_con_segundos_y_guiones_tambien(self) -> None:
        columna = ["04-13-2026 10:30:15", "03-04-2026 11:00:05"]
        convenio = inferir_convenio_de_fecha(columna)
        assert convenio is not None and convenio.orden == "mdy"
        r = parsear_fecha_de_columna("03-04-2026 11:00:05", convenio, hoy=_HOY)
        assert r.valor == datetime(2026, 3, 4, 11, 0, 5)

    def test_una_fecha_que_seria_futura_leida_dd_mm_entra_en_su_mes(self) -> None:
        """'10/12/2026 09:00' en una columna US es el 12 de octubre: leída dd/mm
        era diciembre y la venta iba a «Otros» como fecha futura."""
        convenio = inferir_convenio_de_fecha(["04/13/2026 10:30", "10/12/2026 09:00"])
        r = parsear_fecha_de_columna("10/12/2026 09:00", convenio, hoy=_HOY)
        assert r.motivo is None
        assert r.valor == datetime(2026, 10, 12, 9, 0)

    def test_la_columna_argentina_con_hora_sigue_igual(self) -> None:
        columna = ["13/04/2026 10:30", "03/04/2026 11:00"]
        convenio = inferir_convenio_de_fecha(columna)
        assert convenio == ConvenioDeFecha(orden="dmy", origen="inequivoco")
        r = parsear_fecha_de_columna("03/04/2026 11:00", convenio, hoy=_HOY)
        assert r.valor == datetime(2026, 4, 3, 11, 0)

    def test_sin_ninguna_senal_manda_el_default_argentino(self) -> None:
        convenio = inferir_convenio_de_fecha(["03/04/2026 11:00", "05/06/2026 12:00"])
        assert convenio == ConvenioDeFecha(orden="dmy", origen="default")

    def test_una_columna_contradictoria_con_hora_manda_la_ambigua_a_revision(self) -> None:
        """La contradicción tampoco se veía: sin señal, ni siquiera se detectaba."""
        convenio = inferir_convenio_de_fecha(
            ["13/04/2026 10:30", "04/13/2026 11:00", "03/04/2026 09:00"]
        )
        assert convenio is None
        r = parsear_fecha_de_columna("03/04/2026 09:00", convenio, hoy=_HOY)
        assert r.valor is None
        assert r.motivo == MOTIVO_FECHA_AMBIGUA
