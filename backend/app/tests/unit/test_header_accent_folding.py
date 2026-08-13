"""La variabilidad de acentos y ñ no puede cambiar cómo se lee un archivo.

Un mismo negocio escribe la misma columna de las dos maneras según quién cargó
la planilla: "Descripción" y "Descripcion", "Año" y "Ano", "Mercadería" y
"Mercaderia". Hasta esta fase el matching de encabezados era sensible al acento
y cada keyword tenía que declarar sus dos formas a mano — lo que funciona sólo
mientras alguien se acuerde de escribir la segunda.

Se cubren los cuatro ejes por los que la misma columna llega distinta:

1. tilde sí / tilde no          — "Descripción" vs "Descripcion"
2. ñ / n                        — "Año" vs "Ano"
3. NFC / NFD                    — la "ñ" como un carácter o como n + tilde
                                  combinante: iguales en pantalla, distintas en
                                  bytes (Excel y macOS exportan las dos)
4. mojibake                     — "DescripciÃ³n": el archivo ya venía roto y sus
                                  bytes son UTF-8 válido, así que ninguna
                                  escalera de decodificación lo arregla
"""

from __future__ import annotations

import unicodedata
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.column_mapping_service import (
    ColumnMappingService,
    _heuristic_match,
    _normalize_col,
)
from app.application.services.file_parsing import analyze_headers, classify_line
from app.domain.header_keys import fold_header, match_key
from app.domain.text_norm import repair_mojibake

# "Año" con la ñ descompuesta (NFD): 'n' + U+0303. Se ve idéntica a "Año".
ANIO_NFD = unicodedata.normalize("NFD", "Año")


class TestFoldHeader:
    """El plegado en sí: los cuatro ejes contra la misma forma canónica."""

    @pytest.mark.parametrize(
        ("crudo", "esperado"),
        [
            ("Descripción", "descripcion"),
            ("Descripcion", "descripcion"),
            ("DESCRIPCIÓN", "descripcion"),
            ("Año", "ano"),
            ("Ano", "ano"),
            (ANIO_NFD, "ano"),
            ("Cumpleaños", "cumpleanos"),
            ("Mercadería", "mercaderia"),
            ("Razón Social", "razon_social"),
            ("Teléfono", "telefono"),
            ("Método de Pago", "metodo_de_pago"),
            # Mojibake: el header doble-codificado pliega igual que el sano.
            ("DescripciÃ³n", "descripcion"),
            ("AÃ±o", "ano"),
            ("MercaderÃ\xada", "mercaderia"),
        ],
    )
    def test_variantes_pliegan_a_la_misma_clave(self, crudo: str, esperado: str) -> None:
        assert fold_header(crudo) == esperado

    def test_nfc_y_nfd_son_indistinguibles_en_pantalla_y_deben_serlo_al_leer(self) -> None:
        """El caso que no se ve revisando a ojo: dos bytes distintos, un carácter."""
        nfc = "Año"
        assert nfc != ANIO_NFD, "el fixture perdió sentido: deberían diferir en bytes"
        assert fold_header(nfc) == fold_header(ANIO_NFD)

    def test_los_acentos_del_castellano_no_cambian_la_longitud(self) -> None:
        """`_heuristic_match` desempata por longitud del keyword: si plegar
        acortara una palabra, movería desempates que nadie pidió mover."""
        for con, sin in [("comisión", "comision"), ("año", "ano"), ("logística", "logistica")]:
            assert len(fold_header(con)) == len(sin)


class TestMatchKey:
    """La clave de matching pliega acentos ADEMÁS de colapsar preposiciones."""

    def test_acento_y_preposicion_a_la_vez(self) -> None:
        assert match_key(_normalize_col("Método de Pago")) == "metodo_pago"
        assert match_key(_normalize_col("Metodo de Pago")) == "metodo_pago"
        assert match_key(_normalize_col("Método Pago")) == "metodo_pago"

    def test_header_solo_stopwords_no_devuelve_cadena_vacia(self) -> None:
        assert match_key("de") == "de"


class TestHeuristicaDeMapeo:
    """El motor de mapeo resuelve al mismo campo escriba o no el acento."""

    @pytest.mark.parametrize(
        ("entidad", "con_acento", "sin_acento"),
        [
            ("product", "Descripción", "Descripcion"),
            ("expense", "Categoría", "Categoria"),
            ("customer", "Teléfono", "Telefono"),
            ("customer", "Razón Social", "Razon Social"),
        ],
    )
    def test_misma_columna_mismo_campo(
        self, entidad: str, con_acento: str, sin_acento: str
    ) -> None:
        con = _heuristic_match(_normalize_col(con_acento), entidad)
        sin = _heuristic_match(_normalize_col(sin_acento), entidad)
        assert con == sin, f"{con_acento} → {con} pero {sin_acento} → {sin}"
        assert con is not None, f"ninguna de las dos formas de {con_acento} se reconoció"

    async def test_encabezado_mojibakeado_se_reconoce(self) -> None:
        """El header roto no se parece a ninguna palabra del castellano: sin
        reparar, la columna cae a 'sin mapear' aunque sea la de siempre.

        Va por `suggest_mappings` y no por `_heuristic_match` porque la
        reparación tiene que ocurrir ANTES del lowercase de `_normalize_col`:
        pasarle a la heurística un normalizado ya en minúsculas llega tarde.
        """
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        sugerencias = await ColumnMappingService(db).suggest_mappings(
            uuid.uuid4(), "product", ["DescripciÃ³n"], [{"DescripciÃ³n": "Gaseosa"}]
        )

        assert sugerencias[0]["target_field"] == "description"
        # El nombre crudo vuelve intacto: es la clave con la que se lee la celda.
        assert sugerencias[0]["source_column"] == "DescripciÃ³n"


