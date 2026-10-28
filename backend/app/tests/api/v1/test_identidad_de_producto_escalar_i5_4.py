"""I5-4 — dos columnas al código de identidad del producto: el confirm pregunta.

Un catálogo «Productos | Código proveedor | Código | Precio»: las dos columnas
de código llegaban al SKU y `_resolve_target_cols` se quedaba con la primera, así
que la identidad del producto pasaba a ser el código del proveedor. El motor
fusiona por SKU: dos artículos de proveedores distintos con el mismo código
quedaban como el mismo, y otro archivo con las columnas al revés reconocía otra
identidad. Ni la UI ni el confirm avisaban. Ahora `sku` y `barcode` son de valor
único: 422 antes del lease y nada escrito.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import SINGLE_VALUE_FIELDS
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CTX = "sheet:catalogo"
_HEADERS = ["Productos", "Código proveedor", "Código", "EAN", "Código de barras", "Precio venta"]


def _summary() -> dict[str, Any]:
    rows = [
        {
            "Productos": "Vela aromática 200g",
            "Código proveedor": "PROV-77",
            "Código": "VEL-200",
            "EAN": "7790000000017",
            "Código de barras": "7790000000024",
            "Precio venta": "2100",
            "__context__": _CTX,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        # Multihoja: es el único camino que consume los mapeos POR CONTEXTO (ver
        # `test_ingestion_scalar_collision`).
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def catalogo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="catalogo.xlsx",
        s3_key="uploads/test/uuid/catalogo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _m(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "product",
    }


async def _confirmar(
    client: AsyncClient, headers: dict[str, Any], file: UploadedFile, mapeos: list[dict[str, Any]]
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{file.id}/confirm",
        json={
            "column_mappings": mapeos,
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
        },
        headers=headers,
    )


class TestLaIdentidadDelProductoNoSeEligePorOrdenDeColumna:
    @pytest.mark.parametrize(
        ("columnas", "target", "rotulo"),
        [
            (("Código proveedor", "Código"), "sku", "Código (SKU)"),
            (("EAN", "Código de barras"), "barcode", "Código de barras"),
        ],
    )
    async def test_dos_columnas_al_mismo_codigo_rechazan_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalogo: UploadedFile,
        db_session: AsyncSession,
        columnas: tuple[str, str],
        target: str,
        rotulo: str,
    ) -> None:
        mapeos = [_m("Productos", "name"), _m("Precio venta", "sale_price_ars")]
        mapeos += [_m(c, target) for c in columnas]
        resp = await _confirmar(client, auth_headers, catalogo, mapeos)

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert rotulo in detail
        for c in columnas:
            assert c in detail
        # Nada escrito: el rechazo es previo a cualquier escritura.
        assert (await db_session.execute(select(Product))).scalars().all() == []

    async def test_eligiendo_una_y_mandando_la_otra_a_campo_propio_importa(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalogo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """La salida: el código propio al SKU, el del proveedor no se pierde."""
        mapeos = [
            _m("Productos", "name"),
            _m("Precio venta", "sale_price_ars"),
            _m("Código", "sku"),
            _m("Código proveedor", "custom_field:codigo_proveedor"),
            _m("EAN", "barcode"),
            _m("Código de barras", "ignore"),
        ]
        resp = await _confirmar(client, auth_headers, catalogo, mapeos)

        assert resp.status_code == 200, resp.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.sku == "VEL-200"
        assert prod.barcode == "7790000000017"


@pytest.mark.parametrize("entity", ["sale", "expense", "product"])
def test_sku_y_barcode_son_de_valor_unico_en_las_tres_hojas(entity: str) -> None:
    """La UI (`scalarCollisions`) y el confirm leen la MISMA lista."""
    assert {"sku", "barcode"} <= SINGLE_VALUE_FIELDS[entity]
