"""I5-3 — un calificador que cambia el significado no se ignora.

El reconocedor proponía «unico» (y por lo tanto mapeado solo) un destino que
mueve plata o stock aunque el encabezado dijera otra cosa: el número de orden
como MONTO, el precio de cada unidad como el total de la línea, una cantidad
vendida o un stock mínimo como stock de apertura, y el código del proveedor como
la identidad del producto.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.column_mapping_service import (
    ColumnMappingService,
    _normalize_col,
    read_header,
)


def _leer(header: str, entity: str):  # noqa: ANN202 — el tipo real es HeaderReading
    return read_header(_normalize_col(header), entity)


async def _sugerir(entity: str, headers: list[str]) -> dict[str, dict[str, Any]]:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    svc = ColumnMappingService(db)
    filas = [{h: "1500" for h in headers}]
    sugerencias = await svc.suggest_mappings(uuid.uuid4(), entity, headers, filas)
    return {s["source_column"]: s for s in sugerencias}


class TestElNumeroDeOrdenNoEsUnMonto:
    @pytest.mark.parametrize(
        ("entity", "header"),
        [
            ("sale", "N° Venta"),
            ("sale", "Nro de venta"),
            ("sale", "Número de venta"),
            ("sale", "Num venta"),
            ("sale", "Número de pago"),
            ("expense", "Nro compra"),
            ("expense", "Número de pago"),
            ("expense", "Nro de pago"),
            ("product", "N° compra"),
        ],
    )
    def test_no_se_propone_como_monto(self, entity: str, header: str) -> None:
        r = _leer(header, entity)
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        assert r.concept == "monto"
        assert "identifica la operación" in r.duda

    @pytest.mark.parametrize(
        ("entity", "header"),
        [("sale", "Monto"), ("sale", "Total venta"), ("expense", "Importe"),
         ("sale", "Monto no gravado")],
    )
    def test_un_monto_de_verdad_sigue_siendo_monto(self, entity: str, header: str) -> None:
        assert _leer(header, entity).target == "amount"


class TestElPrecioDeCadaUnidadNoEsElTotal:
    @pytest.mark.parametrize("entity", ["sale", "expense"])
    @pytest.mark.parametrize("header", ["Valor unitario", "Monto unitario", "Importe unitario"])
    def test_va_al_precio_unitario(self, entity: str, header: str) -> None:
        r = _leer(header, entity)
        assert r.outcome == "unico"
        assert r.target == "unit_price"


class TestUnaCantidadQueNoEsElStockDeHoy:
    @pytest.mark.parametrize(
        "header",
        ["Cantidad vendida", "Unidades vendidas", "Cant. vendida", "Stock vendido"],
    )
    def test_lo_vendido_no_es_stock_de_apertura(self, header: str) -> None:
        r = _leer(header, "product")
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        assert "cantidad vendida" in r.duda

    @pytest.mark.parametrize(
        "header",
        ["Stock mínimo", "Stock min", "Cantidad mínima", "Stock máximo", "Stock mín."],
    )
    def test_un_umbral_no_es_stock_de_apertura(self, header: str) -> None:
        r = _leer(header, "product")
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        assert "umbral" in r.duda

    @pytest.mark.parametrize("header", ["Stock", "Cantidad", "Stock actual", "Existencias"])
    def test_el_stock_de_verdad_sigue_siendo_stock(self, header: str) -> None:
        assert _leer(header, "product").target == "stock_units"

    def test_en_una_venta_la_cantidad_vendida_es_la_cantidad(self) -> None:
        """El calificador no cambia lo que ya era correcto en otra hoja."""
        assert _leer("Cantidad vendida", "sale").target == "quantity"


class TestElCodigoDelProveedorNoEsLaIdentidadDelProducto:
    @pytest.mark.parametrize("header", ["Código proveedor", "Cod. proveedor", "SKU proveedor"])
    def test_en_un_catalogo_no_se_propone_como_sku(self, header: str) -> None:
        r = _leer(header, "product")
        assert r.outcome == "sin_evidencia"
        assert r.target is None
        assert "proveedor" in r.duda

    def test_en_una_compra_se_pregunta(self) -> None:
        """En una compra puede ser el código del artículo o el del proveedor."""
        r = _leer("Código proveedor", "expense")
        assert r.outcome == "ambiguo"
        assert set(r.options) == {"sku", "supplier_external_code"}

    @pytest.mark.parametrize("header", ["Código", "SKU", "Código producto"])
    def test_el_codigo_propio_sigue_siendo_sku(self, header: str) -> None:
        assert _leer(header, "product").target == "sku"


class TestLaDudaNoLaDeshaceLaCapaDeAbajo:
    """Una lectura con duda corta la cadena: ni fuzzy ni el LLM la reemplazan."""

    async def test_ventas_numero_de_orden_queda_sin_mapear(self) -> None:
        headers = ["Fecha", "N° Venta", "Producto", "Cantidad", "Valor unitario"]
        s = await _sugerir("sale", headers)
        assert s["N° Venta"]["target_field"] is None
        assert s["N° Venta"]["source"] != "fuzzy"
        assert s["N° Venta"]["duda"]
        assert s["Valor unitario"]["target_field"] == "unit_price"

    async def test_catalogo_umbral_vendido_y_codigo_de_proveedor_quedan_sin_mapear(self) -> None:
        headers = ["Producto", "Código proveedor", "Cantidad vendida", "Stock mínimo", "Stock"]
        s = await _sugerir("product", headers)
        for h in ("Código proveedor", "Cantidad vendida", "Stock mínimo"):
            assert s[h]["target_field"] is None, h
            assert s[h]["source"] != "fuzzy", h
        assert s["Stock"]["target_field"] == "stock_units"
