"""F8a — tests de API del riesgo contextual de columnas.

- ``GET /files/{id}/preview`` devuelve ``contextual_column_risk`` desde las
  sugerencias de mapeo (informativo).
- ``POST /files/{id}/column-risk`` recalcula el riesgo con el mapeo efectivo del
  usuario (``user_selected``) y es READ-ONLY (no muta el summary).
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant


def _sale_summary_with_null_dates() -> dict[str, Any]:
    """10 filas de venta; 8 con fecha vacía (heurística mapea fecha→transaction_date)."""
    rows = [
        {
            "fecha": ("01/02/2026" if i < 2 else None),
            "monto": "100",
            "categoria": (None if i > 0 else "Bebidas"),
            "__context__": "table",
        }
        for i in range(10)
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "row_count": 10,
        "ventas_detectadas": rows,
        "mapping_contexts": [
            {
                "context_id": "table",
                "label": "Tabla",
                "source_kind": "table",
                "entity_type": "sale",
                "headers": ["fecha", "monto", "categoria"],
                "fields": None,
                "preview_rows": rows[:10],
                "row_count": 10,
            }
        ],
    }


@pytest_asyncio.fixture
async def sale_file_with_risk(
    db_session: AsyncSession, sample_tenant: Tenant
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key="uploads/test/uuid/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_sale_summary_with_null_dates(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


class TestContextualColumnRisk:
    async def test_preview_incluye_riesgo_contextual_de_fecha(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file_with_risk: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{sale_file_with_risk.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 200
        risks = response.json()["contextual_column_risk"]
        fecha = next((r for r in risks if r["source_column"] == "fecha"), None)
        assert fecha is not None
        assert fecha["field_requirement"] == "required"
        assert fecha["context_id"] == "table"
        assert "route_affected_rows_to_others" in fecha["allowed_actions"]
        # requerido con una sola columna: no se puede dropear
        assert "drop_column" not in fecha["allowed_actions"]
        assert fecha["affected_rows"] == 8

    async def test_column_risk_opcional_seleccionado_por_usuario_es_accionable(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file_with_risk: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                {
                    "source_column": "categoria",
                    "target_field": "category",
                    "context_id": "table",
                    "entity_type": "sale",
                    "user_selected": True,
                }
            ],
            "context_confirmed": {"table": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file_with_risk.id}/column-risk",
            headers=auth_headers,
            json=body,
        )
        assert response.status_code == 200
        risks = response.json()
        cat = next((r for r in risks if r["source_column"] == "categoria"), None)
        assert cat is not None
        assert cat["field_requirement"] == "explicitly_selected"
        assert "drop_column" in cat["allowed_actions"]

    async def test_column_risk_context_entity_invalido_devuelve_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file_with_risk: UploadedFile,
    ) -> None:
        """`context_entity` es `Literal[sale|expense|product|customer|supplier]`
        (review de armonía F8): un valor fuera de ese set (incluido `""`, que
        antes caía silenciosamente vía `or` a la entidad original) se rechaza
        en el borde de la API, no más adentro del pipeline."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file_with_risk.id}/column-risk",
            headers=auth_headers,
            json={"context_entity": {"table": ""}},
        )
        assert response.status_code == 422

    async def test_column_risk_contexto_excluido_no_devuelve_riesgo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file_with_risk: UploadedFile,
    ) -> None:
        """El usuario excluyó el contexto del import → sin riesgo accionable."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file_with_risk.id}/column-risk",
            headers=auth_headers,
            json={"context_confirmed": {"table": False}},
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_column_risk_es_read_only(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sale_file_with_risk: UploadedFile,
    ) -> None:
        before = dict(sale_file_with_risk.parsed_summary_json or {})
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file_with_risk.id}/column-risk",
            headers=auth_headers,
            json={"column_mappings": []},
        )
        assert response.status_code == 200
        await db_session.refresh(sale_file_with_risk)
        assert sale_file_with_risk.parsed_summary_json == before

    async def test_column_risk_archivo_inexistente_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        import uuid

        response = await client.post(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/column-risk",
            headers=auth_headers,
            json={"column_mappings": []},
        )
        assert response.status_code == 404


@pytest_asyncio.fixture
async def customer_file_with_risk(
    db_session: AsyncSession, sample_tenant: Tenant
) -> UploadedFile:
    """Hoja de clientes con `nombre` parcialmente vacío (maestro)."""
    rows = [
        {"nombre": (None if i > 2 else f"Cliente {i}"), "tel": None, "__context__": "table"}
        for i in range(6)
    ]
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="clientes.xlsx",
        s3_key="uploads/test/uuid/clientes.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="clientes",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH",
            "file_type": "spreadsheet",
            "inferred_type": "clientes",
            "row_count": 6,
            "clientes_detectados": rows,
            "mapping_contexts": [
                {
                    "context_id": "table",
                    "label": "Tabla",
                    "source_kind": "table",
                    "entity_type": "customer",
                    "headers": ["nombre", "tel"],
                    "fields": None,
                    "preview_rows": rows[:10],
                    "row_count": 6,
                }
            ],
        },
    )
    db_session.add(record)
    await db_session.commit()
    return record


class TestMasterColumnRisk:
    """F8a fix: customer/supplier participan del protocolo (no solo transaccionales)."""

    async def test_column_risk_cubre_customer_name_requerido(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        customer_file_with_risk: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                {
                    "source_column": "nombre",
                    "target_field": "name",
                    "context_id": "table",
                    "entity_type": "customer",
                }
            ],
            "context_confirmed": {"table": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{customer_file_with_risk.id}/column-risk",
            headers=auth_headers,
            json=body,
        )
        assert response.status_code == 200
        risks = response.json()
        name = next((r for r in risks if r["source_column"] == "nombre"), None)
        assert name is not None
        assert name["entity_type"] == "customer"
        assert name["field_requirement"] == "required"
        assert name["affected_rows"] == 3
