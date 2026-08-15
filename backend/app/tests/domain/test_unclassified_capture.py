"""F-O.4 — `is_aggregate_row`: una fila de agregado (Subtotal/Total) no es un
movimiento. Módulo puro, sin DB — casos reales medidos en la muestra que
motivó la fase."""

from __future__ import annotations

from app.domain.unclassified_capture import is_aggregate_row


class TestConAnchorColumn:
    """Cuando el mapeo ya resolvió la columna de fecha/ancla de la hoja."""

    def test_valor_exacto_subtotal_en_la_columna_ancla(self) -> None:
        assert is_aggregate_row({"fecha": "Subtotal", "monto": "1500"}, anchor_column="fecha")

    def test_valor_exacto_total_en_la_columna_ancla(self) -> None:
        assert is_aggregate_row({"fecha": "Total", "monto": "9000"}, anchor_column="fecha")

    def test_totales_y_subtotales_en_plural(self) -> None:
        assert is_aggregate_row({"fecha": "Totales"}, anchor_column="fecha")
        assert is_aggregate_row({"fecha": "Subtotales"}, anchor_column="fecha")

    def test_case_insensitive_y_con_acentos(self) -> None:
        assert is_aggregate_row({"fecha": "SUBTOTAL"}, anchor_column="fecha")
        assert is_aggregate_row({"fecha": "  total  "}, anchor_column="fecha")

    def test_fecha_real_que_contiene_la_subcadena_no_se_confunde(self) -> None:
        """Regresión explícita: "Total" NO puede ser un match parcial — una
        fecha real que contenga la subcadena (ej. un texto libre) no debe
        descartarse como agregado."""
        assert not is_aggregate_row(
            {"fecha": "Total facturado 15/03/2024"}, anchor_column="fecha"
        )
        assert not is_aggregate_row({"fecha": "2024-03-15"}, anchor_column="fecha")

    def test_columna_ancla_ausente_o_none_no_es_agregado(self) -> None:
        assert not is_aggregate_row({"monto": "100"}, anchor_column="fecha")
        assert not is_aggregate_row({"fecha": None, "monto": "100"}, anchor_column="fecha")

    def test_solo_mira_la_columna_ancla_no_otras_celdas(self) -> None:
        """Aunque otra celda de la fila diga "Total", si la ancla es una fecha
        real la fila NO es de agregado — con anchor_column conocida, es la
        ÚNICA fuente de verdad."""
        assert not is_aggregate_row(
            {"fecha": "2024-03-15", "categoria": "Total"}, anchor_column="fecha"
        )


class TestSinAnchorColumn:
    """Hoja completamente no clasificada, sin target de fecha confirmado —
    heurística conservadora: exige match exacto Y poco contenido en la fila."""

    def test_fila_dispersa_con_total_y_un_monto_es_agregado(self) -> None:
        assert is_aggregate_row({"col_a": "Total", "col_b": "18500"})

    def test_fila_solo_con_total_es_agregado(self) -> None:
        assert is_aggregate_row({"col_a": "Total"})

    def test_fila_con_datos_reales_y_una_celda_total_no_se_descarta(self) -> None:
        """No alcanza con que UNA celda diga "Total" — si el resto de la fila
        tiene datos reales (una operación real), no es agregado."""
        assert not is_aggregate_row(
            {
                "producto": "Total limpieza premium",
                "cantidad": "3",
                "precio": "1200",
                "fecha": "2024-03-15",
            }
        )

    def test_fila_sin_ninguna_celda_agregado_nunca_dispara(self) -> None:
        assert not is_aggregate_row({"col_a": "Agua mineral", "col_b": "500"})

    def test_context_marker_se_ignora(self) -> None:
        assert is_aggregate_row({"__context__": "sheet:1", "col_a": "Total"})
