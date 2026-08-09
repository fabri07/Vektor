"""F-M — el reconocedor cableado: qué corta la cadena y qué la deja seguir.

`suggest_mappings` era un `if/elif/else` de tres capas y un tercer estado no
tenía rama. Las dos salidas obvias son peores que el bug que la fase arregla:

- si el ambiguo entra por la rama de heurística, llega al frontend como `mapped`
  con un target elegido a dedo;
- si cae al `else`, **lo resuelve fuzzy** — la capa menos informada, que compara
  contra los keywords crudos.

Lo segundo no es teórico y este archivo lo prueba con el caso que da nombre a la
fase: `Envío unitario` es `sin_evidencia` para el reconocedor, y fuzzy lo
resolvería a `unit_price` con ratio 0,76. Sin el corte, la capa de abajo deshace
la corrección y el flete vuelve a entrar como precio de compra.

La regla, entonces: una lectura que ENTENDIÓ el encabezado y aun así no puede
elegir corta la cadena. Sólo la que no reconoció nada sigue al camino de siempre,
para que fuzzy y el LLM conserven su función de rescate.
"""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services import llm_column_mapper
from app.application.services.column_mapping_service import (
    ColumnMappingService,
    _fuzzy_match,
    _normalize_col,
    heuristic_target,
)


def _svc(history: list[Any] | None = None) -> ColumnMappingService:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = history or []
    db.execute = AsyncMock(return_value=result)
    return ColumnMappingService(db)


def _fila(headers: list[str]) -> list[dict[str, Any]]:
    return [{h: "1500" for h in headers}]


async def _sugerir(entity: str, headers: list[str], history: list[Any] | None = None):  # noqa: ANN202
    svc = _svc(history)
    sugerencias = await svc.suggest_mappings(uuid.uuid4(), entity, headers, _fila(headers))
    return {s["source_column"]: s for s in sugerencias}


class TestLaCapaDeAbajoNoDeshaceLaCorreccion:
    def test_fuzzy_resolveria_envio_unitario_a_un_precio(self) -> None:
        """La premisa del corte, medida. Si esto deja de ser cierto, el test de
        abajo dejaría de probar algo y hay que revisarlo, no borrarlo."""
        target, ratio = _fuzzy_match(_normalize_col("Envío unitario"), "expense")
        assert target == "unit_price"
        assert ratio >= 0.70

    @pytest.mark.asyncio
    async def test_pero_no_llega_a_correr(self) -> None:
        s = (await _sugerir("expense", ["Envío unitario"]))["Envío unitario"]
        assert s["target_field"] is None
        assert s["source"] != "fuzzy"
        assert s["status"] == "unmapped"