class TestClasificacionDeHoja:
    """La inferencia de tipo de archivo tampoco puede depender del acento."""

    @pytest.mark.parametrize(
        ("señal", "esperado"),
        [
            ("Facturación", "ventas"),
            ("Categoría", "gastos"),
            ("Comisión", "gastos"),
        ],
    )
    def test_mismo_tipo_inferido(self, señal: str, esperado: str) -> None:
        """La columna acentuada es la ÚNICA señal que decide el tipo: el resto de
        los encabezados es neutro a propósito, si no el archivo se clasificaría
        igual con o sin el plegado y el test no probaría nada (sin la señal, el
        tipo cae a 'general')."""
        sin_tilde = unicodedata.normalize("NFKD", señal).encode("ascii", "ignore").decode()

        con = analyze_headers(["Fecha", señal, "Monto"])
        sin = analyze_headers(["Fecha", sin_tilde, "Monto"])

        assert con == sin
        assert con["inferred_type"] == esperado
        assert analyze_headers(["Fecha", "Monto"])["inferred_type"] == "general"

    def test_classify_line_ignora_el_acento(self) -> None:
        """Los *_CTX se declaran sin tilde: sin plegar la línea, "mercadería" no
        matchea "mercaderia" y la línea cae a 'desconocido'."""
        assert classify_line("compre mercadería por 5000") == "stock"
        assert classify_line("compre mercaderia por 5000") == "stock"


class TestHistorialDelTenant:
    """El alias aprendido se persiste con la tilde que trajo el archivo que lo
    enseñó; encontrarlo no puede depender de que el siguiente la escriba igual."""

    @staticmethod
    def _mapping(source_column: str, target_field: str, *, count: int = 5) -> MagicMock:
        m = MagicMock()
        m.source_column = source_column
        m.target_field = target_field
        m.confirmed_count = count
        m.last_seen_at = 0
        return m

    @staticmethod
    def _svc(rows: list[MagicMock]) -> ColumnMappingService:
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db.execute = AsyncMock(return_value=result)
        return ColumnMappingService(db)

    async def test_alias_con_tilde_resuelve_un_header_sin_tilde(self) -> None:
        svc = self._svc([self._mapping("descripción", "description")])

        sugerencias = await svc.suggest_mappings(
            uuid.uuid4(), "product", ["Descripcion"], [{"Descripcion": "Gaseosa 500ml"}]
        )

        assert sugerencias[0]["target_field"] == "description"
        assert sugerencias[0]["source"] == "tenant_history"

    async def test_alias_sin_tilde_resuelve_un_header_con_tilde(self) -> None:
        """La dirección inversa: lo aprendido sin tilde sirve para el archivo que
        sí la escribe."""
        svc = self._svc([self._mapping("observaciones", "description")])

        sugerencias = await svc.suggest_mappings(
            uuid.uuid4(), "product", ["Observaciónes"], [{"Observaciónes": "x"}]
        )

        assert sugerencias[0]["source"] == "tenant_history"
        assert sugerencias[0]["target_field"] == "description"

    async def test_la_coincidencia_exacta_le_gana_a_la_plegada(self) -> None:
        """Si el tenant tiene el alias TAL CUAL vino el header, ese manda: plegar
        es una red de contención, no una reinterpretación de lo que ya confirmó."""
        svc = self._svc(
            [
                self._mapping("descripcion", "description", count=1),
                self._mapping("descripción", "name", count=99),
            ]
        )

        sugerencias = await svc.suggest_mappings(
            uuid.uuid4(), "product", ["Descripcion"], [{"Descripcion": "x"}]
        )

        assert sugerencias[0]["target_field"] == "description"

    async def test_entre_dos_alias_que_pliegan_igual_gana_el_mas_confirmado(self) -> None:
        """Sin criterio explícito, el resultado dependería del orden en que la
        base devolvió las filas.

        El header entra en NFD, así que no coincide EXACTO con ninguno de los dos
        alias guardados (los dos están en NFC) y la resolución tiene que pasar sí
        o sí por el índice plegado.
        """
        svc = self._svc(
            [
                self._mapping("descripción", "name", count=1),
                self._mapping("descripcion", "description", count=40),
            ]
        )
        header_nfd = unicodedata.normalize("NFD", "Descripción")
        assert header_nfd != "Descripción"

        sugerencias = await svc.suggest_mappings(
            uuid.uuid4(), "product", [header_nfd], [{header_nfd: "x"}]
        )

        assert sugerencias[0]["target_field"] == "description"

    async def test_el_encabezado_crudo_se_devuelve_sin_tocar(self) -> None:
        """Se pliega para COMPARAR. El nombre de la columna que vuelve es el del
        archivo — es con ese que el importador lee la celda de cada fila."""
        svc = self._svc([self._mapping("descripción", "description")])

        sugerencias = await svc.suggest_mappings(
            uuid.uuid4(), "product", ["DESCRIPCION "], [{"DESCRIPCION ": "x"}]
        )

        assert sugerencias[0]["source_column"] == "DESCRIPCION "


class TestRepairMojibake:
    def test_repara_el_doble_codificado(self) -> None:
        roto = "Descripción;Año;Mercadería".encode().decode("cp1252")
        assert repair_mojibake(roto) == "Descripción;Año;Mercadería"

    @pytest.mark.parametrize(
        "sano", ["Descripción", "Año", "Mercadería", "Precio", "Razón Social", ""]
    )
    def test_sobre_texto_sano_es_identidad(self, sano: str) -> None:
        assert repair_mojibake(sano) == sano
