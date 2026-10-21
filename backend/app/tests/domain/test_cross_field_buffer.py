"""B·6 (F-D) — el buffer de campos de otra sección y la normalización del valor.

Puro: qué valor propone cada fila para cada ficha, cuál gana y cuándo una celda
no se puede guardar. La escritura y la reversa se prueban en
``app/tests/services/test_campos_cruzados_b6.py``.
"""

from __future__ import annotations

from app.domain.cross_field_buffer import (
    CrossFieldBuffer,
    is_ficha_value_blank,
    normalize_cross_value,
)


class TestPrimeraFilaGana:
    def test_la_primera_fila_con_valor_gana_y_las_demas_no_pisan(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "c1", "last_name", "Pérez", source_row_ref="r1")
        buf.add("customer", "c1", "last_name", "Gómez", source_row_ref="r2")

        [(kind, eid, campos)] = buf.resolved()
        assert (kind, eid) == ("customer", "c1")
        assert campos["last_name"].value == "Pérez"
        assert campos["last_name"].source_row_ref == "r1"

    def test_una_celda_vacia_no_ocupa_el_lugar_de_la_siguiente(self) -> None:
        """Si la primera fila trae la celda vacía, no "gana" con nada: la
        siguiente con dato es la que completa."""
        buf = CrossFieldBuffer()
        for vacio in (None, "", "   ", "nan", float("nan")):
            buf.add("customer", "c1", "locality", vacio, source_row_ref="r0")
        buf.add("customer", "c1", "locality", "San Isidro", source_row_ref="r2")

        [(_, _, campos)] = buf.resolved()
        assert campos["locality"].value == "San Isidro"

    def test_un_valor_invalido_no_gana_y_se_cuenta(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "c1", "iva_condition", "Inventada", source_row_ref="r1")
        buf.add("customer", "c1", "iva_condition", "Responsable Inscripto", source_row_ref="r2")

        [(_, _, campos)] = buf.resolved()
        assert campos["iva_condition"].value == "responsable_inscripto"
        assert buf.invalid == 1

    def test_una_entrada_por_ficha_con_todos_sus_campos(self) -> None:
        buf = CrossFieldBuffer()
        buf.add("customer", "c1", "last_name", "Pérez", source_row_ref=None)
        buf.add("customer", "c1", "address", "Mitre 10", source_row_ref=None)
        buf.add("supplier", "s1", "payment_method", "transferencia", source_row_ref=None)

        assert len(buf) == 2
        por_ficha = {(k, e): set(c) for k, e, c in buf.resolved()}
        assert por_ficha == {
            ("customer", "c1"): {"last_name", "address"},
            ("supplier", "s1"): {"payment_method"},
        }


class TestNormalizacion:
    def test_catalogo_cerrado_se_traduce_al_valor_canonico(self) -> None:
        assert normalize_cross_value("customer", "customer_type", "Empresa").value == "company"
        assert normalize_cross_value("customer", "customer_type", "Persona física").value == (
            "person"
        )
        assert normalize_cross_value("customer", "iva_condition", "Consumidor Final").value == (
            "consumidor_final"
        )
        assert normalize_cross_value("customer", "iva_condition", "R.I.").value == (
            "responsable_inscripto"
        )

    def test_catalogo_cerrado_desconocido_es_invalido_nunca_se_guarda_tal_cual(self) -> None:
        r = normalize_cross_value("customer", "customer_type", "Mayorista")
        assert not r.valid
        assert r.value is None

    def test_texto_mas_largo_que_la_columna_se_rechaza_no_se_recorta(self) -> None:
        r = normalize_cross_value("customer", "postal_code", "B1642ABC12345")
        assert not r.valid

    def test_codigo_postal_numerico_sin_decimal_de_excel(self) -> None:
        assert normalize_cross_value("customer", "postal_code", 1642.0).value == "1642"

    def test_campo_fuera_de_la_lista_es_invalido(self) -> None:
        assert not normalize_cross_value("customer", "name", "Juan").valid
        assert not normalize_cross_value("supplier", "address", "Mitre 10").valid

    def test_vacio_de_la_ficha(self) -> None:
        assert is_ficha_value_blank(None)
        assert is_ficha_value_blank("  ")
        assert not is_ficha_value_blank("Pérez")
