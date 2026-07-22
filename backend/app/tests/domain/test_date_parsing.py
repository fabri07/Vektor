"""F6-C1: parser único de fechas de negocio.

La razón de existir de este módulo es que el gate de calidad y el importador
tenían listas de formatos distintas y discrepaban sobre el MISMO archivo.
Estos tests son el contrato compartido: si alguien agrega un formato, va acá.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.domain.date_parsing import (
    parse_business_date,
    parse_business_datetime,
)


class TestFormatosAceptados:
    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            # ISO — el caso que validation_gate NO aceptaba y el importador sí.
            ("2026-06-05T14:30:00", datetime(2026, 6, 5, 14, 30)),
            ("2026-06-05 14:30:00", datetime(2026, 6, 5, 14, 30)),
            ("2026-06-05", datetime(2026, 6, 5)),
            # dd/mm con hora
            ("05/06/2026 14:30:00", datetime(2026, 6, 5, 14, 30)),
            ("05/06/2026 14:30", datetime(2026, 6, 5, 14, 30)),
            ("05-06-2026 14:30", datetime(2026, 6, 5, 14, 30)),
            # solo fecha
            ("05/06/2026", datetime(2026, 6, 5)),
            ("05-06-2026", datetime(2026, 6, 5)),
            ("2026/06/05", datetime(2026, 6, 5)),
            ("5/6/2026", datetime(2026, 6, 5)),
            # año de 2 dígitos: las CUATRO combinaciones separador x largo (C2).
            ("05/06/26", datetime(2026, 6, 5)),
            ("05-06-26", datetime(2026, 6, 5)),
        ],
    )
    def test_parsea(self, raw: str, esperado: datetime) -> None:
        assert parse_business_datetime(raw) == esperado

    def test_dd_mm_yy_con_guiones_es_el_gap_de_c2(self) -> None:
        """Antes de F6-C esto devolvía None y la fila caía en el fallback a hoy."""
        assert parse_business_datetime("05-06-26") == datetime(2026, 6, 5)


class TestConvencionArgentina:
    def test_dia_antes_que_mes(self) -> None:
        """03/04/2026 es 3 de ABRIL, no 4 de marzo. Convención AR, determinística."""
        assert parse_business_datetime("03/04/2026") == datetime(2026, 4, 3)

    def test_ambos_menores_a_13_no_es_ambiguo_para_nosotros(self) -> None:
        # El test viejo (test_ingestion_import_multisheet) usaba 05/06/2026, que
        # pasa igual con dd/mm y mm/dd — no discriminaba nada.
        assert parse_business_datetime("05/06/2026") == datetime(2026, 6, 5)

    def test_cae_a_mm_dd_solo_si_dd_mm_es_imposible(self) -> None:
        # 25 no es un mes válido, así que dd/mm falla y gana mm/dd.
        assert parse_business_datetime("12/25/2026") == datetime(2026, 12, 25)


class TestNoParseables:
    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "no soy fecha", "05/06/226", "32/01/2026", "00/00/0000"],
    )
    def test_devuelve_none(self, raw: object) -> None:
        assert parse_business_datetime(raw) is None

    def test_nunca_inventa_hoy(self) -> None:
        """Invariante de F6: un valor ilegible es None, jamás la fecha actual."""
        assert parse_business_datetime("basura") is None


class TestParseBusinessDate:
    def test_devuelve_date(self) -> None:
        assert parse_business_date("05/06/2026") == date(2026, 6, 5)

    def test_descarta_la_hora(self) -> None:
        assert parse_business_date("2026-06-05T14:30:00") == date(2026, 6, 5)

    def test_passthrough_de_date(self) -> None:
        d = date(2026, 6, 5)
        assert parse_business_date(d) == d

    def test_passthrough_de_datetime(self) -> None:
        assert parse_business_date(datetime(2026, 6, 5, 14, 30)) == date(2026, 6, 5)

    def test_no_parseable(self) -> None:
        assert parse_business_date("basura") is None


class TestPivoteDeSiglo:
    """Los cumpleaños necesitan pivote 30 (una persona nacida en '50 es 1950).
    Las transacciones usan el default de strptime. La diferencia es explícita,
    nunca implícita.
    """

    def test_default_usa_strptime(self) -> None:
        assert parse_business_date("01/01/26") == date(2026, 1, 1)

    def test_pivote_explicito_manda_al_1900(self) -> None:
        assert parse_business_date("15/08/50", century_pivot=30) == date(1950, 8, 15)

    def test_pivote_explicito_deja_reciente_en_2000(self) -> None:
        assert parse_business_date("15/08/26", century_pivot=30) == date(2026, 8, 15)
