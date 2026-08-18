"""F-D (sub-commit 3) — buffer puro de campos cross-sección: "primera fila
gana" y la definición de "vacío" por tipo de campo (texto vs numérico)."""

from __future__ import annotations

from app.domain.cross_field_buffer import (
    CrossFieldBuffer,
    is_cross_value_blank,
)


class TestIsCrossValueBlank:
    def test_texto_none_vacio_espacios_y_nan_cuentan_como_vacio(self) -> None:
        for value in (None, "", "   ", "nan", "NaN", "  nan  "):
            assert is_cross_value_blank("last_name", value) is True

    def test_texto_con_contenido_no_es_vacio(self) -> None:
        assert is_cross_value_blank("last_name", "Pérez") is False
        assert is_cross_value_blank("address", "San Martín 123") is False

    def test_numerico_solo_none_es_vacio_cero_es_dato_valido(self) -> None:
        assert is_cross_value_blank("unit_cost_ars", None) is True
        assert is_cross_value_blank("unit_cost_ars", 0) is False
        assert is_cross_value_blank("unit_cost_ars", 0.0) is False
        assert is_cross_value_blank("unit_cost_ars", "") is False  # no es None


class TestCrossFieldBufferPrimeraFilaGana:
    def test_primera_fila_con_dato_gana_segunda_se_ignora(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "cust-1", "last_name", "Pérez", source_row_ref="row:1")
        buf.add("customer", "cust-1", "last_name", "Gómez", source_row_ref="row:2")
        [(kind, entity_id, fields)] = buf.resolved()
        assert kind == "customer"
        assert entity_id == "cust-1"
        assert fields["last_name"].value == "Pérez"
        assert fields["last_name"].source_row_ref == "row:1"

    def test_valores_vacios_no_ganan_ni_bloquean_una_fila_posterior_con_dato(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "cust-1", "last_name", "", source_row_ref="row:1")
        buf.add("customer", "cust-1", "last_name", "Pérez", source_row_ref="row:2")
        [(_, _, fields)] = buf.resolved()
        assert fields["last_name"].value == "Pérez"
        assert fields["last_name"].source_row_ref == "row:2"

    def test_campos_distintos_de_la_misma_entidad_coexisten(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "cust-1", "last_name", "Pérez", source_row_ref="row:1")
        buf.add("customer", "cust-1", "address", "San Martín 123", source_row_ref="row:2")
        [(_, _, fields)] = buf.resolved()
        assert set(fields) == {"last_name", "address"}

    def test_cero_numerico_de_la_primera_fila_gana_no_se_trata_como_vacio(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("product", "prod-1", "unit_cost_ars", 0, source_row_ref="row:1")
        buf.add("product", "prod-1", "unit_cost_ars", 500, source_row_ref="row:2")
        [(_, _, fields)] = buf.resolved()
        assert fields["unit_cost_ars"].value == 0
        assert fields["unit_cost_ars"].source_row_ref == "row:1"

    def test_entidades_distintas_no_se_mezclan(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "cust-1", "last_name", "Pérez", source_row_ref="row:1")
        buf.add("customer", "cust-2", "last_name", "Gómez", source_row_ref="row:2")
        assert len(buf) == 2
        resolved = {(kind, eid): fields for kind, eid, fields in buf.resolved()}
        assert resolved[("customer", "cust-1")]["last_name"].value == "Pérez"
        assert resolved[("customer", "cust-2")]["last_name"].value == "Gómez"

    def test_buffer_vacio_no_resuelve_nada(self) -> None:
        buf = CrossFieldBuffer()
        assert buf.resolved() == []
        assert len(buf) == 0