class TestUnAmbiguoNoSeResuelveSolo:
    @pytest.mark.asyncio
    async def test_no_llega_como_mapped_con_un_target_elegido_a_dedo(self) -> None:
        s = (await _sugerir("expense", ["Precio con IVA"]))["Precio con IVA"]
        assert s["status"] == "ambiguo"
        assert s["target_field"] is None
        assert s["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_viaja_con_los_candidatos_y_el_porque(self) -> None:
        """`ambiguo` es un estado propio, no un `unmapped` con suerte: la pantalla
        recibe los dos candidatos y la razón, sin tener que reconstruir ninguno."""
        s = (await _sugerir("expense", ["Precio con IVA"]))["Precio con IVA"]
        assert set(s["options"]) == {"amount", "unit_price"}
        assert s["duda"]

    @pytest.mark.asyncio
    async def test_un_concepto_sin_campo_explica_pero_no_ofrece_candidatos(self) -> None:
        """«Entiendo qué es y no tengo dónde ponerlo» no es una ambigüedad: no hay
        entre qué elegir, así que queda `unmapped` — pero con la explicación."""
        s = (await _sugerir("expense", ["Envío unitario"]))["Envío unitario"]
        assert s["status"] == "unmapped"
        assert s["options"] == []
        assert s["duda"]

    @pytest.mark.asyncio
    async def test_lo_desconocido_no_inventa_una_explicacion(self) -> None:
        s = (await _sugerir("sale", ["ColRara99"]))["ColRara99"]
        assert s["duda"] is None
        assert s["options"] == []

    @pytest.mark.asyncio
    async def test_tampoco_lo_desempata_el_llm(self) -> None:
        """Baja confianza y ambigüedad no son lo mismo: la primera dice «no sé» y
        el LLM puede ayudar; la segunda dice «entendí, y siguen siendo dos». Una
        respuesta del LLM no demuestra la intención del usuario."""
        vistas: list[str] = []

        async def _fake_llm(
            entity_type: str, columns: list[dict[str, Any]], valid_fields: dict[str, str]
        ) -> dict[str, dict[str, Any]]:
            vistas.extend(c["header"] for c in columns)
            return {}

        with unittest.mock.patch.object(
            llm_column_mapper, "suggest_with_llm", side_effect=_fake_llm
        ):
            await _sugerir("expense", ["Precio con IVA", "ColRara99"])

        assert "Precio con IVA" not in vistas
        assert "ColRara99" in vistas, "el rescate del desconocido no se puede perder"


class TestElInvarianteDelContrato:
    """«`mapped` ⇒ sin duda», sostenido en el único lugar que puede romperlo.

    Ojo con lo que este test es: el caller de hoy **no puede** producir el estado
    que arma acá, porque las columnas con duda viajan en `skip` y nunca llegan al
    LLM. Se prueba directo contra `_apply_llm_fallback` a propósito — el guard
    existe para que el invariante no dependa de ese hecho, y sacar el `skip` en el
    futuro no debería poder dejar una columna resuelta explicando por qué no se
    podía resolver.
    """

    @pytest.mark.asyncio
    async def test_resolver_una_columna_le_saca_la_duda(self) -> None:
        sugerencia = {
            "source_column": "Precio con IVA",
            "normalized_column": "precio_con_iva",
            "sample_values": ["1500"],
            "target_field": None,
            "confidence": 0.0,
            "source": "none",
            "status": "ambiguo",
            "options": ["amount", "unit_price"],
            "duda": "¿es el precio de cada unidad, o el total de la línea?",
        }

        async def _fake_llm(
            entity_type: str, columns: list[dict[str, Any]], valid_fields: dict[str, str]
        ) -> dict[str, dict[str, Any]]:
            return {"Precio con IVA": {"target_field": "amount", "confidence": 0.9}}

        with unittest.mock.patch.object(
            llm_column_mapper, "suggest_with_llm", side_effect=_fake_llm
        ):
            # Sin `skip`: es el escenario que el guard tiene que sostener.
            await _svc()._apply_llm_fallback("expense", [sugerencia])

        assert sugerencia["status"] == "mapped"
        assert sugerencia["duda"] is None
        assert sugerencia["options"] == []


class TestLoQueNoSeReconocioSigueSuCamino:
    @pytest.mark.asyncio
    async def test_un_header_desconocido_sigue_llegando_al_llm(self) -> None:
        recibidas: list[str] = []

        async def _fake_llm(
            entity_type: str, columns: list[dict[str, Any]], valid_fields: dict[str, str]
        ) -> dict[str, dict[str, Any]]:
            recibidas.extend(c["header"] for c in columns)
            return {"ColRara99": {"target_field": "amount", "confidence": 0.88}}

        with unittest.mock.patch.object(
            llm_column_mapper, "suggest_with_llm", side_effect=_fake_llm
        ):
            s = (await _sugerir("sale", ["ColRara99"]))["ColRara99"]

        assert recibidas == ["ColRara99"]
        assert s["source"] == "llm"
        assert s["target_field"] == "amount"

    @pytest.mark.asyncio
    async def test_un_header_claro_sigue_resolviendo(self) -> None:
        s = (await _sugerir("sale", ["Fecha"]))["Fecha"]
        assert s["source"] == "heuristic"
        assert s["target_field"] == "transaction_date"
        assert s["status"] == "mapped"


class TestElHistorialDelTenantSigueMandando:
    @pytest.mark.asyncio
    async def test_una_decision_previa_del_usuario_desempata_el_ambiguo(self) -> None:
        """La única cosa que SÍ demuestra la intención: que ese tenant ya lo
        resolvió antes. El historial es la capa 1 y le gana al reconocedor."""
        rec = MagicMock()
        rec.source_column = _normalize_col("Precio con IVA")
        rec.target_field = "unit_price"
        rec.confirmed_count = 4

        s = (await _sugerir("expense", ["Precio con IVA"], history=[rec]))["Precio con IVA"]
        assert s["source"] == "tenant_history"
        assert s["target_field"] == "unit_price"
        assert s["status"] == "mapped"


class TestElWrapperSincronico:
    """Remitos y proveedores no tienen pantalla donde desambiguar: reciben un
    campo o nada, como hasta ahora."""

    def test_devuelve_el_target_cuando_es_inequivoco(self) -> None:
        assert heuristic_target(_normalize_col("Precio de compra"), "product") == "unit_cost_ars"

    def test_un_ambiguo_es_lo_mismo_que_un_desconocido(self) -> None:
        assert heuristic_target(_normalize_col("Precio con IVA"), "expense") is None

    def test_y_un_concepto_sin_campo_tampoco_inventa(self) -> None:
        assert heuristic_target(_normalize_col("Envío unitario"), "expense") is None
