"""Dos columnas al mismo campo propio: el confirm rechaza en vez de elegir.

Hermano de ``test_ingestion_scalar_collision.py``. Aquel cubre los campos
canónicos escalares (los precios del incidente ASTERIA); éste cubre la misma
forma del problema en los campos propios, que hasta F-0 no tenía ni el guard
silencioso de first-wins ni el rechazo explícito.

Por qué importa ahora y no antes: mientras los campos propios los escribía el
usuario a mano y de a uno, dos columnas colapsando al mismo nombre era raro. En
cuanto el mapeo propone un campo propio por cada columna que no reconoce, un
archivo con "Observaciones" y "Obs." genera dos columnas al mismo destino sin
que nadie lo haya pedido.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CONTEXT = "sheet:catalogo"
_HEADERS = ["Productos", "Precio de venta final", "Observaciones", "Obs."]


def _catalog_summary() -> dict[str, Any]:
    rows = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de venta final": "2100",
            "Observaciones": "importada",
            "Obs.": "fragil",
            "__context__": _CONTEXT,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": _CONTEXT,
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
async def catalog_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
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
        parsed_summary_json=_catalog_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _mapping(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CONTEXT,
        "entity_type": "product",
    }


_BASE = [
    _mapping("Productos", "name"),
    _mapping("Precio de venta final", "sale_price_ars"),
]


@pytest.mark.asyncio
class TestColisionDeCampoPropio:
    async def test_dos_columnas_al_mismo_campo_propio_rechaza_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        body = {
            "column_mappings": [
                *_BASE,
                _mapping("Observaciones", "custom_field:obs"),
                _mapping("Obs.", "custom_field:obs"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CONTEXT: True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        # Nombra las dos columnas en conflicto: sin eso el usuario no sabe cuál tocar.
        assert "Observaciones" in detail
        assert "Obs." in detail

        # Y nada se importó: el rechazo es previo a cualquier escritura.
        productos = (await db_session.execute(select(Product))).scalars().all()
        assert productos == []

    async def test_campos_propios_distintos_no_colisionan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """La salida disponible: un nombre para cada columna."""
        body = {
            "column_mappings": [
                *_BASE,
                _mapping("Observaciones", "custom_field:observaciones"),
                _mapping("Obs.", "custom_field:obs"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CONTEXT: True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.custom_fields.get("observaciones") == "importada"
        assert prod.custom_fields.get("obs") == "fragil"

    async def test_columna_dropeada_por_riesgo_no_cuenta_como_colision(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        """Misma regla que en los escalares: se evalúa el mapeo EFECTIVO.

        Si el usuario ya decidió dropear una de las dos, después del drop queda
        una sola columna: bloquear sería exigirle deshacer una decisión tomada.
        """
        body = {
            "column_mappings": [
                *_BASE,
                _mapping("Observaciones", "custom_field:obs"),
                _mapping("Obs.", "custom_field:obs"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CONTEXT: True},
            "column_risk_decisions": [
                {
                    "context_id": _CONTEXT,
                    "source_column": "Obs.",
                    "target_field": "custom_field:obs",
                    "action": "drop_column",
                }
            ],
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text

    async def test_una_columna_ignorada_no_compite_por_el_campo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                *_BASE,
                _mapping("Observaciones", "custom_field:obs"),
                _mapping("Obs.", "ignore"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CONTEXT: True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
