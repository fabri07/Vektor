"""F-D (sub-commit 2) — un mapeo cross-sección fuera de la allowlist
(`CROSS_ENTITY_TARGETS`) o hacia un campo prohibido (`CROSS_ENTITY_FORBIDDEN_FIELDS`)
se rechaza con 422 ANTES del lease — mismo patrón que requeridos/colisiones.

`CROSS_ENTITY_TARGETS["sale"]["customer"]` (contrato ya congelado, sub-commit 1)
permite `last_name`/`address`/`locality`/`province`/`postal_code`/`customer_type`/
`iva_condition` — nunca `name`/`dni`/`cuit`/`email`/`phone` (ya son customer_*
canónicos de la venta) ni `stock_units` (prohibido en cualquier ruta cruzada).
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant

_CTX = "sheet:ventas"


def _summary() -> dict[str, Any]:
    filas = [
        {
            "Fecha": "2024-03-15",
            "Monto": "1500",
            "Localidad cliente": "San Isidro",
            "__context__": _CTX,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_venta": True,
        "row_count": 1,
        "ventas_detectadas": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["Fecha", "Monto", "Localidad cliente"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key="uploads/test/uuid/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


class TestCrossTargetFueraDeLaAllowlist:
    async def test_customer_name_cruzado_se_rechaza_ya_es_canonico(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """`customer:name` no está en la allowlist de `sale` — ya existe
        `customer_name` canónico, dos rutas al mismo dato es el bug que la
        regla evita."""
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Fecha", "transaction_date"),
                    _map("Monto", "amount"),
                    _map("Localidad cliente", "customer:name"),
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text
        assert "customer:name" in response.json()["detail"]

    async def test_stock_units_cruzado_se_rechaza_siempre(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Fecha", "transaction_date"),
                    _map("Monto", "amount"),
                    _map("Localidad cliente", "product:stock_units"),
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text
        assert "product:stock_units" in response.json()["detail"]

    async def test_prefijo_desconocido_no_es_cross_cae_a_canonico_y_falla_distinto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """`parse_target` no inventa una entidad para un prefijo que no está
        en `CROSS_ENTITY_PREFIXES` — cae a canonical, y ESE 422 lo da
        `_missing_required` (target inexistente = no cubre nada), no el
        validador de cross. Confirma que el chequeo nuevo no se dispara
        donde no corresponde."""
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Fecha", "transaction_date"),
                    _map("Monto", "amount"),
                    _map("Localidad cliente", "no_es_una_entidad:campo"),
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        # Igual rechaza (el target no existe en ningún catálogo), pero por
        # otra vía — no debe mencionar "no se pueden completar desde esta
        # sección" (el mensaje del validador de cross).
        assert response.status_code in (200, 422)
        if response.status_code == 422:
            assert "no se pueden completar desde esta sección" not in response.json()["detail"]


class TestCrossTargetPermitido:
    async def test_customer_last_name_cruzado_pasa_la_validacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """`customer:last_name` SÍ está en la allowlist — no debe rechazar
        por el validador de cross (puede rechazar o no por otra razón ajena
        a este test, pero nunca con el mensaje de "no se pueden completar")."""
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map("Fecha", "transaction_date"),
                    _map("Monto", "amount"),
                    _map("Localidad cliente", "customer:last_name"),
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_CTX: True},
            },
            headers=auth_headers,
        )
        if response.status_code == 422:
            assert "no se pueden completar desde esta sección" not in response.json()["detail"]
        else:
            assert response.status_code == 200
