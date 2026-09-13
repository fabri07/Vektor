"""Cambio 3 (docs/plans/conservacion-y-acceso-datos-negocio.md): formato de
celda EXACTO para exportar, distinto del formato de UI."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.domain.csv_safety import format_export_value, formula_safe_cell


class TestFormulaSafeCell:
    def test_prefija_los_disparadores_conocidos(self) -> None:
        for lead in ("=", "+", "-", "@", "\t", "\r"):
            assert formula_safe_cell(f"{lead}cmd|'/c calc'!A1") == f"'{lead}cmd|'/c calc'!A1"

    def test_no_toca_un_valor_normal(self) -> None:
        assert formula_safe_cell("Silla de living") == "Silla de living"

    def test_solo_dispara_por_el_primer_caracter(self) -> None:
        assert formula_safe_cell("guión - en el medio") == "guión - en el medio"

    def test_cadena_vacia_no_explota(self) -> None:
        assert formula_safe_cell("") == ""


class TestFormatExportValue:
    def test_none_es_cadena_vacia_no_un_guion(self) -> None:
        assert format_export_value(None, "text") == ""
        assert format_export_value(None, "number") == ""

    def test_decimal_se_exporta_exacto_no_redondeado(self) -> None:
        assert format_export_value(Decimal("15000.005"), "number") == "15000.005"
        assert format_export_value(Decimal("0"), "number") == "0"

    def test_entero_exacto(self) -> None:
        assert format_export_value(3, "number") == "3"

    def test_fecha_en_iso_inequivoco(self) -> None:
        assert format_export_value(date(2026, 3, 5), "date") == "2026-03-05"
        assert format_export_value(datetime(2026, 3, 5, 14, 30), "date") == "2026-03-05T14:30:00"

    def test_boolean_como_token_literal_no_si_no(self) -> None:
        assert format_export_value(True, "boolean") == "true"
        assert format_export_value(False, "boolean") == "false"

    def test_texto_preserva_ceros_a_la_izquierda(self) -> None:
        assert format_export_value("0007", "text") == "0007"

    def test_falso_y_cero_no_son_ausencia(self) -> None:
        assert format_export_value(False, "boolean") != ""
        assert format_export_value(Decimal("0"), "number") != ""
