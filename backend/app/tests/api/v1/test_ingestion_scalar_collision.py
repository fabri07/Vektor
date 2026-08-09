"""Dos columnas al mismo campo escalar: el confirm rechaza en vez de elegir.

Incidente ASTERIA (2026-07-31): "Precio de compra", "Precio de lista" y "Precio
de venta final" apuntaban las tres a ``sale_price_ars``. El importador
(``_resolve_target_cols``) se quedaba con la PRIMERA del orden del Excel y
descartaba las otras dos sin avisar — el precio que terminaba guardado dependía
de cómo estaba ordenada la planilla.

Elegir un dato de negocio por un detalle de implementación es inventarlo, así que
el confirm ahora corta con 422 y le pide al usuario que decida. El rechazo ocurre
ANTES del lease: una request que va a rebotar nunca lo toma.
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

_HEADERS = ["Productos", "Precio de compra", "Precio de lista", "Precio de venta final"]


def _catalog_summary() -> dict[str, Any]:
    rows = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de compra": "1200",
            "Precio de lista": "2400",
            "Precio de venta final": "2100",
            "__context__": "sheet:precios y stock",
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        # Como el archivo real: 9 hojas → el importador entra por
        # `_insert_multisheet_data`, que es el único camino que consume los
        # mapeos POR CONTEXTO. Con `inferred_type: "stock"` y sin `multi_sheet`
        # se toma el camino flat y los context_mappings se ignoran.
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": "sheet:precios y stock",
                "label": "precios y stock",
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
        original_filename="ASTERIA_home_deco.xlsx",
        s3_key="uploads/test/uuid/asteria.xlsx",
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
        "context_id": "sheet:precios y stock",
        "entity_type": "product",
    }


@pytest.mark.asyncio
class TestColisionDeCampoEscalar:
    async def test_tres_columnas_al_mismo_precio_rechaza_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """El mapeo exacto que produjo el incidente."""
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "sale_price_ars"),
                _mapping("Precio de lista", "sale_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        # El mensaje nombra el campo en castellano y las tres columnas en conflicto.
        assert "Precio de venta" in detail
        for col in ("Precio de compra", "Precio de lista", "Precio de venta final"):
            assert col in detail

        # Y NADA se importó: el rechazo es previo a cualquier escritura.
        productos = (await db_session.execute(select(Product))).scalars().all()
        assert productos == []

    async def test_los_tres_precios_a_campos_distintos_no_colisionan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """La salida que el usuario tiene disponible: un campo para cada uno."""
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "unit_cost_ars"),
                _mapping("Precio de lista", "list_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.unit_cost_ars is not None
        assert prod.list_price_ars is not None
        assert prod.sale_price_ars is not None
        # Cada número quedó en su campo, ninguno pisó a otro.
        assert len({prod.unit_cost_ars, prod.list_price_ars, prod.sale_price_ars}) == 3

    async def test_columna_dropeada_por_riesgo_no_cuenta_como_colision(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        """La colisión se evalúa sobre el mapeo EFECTIVO, no sobre lo enviado.

        Dos columnas al mismo campo donde el usuario ya decidió dropear una no es
        una colisión: después del drop queda una sola. Bloquear acá sería exigirle
        deshacer una decisión que ya tomó.
        """
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "sale_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
            "column_risk_decisions": [
                {
                    "context_id": "sheet:precios y stock",
                    "source_column": "Precio de compra",
                    "target_field": "sale_price_ars",
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

    async def test_campo_no_escalar_admite_varias_columnas(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        """`description` no está en SINGLE_VALUE_FIELDS: dos columnas no bloquean.

        Bloquear todo sería tan malo como no bloquear nada — trabaría imports
        legítimos donde varias columnas alimentan un texto.
        """
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "description"),
                _mapping("Precio de lista", "description"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text


# Con fecha y monto: los requeridos se validan ANTES que la colisión escalar, así
# que una hoja incompleta rebotaría por el otro motivo y este test no probaría nada.
_HEADERS_COMPRA = [
    "Fecha",
    "Monto",
    "Producto",
    "Cantidad",
    "Bonificación",
    "Descuento",
]


def _compra_summary() -> dict[str, Any]:
    rows = [
        {
            "Fecha": "2026-03-10",
            "Monto": "5000",
            "Producto": "Vela aromática 200g",
            "Cantidad": "10",
            "Bonificación": "150",
            "Descuento": "80",
            "__context__": "sheet:compras",
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "row_count": 1,
        "gastos_detectados": rows,
        "mapping_contexts": [
            {
                "context_id": "sheet:compras",
                "label": "compras",
                "source_kind": "sheet",
                "entity_type": "expense",
                "headers": _HEADERS_COMPRA,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def compra_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key="uploads/test/uuid/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="gastos",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_compra_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


@pytest.mark.asyncio
class TestLosCostosDeCompraTambienSonEscalares:
    """F-M.7 — `discount`, `taxes` y `shipping_cost_line` son escalares.

    Una planilla real trae «Bonificación» y «Descuento» como columnas separadas y
    las dos son descuentos. Sumarlas sola sería inventar una cuenta que nadie
    pidió; quedarse con la primera del orden del Excel es el incidente ASTERIA
    otra vez, ahora sobre el costo de una compra en vez del precio de un producto.
    """

    async def test_dos_columnas_de_descuento_rechazan_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        compra_file: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                {
                    "source_column": c,
                    "target_field": t,
                    "context_id": "sheet:compras",
                    "entity_type": "expense",
                }
                for c, t in [
                    ("Fecha", "expense_date"),
                    ("Monto", "amount"),
                    ("Producto", "product_name"),
                    ("Cantidad", "quantity"),
                    ("Bonificación", "discount"),
                    ("Descuento", "discount"),
                ]
            ],
            "confirmed_fields": {"gastos": True},
            "context_confirmed": {"sheet:compras": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{compra_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "Descuento de la línea" in detail, "el mensaje nombra el campo en castellano"
        for col in ("Bonificación", "Descuento"):
            assert col in detail
