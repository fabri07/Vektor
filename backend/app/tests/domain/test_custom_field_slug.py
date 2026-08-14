"""F-A — el encabezado de una columna convertido en clave de campo propio.

La clave se PERSISTE (``tenant_custom_field_definitions.field_key``), así que la
forma importa: hasta esta fase la generaba el frontend con
``.trim().toLowerCase().replace(/\\s+/g, "_")`` y la ingesta no validaba nada,
o sea que podía escribir claves que ``POST /fields`` rechaza.
"""

from __future__ import annotations

import re

import pytest

from app.domain.header_keys import custom_field_slug
from app.schemas.fields import CreateCustomFieldRequest

#: El patrón que el schema de `/fields` exige. Se lee del propio schema y no se
#: copia: si mañana lo cambian, este test tiene que enterarse, no seguir midiendo
#: contra una copia vieja.
PATRON_DE_FIELDS = CreateCustomFieldRequest.model_fields["field_key"].metadata


class TestFormaDeLaClave:
    @pytest.mark.parametrize(
        ("header", "esperado"),
        [
            ("Observaciones", "observaciones"),
            # Acentos y eñes: el frontend los dejaba pasar tal cual.
            ("Año Fiscal", "ano_fiscal"),
            ("Código", "codigo"),
            # Puntuación → separador, sin dejar el punto adentro. `_normalize_col`
            # devolvía "p._venta" acá, que es la razón de no reusarla.
            ("P. Venta", "p_venta"),
            ("Obs.", "obs"),
            # Separadores repetidos colapsan en uno solo.
            ("Precio   ---   Final", "precio_final"),
            ("  Notas  ", "notas"),
        ],
    )
    def test_normaliza(self, header: str, esperado: str) -> None:
        assert custom_field_slug(header) == esperado

    def test_no_arranca_con_digito(self) -> None:
        # Un identificador que empieza con número no es válido en ningún lado.
        assert custom_field_slug("2024 Total") == "c_2024_total"

    def test_una_sola_letra_tambien_se_prefija(self) -> None:
        # `/fields` pide min_length=2: sin esto, una columna "A" produce una
        # clave que la propia API del producto rechazaría.
        assert custom_field_slug("A") == "c_a"

    def test_trunca_sin_dejar_separador_colgando(self) -> None:
        slug = custom_field_slug("a" * 100)
        assert slug is not None
        assert len(slug) == 72
        # El corte se hace ANTES del tope de la columna (80) para que la
        # desambiguación (`_2`, `_10`) entre sin volver a pasarse.
        assert len(slug) < 80

    def test_el_corte_no_deja_guion_bajo_final(self) -> None:
        # 71 caracteres + separador + más texto: cortar en 72 caería justo en el
        # `_`, y una clave terminada en separador es basura visible en el ERD.
        slug = custom_field_slug("a" * 71 + " final")
        assert slug is not None
        assert not slug.endswith("_")


class TestSinNadaUsable:
    @pytest.mark.parametrize("header", ["", "   ", "...", "---", "¿?", None])
    def test_devuelve_none_no_cadena_vacia(self, header: str | None) -> None:
        """`""` sería una clave, y dos columnas sin nombre colisionarían en ella.

        `None` obliga al caller a decidir qué hacer con una columna que no tiene
        cómo llamarse, en vez de dejarlo pasar.
        """
        assert custom_field_slug(header) is None


class TestCompatibilidadConLaApiDeCampos:
    """Lo que produce la ingesta tiene que ser aceptable para `POST /fields`.

    Las dos puntas escribían en la MISMA columna con reglas distintas: el schema
    validaba `^[a-z][a-z0-9_]*$` y la ingesta no validaba nada.
    """

    @pytest.mark.parametrize(
        "header",
        [
            "Observaciones",
            "Año Fiscal",
            "P. Venta",
            "2024 Total",
            "A",
            "Nº Documento",
            "Precio   ---   Final",
            "a" * 100,
        ],
    )
    def test_toda_clave_generada_pasa_el_schema(self, header: str) -> None:
        slug = custom_field_slug(header)
        assert slug is not None
        # Construir el schema real es la prueba: replicar el regex acá sería otra
        # copia de la regla, que es el problema que la fase viene a cerrar.
        creado = CreateCustomFieldRequest(
            field_key=slug, label="x", entity_type="sale", data_type="text"
        )
        assert creado.field_key == slug

    def test_el_patron_del_schema_sigue_siendo_el_que_este_test_asume(self) -> None:
        # Si `/fields` relaja o endurece su patrón, esta suite tiene que fallar en
        # vez de seguir afirmando una compatibilidad que ya no se verificó.
        patrones = [getattr(m, "pattern", None) for m in PATRON_DE_FIELDS]
        assert "^[a-z][a-z0-9_]*$" in patrones

    def test_ninguna_clave_tiene_caracteres_fuera_del_alfabeto(self) -> None:
        for header in ["Ñandú", "café/té", "50% dto", "a.b-c d"]:
            slug = custom_field_slug(header)
            assert slug is not None
            assert re.fullmatch(r"[a-z][a-z0-9_]*", slug), (header, slug)
