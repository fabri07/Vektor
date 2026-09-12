"""F6-C1: parser único de fechas de negocio.

La razón de existir de este módulo es que el gate de calidad y el importador
tenían listas de formatos distintas y discrepaban sobre el MISMO archivo.
Estos tests son el contrato compartido: si alguien agrega un formato, va acá.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.domain.date_parsing import (
    MOTIVO_FECHA_AMBIGUA,
    MOTIVO_FECHA_ILEGIBLE,
    inferir_convenio_de_fecha,
    parse_business_date,
    parse_business_datetime,
    parse_excel_serial,
    parsear_fecha_de_columna,
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
    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            pytest.param("05/06/2026", date(2026, 6, 5), id="test_devuelve_date"),
            pytest.param("2026-06-05T14:30:00", date(2026, 6, 5), id="test_descarta_la_hora"),
            # Un `date`/`datetime` que ya viene tipado pasa derecho, sin re-parsear.
            pytest.param(date(2026, 6, 5), date(2026, 6, 5), id="test_passthrough_de_date"),
            pytest.param(
                datetime(2026, 6, 5, 14, 30),
                date(2026, 6, 5),
                id="test_passthrough_de_datetime",
            ),
        ],
    )
    def test_devuelve_el_date(self, raw: object, esperado: date) -> None:
        assert parse_business_date(raw) == esperado

    def test_no_parseable(self) -> None:
        assert parse_business_date("basura") is None


class TestPivoteDeSiglo:
    """Los cumpleaños necesitan pivote 30 (una persona nacida en '50 es 1950).
    Las transacciones usan el default de strptime. La diferencia es explícita,
    nunca implícita.
    """

    @pytest.mark.parametrize(
        ("raw", "pivote", "esperado"),
        [
            # `None` = sin pivote explícito, o sea el default de strptime.
            pytest.param("01/01/26", None, date(2026, 1, 1), id="test_default_usa_strptime"),
            pytest.param(
                "15/08/50",
                30,
                date(1950, 8, 15),
                id="test_pivote_explicito_manda_al_1900",
            ),
            pytest.param(
                "15/08/26",
                30,
                date(2026, 8, 15),
                id="test_pivote_explicito_deja_reciente_en_2000",
            ),
        ],
    )
    def test_pivote(self, raw: str, pivote: int | None, esperado: date) -> None:
        assert parse_business_date(raw, century_pivot=pivote) == esperado


class TestElOrdenDiaMesLoDecideLaColumna:
    """`03/04/2026` es el 3 de abril o el 4 de marzo, y la celda no lo dice.

    El orden de `BUSINESS_DATE_FORMATS` la resuelve como 3 de abril, que es la
    convención argentina y está bien mientras el archivo sea argentino. Deja de
    estarlo cuando no lo es: en una columna exportada en formato US, `04/13/2026`
    sólo se puede leer mm/dd —no hay mes 13— y esa evidencia vale para TODA la
    columna, incluida la fecha ambigua de al lado, que se leía al revés y erraba
    por un mes entero sin que nada lo marcara.
    """

    def test_una_fecha_imposible_revela_el_orden_de_su_columna(self) -> None:
        convenio = inferir_convenio_de_fecha(["04/13/2026", "03/04/2026"])
        assert convenio is not None
        assert convenio.orden == "mdy" and convenio.origen == "inequivoco"
        assert parsear_fecha_de_columna("03/04/2026", convenio).valor == datetime(2026, 3, 4)

    def test_la_columna_argentina_sigue_leyendose_como_siempre(self) -> None:
        convenio = inferir_convenio_de_fecha(["13/04/2026", "03/04/2026"])
        assert convenio is not None and convenio.orden == "dmy"
        assert parsear_fecha_de_columna("03/04/2026", convenio).valor == datetime(2026, 4, 3)

    def test_sin_ninguna_senal_manda_la_convencion_argentina(self) -> None:
        """No es una adivinanza: es la convención declarada del importador, y
        cambiarla rompería todos los imports que hoy andan bien. Lo que sí cambia
        es que ahora queda explícito que salió del default y no de la columna."""
        convenio = inferir_convenio_de_fecha(["03/04/2026", "05/06/2026"])
        assert convenio is not None
        assert convenio.orden == "dmy" and convenio.origen == "default"
        assert parsear_fecha_de_columna("03/04/2026", convenio).valor == datetime(2026, 4, 3)

    def test_una_columna_contradictoria_manda_lo_ambiguo_a_revision(self) -> None:
        """`13/04` y `04/13` en la misma columna: cada una excluye el orden de la
        otra. Elegir uno le erraría a la mitad de las filas, así que lo que se
        puede leer se lee y lo ambiguo va a revisión con el original."""
        assert inferir_convenio_de_fecha(["13/04/2026", "04/13/2026"]) is None
        ambigua = parsear_fecha_de_columna("03/04/2026", None)
        assert ambigua.valor is None
        assert ambigua.motivo == MOTIVO_FECHA_AMBIGUA
        assert ambigua.original == "03/04/2026"
        # Las que se explican solas no dependen del convenio y entran igual.
        assert parsear_fecha_de_columna("13/04/2026", None).valor == datetime(2026, 4, 13)
        assert parsear_fecha_de_columna("2026-03-04", None).valor == datetime(2026, 3, 4)

    def test_un_nativo_no_se_reinterpreta(self) -> None:
        nativo = datetime(2026, 3, 4, 14, 30)
        assert parsear_fecha_de_columna(nativo, None).valor == nativo

    def test_una_fecha_ilegible_no_es_ambigua(self) -> None:
        rota = parsear_fecha_de_columna("ayer", None)
        assert rota.valor is None
        assert rota.motivo == MOTIVO_FECHA_ILEGIBLE

    def test_vacio_no_genera_motivo(self) -> None:
        assert parsear_fecha_de_columna("", None).ausente is True
        assert parsear_fecha_de_columna(None, None).ausente is True


class TestSerialesDeExcel:
    """Un `.xlsx` con la celda sin formato de fecha —y todo CSV exportado desde
    Excel— trae el número de días desde 1899-12-30. openpyxl ya devuelve
    `datetime` para las celdas formateadas, así que esto cubre justo el caso que
    quedaba ilegible y mandaba la fila a revisión sin motivo real."""

    @pytest.mark.parametrize(
        ("serial", "esperado"),
        [
            (45123, datetime(2023, 7, 16)),
            (45123.5, datetime(2023, 7, 16, 12)),
            (32874, datetime(1990, 1, 1)),
        ],
    )
    def test_convierte_dentro_de_la_ventana(self, serial: float, esperado: datetime) -> None:
        assert parse_excel_serial(serial) == esperado
        assert parsear_fecha_de_columna(serial, None).valor == esperado

    def test_un_serial_escrito_como_texto_tambien_cuenta(self) -> None:
        """El parser de archivos normaliza toda celda a texto antes de llegar acá:
        un serial SIEMPRE viaja como `"45123"`. Rechazar los strings dejaba la
        conversión sin ningún caso real — lo descubrió el test e2e, no éste."""
        assert parse_excel_serial("45123") == datetime(2023, 7, 16)

    @pytest.mark.parametrize("valor", [1500, 0, -5, 99999, True, "hoy", "45.123,5", None])
    def test_afuera_de_la_ventana_no_se_convierte(self, valor: object) -> None:
        """La ventana existe para que un precio mal mapeado no se vuelva una fecha
        de 1904 perfectamente plausible que nadie iba a notar. Un `True` tampoco
        es el día 1."""
        assert parse_excel_serial(valor) is None
