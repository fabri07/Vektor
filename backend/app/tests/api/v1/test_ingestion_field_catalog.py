"""El catálogo de campos tiene UNA sola fuente.

El frontend mantenía una copia manual de ``CANONICAL_FIELDS`` comentada "mantener
en sync" y divergió: a ``expense`` le faltaban ``payment_method`` e
``is_recurring``. Como el ``<select>`` del panel solo renderiza opciones de esa
copia, cuando el backend sugería ``payment_method`` ninguna ``<option>``
matcheaba, el DOM caía a la primera y la pantalla mostraba "Sin mapear" mientras
el estado enviaba ``payment_method`` (incidente ASTERIA, captura 16:50:50).

Este endpoint es lo que el frontend consume ahora, en vez de tener su propia lista.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    REQUIRED_FIELDS,
    SINGLE_VALUE_FIELDS,
)


@pytest.mark.asyncio
class TestFieldCatalog:
    async def test_devuelve_todas_las_entidades_con_requeridos_y_campos(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        assert response.status_code == 200
        catalog = response.json()

        assert set(catalog) == set(CANONICAL_FIELDS)
        for entity, entry in catalog.items():
            assert entry["required"] == REQUIRED_FIELDS.get(entity, [])
            valores = {f["value"] for f in entry["fields"]}
            assert valores == set(CANONICAL_FIELDS[entity])
            # Todo campo trae etiqueta en castellano para mostrar en el select.
            assert all(f["label"] for f in entry["fields"])

    async def test_expense_incluye_los_campos_que_el_frontend_habia_perdido(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """La divergencia concreta que rompió el mapeo de `forma_pago`."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        valores = {f["value"] for f in response.json()["expense"]["fields"]}
        assert "payment_method" in valores
        assert "is_recurring" in valores

    async def test_los_tres_precios_de_producto_estan_disponibles(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        fields = {f["value"]: f for f in response.json()["product"]["fields"]}
        assert fields["unit_cost_ars"]["label"] == "Costo unitario"
        assert fields["list_price_ars"]["label"] == "Precio de lista (sugerido)"
        assert fields["sale_price_ars"]["label"] == "Precio de venta"

    async def test_marca_los_campos_de_valor_unico(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """`single_value` es lo que le permite a la UI bloquear la colisión con la
        MISMA regla que aplica el confirm, en vez de tener su propia lista."""
        response = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
        catalog = response.json()

        for entity, entry in catalog.items():
            escalares = {f["value"] for f in entry["fields"] if f["single_value"]}
            assert escalares == set(SINGLE_VALUE_FIELDS.get(entity, frozenset()))

        producto = {f["value"]: f["single_value"] for f in catalog["product"]["fields"]}
        assert producto["sale_price_ars"] is True
        assert producto["list_price_ars"] is True
        assert producto["unit_cost_ars"] is True
        # Varias columnas pueden alimentar una descripción: no bloquea.
        assert producto["description"] is False

    async def test_requiere_autenticacion(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/ingestion/field-catalog")
        assert response.status_code in (401, 403)
