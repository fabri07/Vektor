"""Tests E2E para el flujo de mapeo inteligente de columnas (Sprint 21).

Cubre:
  - GET /ingestion/files/{id}/column-mappings → sugerencias
  - POST /confirm con column_mappings explícitos → importa con mapeo correcto
  - POST /confirm con campo requerido faltante → 422
  - POST /confirm con custom_field → crea TenantCustomFieldDefinition
  - GET /ingestion/column-mappings → aprendizaje persiste después de confirm
  - DELETE /ingestion/column-mappings/{id}
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.field_definitions import TenantCustomFieldDefinition
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

# ── Fixture: archivo con headers y datos ─────────────────────────────────────

def _make_file(
    tenant_id: uuid.UUID,
    inferred_type: str = "ventas",
    headers: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> UploadedFile:
    headers = headers or ["Fecha", "P. Unitario", "Cant.", "Descripcion"]
    rows = rows or [
        {"Fecha": "2024-01-15", "P. Unitario": "1500", "Cant.": "2", "Descripcion": "Jugo"},
        {"Fecha": "2024-01-16", "P. Unitario": "800", "Cant.": "1", "Descripcion": "Gaseosa"},
    ]
    preview = rows[:10]
    return UploadedFile(
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/{tenant_id}/uuid/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "file_type": "spreadsheet",
            "inferred_type": inferred_type,
            "confidence": "HIGH",
            "has_fecha": True,
            "has_venta": True,
            "headers": headers,
            "preview_rows": preview,
            "ventas_detectadas": rows,
            "gastos_detectados": [],
            "stock_detectado": [],
            "row_count": len(rows),
            "rows_processed": len(rows),
        },
    )


@pytest_asyncio.fixture
async def sale_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = _make_file(sample_tenant.tenant_id)
    db_session.add(record)
    await db_session.commit()
    return record


@pytest_asyncio.fixture
async def product_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = _make_file(
        sample_tenant.tenant_id,
        inferred_type="stock",
        headers=["Nombre", "Precio Venta", "Costo", "SKU", "Unidades"],
        rows=[
            {
                "Nombre": "Coca Cola 500ml", "Precio Venta": "1200",
                "Costo": "800", "SKU": "CC500", "Unidades": "50",
            },
            {
                "Nombre": "Sprite 500ml", "Precio Venta": "1100",
                "Costo": "750", "SKU": "SP500", "Unidades": "30",
            },
        ],
    )
    db_session.add(record)
    await db_session.commit()
    return record


# ── Tests: GET /column-mappings ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetColumnMappings:
    async def test_returns_suggestion_per_header(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{sale_file.id}/column-mappings?entity_type=sale",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        # Debe haber una sugerencia por cada header del archivo
        assert len(data) == 4  # Fecha, P. Unitario, Cant., Descripcion
        source_cols = [s["source_column"] for s in data]
        assert "Fecha" in source_cols
        assert "P. Unitario" in source_cols

    async def test_fecha_maps_to_transaction_date(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{sale_file.id}/column-mappings?entity_type=sale",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        fecha_sugg = next((s for s in data if s["source_column"] == "Fecha"), None)
        assert fecha_sugg is not None
        assert fecha_sugg["target_field"] == "transaction_date"
        assert fecha_sugg["source"] == "heuristic"
        assert fecha_sugg["status"] == "mapped"

    async def test_sample_values_populated(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{sale_file.id}/column-mappings?entity_type=sale",
            headers=auth_headers,
        )
        data = response.json()
        fecha_sugg = next(s for s in data if s["source_column"] == "Fecha")
        assert len(fecha_sugg["sample_values"]) > 0
        assert "2024-01-15" in fecha_sugg["sample_values"]

    async def test_returns_404_for_unknown_file(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/column-mappings",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_product_file_suggestions(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        product_file: UploadedFile,
    ) -> None:
        response = await client.get(
            f"/api/v1/ingestion/files/{product_file.id}/column-mappings?entity_type=product",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        nombre = next((s for s in data if s["source_column"] == "Nombre"), None)
        assert nombre is not None
        assert nombre["target_field"] == "name"


# ── Tests: POST /confirm con column_mappings ──────────────────────────────────


@pytest.fixture
def mock_score_trigger_e2e():
    from app.application.services.score_trigger_service import trigger_score_recalculation

    with unittest.mock.patch.object(trigger_score_recalculation, "delay") as mock:
        yield mock


@pytest.mark.asyncio
class TestConfirmWithColumnMappings:
    async def test_explicit_mappings_import_with_correct_date(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """column_mappings explícitos: la fecha se importa desde la columna correcta."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True, "gastos": False, "productos": False},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                    {"source_column": "Cant.", "target_field": "quantity"},
                    {"source_column": "Descripcion", "target_field": "notes"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == PROCESSING_STATUS_DONE

        # Verificar que las ventas se importaron con la fecha correcta
        result = await db_session.execute(
            select(SaleEntry).where(SaleEntry.tenant_id == sale_file.tenant_id)
        )
        sales = result.scalars().all()
        assert len(sales) == 2
        # transaction_date ahora es datetime (medianoche al importar solo fecha).
        dates = {s.transaction_date.date().isoformat() for s in sales}
        assert "2024-01-15" in dates
        assert "2024-01-16" in dates
        amounts = {s.amount for s in sales}
        assert Decimal("1500") in amounts

    async def test_explicit_mappings_save_tenant_column_mapping(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Tras confirmar con mappings, el aprendizaje se persiste en tenant_column_mappings."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                ],
            },
        )

        result = await db_session.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == sale_file.tenant_id,
                TenantColumnMapping.entity_type == "sale",
            )
        )
        mappings = result.scalars().all()
        mapping_dict = {m.source_column: m.target_field for m in mappings}
        # source_column se normaliza: "p._unitario"
        assert mapping_dict.get("fecha") == "transaction_date"
        assert mapping_dict.get("p._unitario") == "amount"

    async def test_missing_required_field_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Si falta 'amount' para ventas con column_mappings → 422."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    # Solo mapeamos fecha, falta amount
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                ],
            },
        )
        assert response.status_code == 422
        assert "amount" in response.json()["detail"]

    async def test_custom_field_mapping_creates_definition(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Mapear a custom_field:obs crea TenantCustomFieldDefinition."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                    {"source_column": "Descripcion", "target_field": "custom_field:obs_venta"},
                ],
            },
        )
        assert response.status_code == 200

        # Verificar que se creó la definición del campo personalizado
        result = await db_session.execute(
            select(TenantCustomFieldDefinition).where(
                TenantCustomFieldDefinition.tenant_id == sale_file.tenant_id,
                TenantCustomFieldDefinition.field_key == "obs_venta",
                TenantCustomFieldDefinition.entity_type == "sale",
            )
        )
        custom_field = result.scalar_one_or_none()
        assert custom_field is not None
        assert custom_field.is_base_field is False
        assert custom_field.data_type == "text"
        # El label debe ser el nombre de la columna original
        assert custom_field.override_label == "Descripcion"

    async def test_ignore_mapping_is_not_learned(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Mapeos 'ignore' no se guardan en tenant_column_mappings."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                    {"source_column": "Descripcion", "target_field": "ignore"},
                ],
            },
        )

        result = await db_session.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == sale_file.tenant_id,
                TenantColumnMapping.source_column == "descripcion",
            )
        )
        assert result.scalar_one_or_none() is None

    async def test_backward_compat_without_column_mappings(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Sin column_mappings → usa heurística legacy. El endpoint no falla aunque
        la heurística no reconozca las columnas no-estándar del archivo de test."""
        response = await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        # La heurística legacy no reconoce "P. Unitario" como monto de venta
        # (no está en VENTA_COLS), pero el endpoint NO debe crashear — devuelve 200.
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == PROCESSING_STATUS_DONE


# ── Tests: GET/DELETE /ingestion/column-mappings ──────────────────────────────


@pytest.mark.asyncio
class TestLearnedMappings:
    async def test_list_returns_empty_initially(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.get("/api/v1/ingestion/column-mappings", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_returns_mapping_after_confirm(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Después de confirmar, GET /column-mappings devuelve el mapeo aprendido."""
        await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                ],
            },
        )

        response = await client.get("/api/v1/ingestion/column-mappings", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        mapping_dict = {m["source_column"]: m["target_field"] for m in data}
        assert mapping_dict.get("fecha") == "transaction_date"
        assert mapping_dict.get("p._unitario") == "amount"
        # confirmed_count debe ser 1
        assert all(m["confirmed_count"] == 1 for m in data)

    async def test_delete_learned_mapping(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """DELETE /column-mappings/{id} elimina el mapeo y devuelve 204."""
        # Primero crear un mapeo vía confirm
        await client.post(
            f"/api/v1/ingestion/files/{sale_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True},
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "P. Unitario", "target_field": "amount"},
                ],
            },
        )

        # Listar y obtener el ID
        list_resp = await client.get("/api/v1/ingestion/column-mappings", headers=auth_headers)
        data = list_resp.json()
        assert len(data) > 0
        mapping_id = data[0]["id"]

        # Eliminar
        del_resp = await client.delete(
            f"/api/v1/ingestion/column-mappings/{mapping_id}",
            headers=auth_headers,
        )
        assert del_resp.status_code == 204

        # Verificar que ya no está
        list_resp2 = await client.get("/api/v1/ingestion/column-mappings", headers=auth_headers)
        remaining_ids = [m["id"] for m in list_resp2.json()]
        assert mapping_id not in remaining_ids

    async def test_delete_nonexistent_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        response = await client.delete(
            f"/api/v1/ingestion/column-mappings/{uuid.uuid4()}",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_tenant_isolation_on_learned_mappings(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        sale_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Un tenant no ve los mapeos aprendidos de otro tenant."""
        from datetime import datetime  # noqa: PLC0415

        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        # Insertar mapeo de otro tenant directamente en DB
        other_tenant_id = uuid.uuid4()
        now = datetime.now(tz=UTC)
        db_session.add(
            TenantColumnMapping(
                id=uuid.uuid4(),
                tenant_id=other_tenant_id,
                entity_type="sale",
                source_column="fecha",
                target_field="transaction_date",
                confirmed_count=1,
                last_seen_at=now,
                created_at=now,
            )
        )
        await db_session.commit()

        # El tenant del test no debe ver el mapeo del otro tenant
        response = await client.get("/api/v1/ingestion/column-mappings", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []


# ── Tests: confirm multi-contexto (mapeo por hoja, Fase 1) ────────────────────


def _make_multisheet_file(tenant_id: uuid.UUID) -> UploadedFile:
    """Archivo multi-hoja con dos contextos (ventas + gastos) y marcadores __context__."""
    ventas = [{"Fecha": "2024-01-15", "Valor": "5400", "__context__": "sheet:Ventas"}]
    gastos = [{"Fecha": "2024-01-15", "Importe": "12000", "__context__": "sheet:Gastos"}]
    return UploadedFile(
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename="mixto.xlsx",
        s3_key=f"uploads/{tenant_id}/uuid/mixto.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="mixed",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "confidence": "HIGH",
            "headers": ["Fecha", "Valor"],
            "mapping_contexts": [
                {
                    "context_id": "sheet:Ventas",
                    "label": "Ventas",
                    "source_kind": "sheet",
                    "entity_type": "sale",
                    "headers": ["Fecha", "Valor"],
                    "fields": None,
                    "preview_rows": [{"Fecha": "2024-01-15", "Valor": "5400"}],
                    "row_count": 1,
                },
                {
                    "context_id": "sheet:Gastos",
                    "label": "Gastos",
                    "source_kind": "sheet",
                    "entity_type": "expense",
                    "headers": ["Fecha", "Importe"],
                    "fields": None,
                    "preview_rows": [{"Fecha": "2024-01-15", "Importe": "12000"}],
                    "row_count": 1,
                },
            ],
            "ventas_detectadas": ventas,
            "gastos_detectados": gastos,
            "stock_detectado": [],
            "row_count": 2,
        },
    )


@pytest_asyncio.fixture
async def multisheet_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = _make_multisheet_file(sample_tenant.tenant_id)
    db_session.add(record)
    await db_session.commit()
    return record


@pytest.mark.asyncio
class TestConfirmWithContextMappings:
    async def test_get_mappings_by_context_uses_sheet_headers_and_entity(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        multisheet_file: UploadedFile,
    ) -> None:
        """GET con context_id usa los headers/entity_type de esa hoja."""
        resp = await client.get(
            f"/api/v1/ingestion/files/{multisheet_file.id}/column-mappings"
            "?entity_type=sale&context_id=sheet:Gastos",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        cols = {s["source_column"] for s in data}
        assert cols == {"Fecha", "Importe"}  # headers de la hoja Gastos, no de Ventas
        assert all(s["context_id"] == "sheet:Gastos" for s in data)

    async def test_context_qualified_confirm_imports_both_sheets(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        multisheet_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Confirm con mapeos cualificados por contexto importa cada hoja con su mapeo."""
        from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

        resp = await client.post(
            f"/api/v1/ingestion/files/{multisheet_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "column_mappings": [
                    {
                        "source_column": "Valor",
                        "target_field": "amount",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "transaction_date",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                    {
                        "source_column": "Importe",
                        "target_field": "amount",
                        "context_id": "sheet:Gastos",
                        "entity_type": "expense",
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "expense_date",
                        "context_id": "sheet:Gastos",
                        "entity_type": "expense",
                    },
                ],
                "context_confirmed": {"sheet:Ventas": True, "sheet:Gastos": True},
            },
        )
        assert resp.status_code == 200
        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == multisheet_file.tenant_id)
            )
        ).scalars().all()
        expenses = (
            await db_session.execute(
                select(ExpenseEntry).where(ExpenseEntry.tenant_id == multisheet_file.tenant_id)
            )
        ).scalars().all()
        assert len(sales) == 1
        assert sales[0].amount == Decimal("5400")
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("12000")

    async def test_context_confirmed_false_excludes_sheet(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        multisheet_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """context_confirmed=False para una hoja → no se importa."""
        from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

        resp = await client.post(
            f"/api/v1/ingestion/files/{multisheet_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "column_mappings": [
                    {
                        "source_column": "Valor",
                        "target_field": "amount",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "transaction_date",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                ],
                "context_confirmed": {"sheet:Ventas": True, "sheet:Gastos": False},
            },
        )
        assert resp.status_code == 200
        sales = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == multisheet_file.tenant_id)
            )
        ).scalars().all()
        expenses = (
            await db_session.execute(
                select(ExpenseEntry).where(ExpenseEntry.tenant_id == multisheet_file.tenant_id)
            )
        ).scalars().all()
        assert len(sales) == 1
        assert len(expenses) == 0

    async def test_entity_type_derived_from_context_not_payload(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        multisheet_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """Con context_id, el entity_type se deriva del contexto del summary, NO del
        payload. Acá el payload miente (entity_type='sale' para la hoja Gastos): la
        validación de requeridos y el aprendizaje deben usar 'expense' del contexto.
        Si se usara el payload, faltaría transaction_date y daría 422."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        resp = await client.post(
            f"/api/v1/ingestion/files/{multisheet_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "column_mappings": [
                    {
                        "source_column": "Importe",
                        "target_field": "amount",
                        "context_id": "sheet:Gastos",
                        "entity_type": "sale",  # ← payload incorrecto a propósito
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "expense_date",
                        "context_id": "sheet:Gastos",
                        "entity_type": "sale",  # ← payload incorrecto a propósito
                    },
                ],
                "context_confirmed": {"sheet:Ventas": False, "sheet:Gastos": True},
            },
        )
        assert resp.status_code == 200  # validó con requeridos de expense, no de sale
        learned = (
            await db_session.execute(
                select(TenantColumnMapping).where(
                    TenantColumnMapping.tenant_id == multisheet_file.tenant_id
                )
            )
        ).scalars().all()
        # Aprendido bajo 'expense' (contexto), no 'sale' (payload).
        assert {m.entity_type for m in learned} == {"expense"}

    async def test_context_mappings_learned_per_entity_type(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        multisheet_file: UploadedFile,
        db_session: AsyncSession,
        mock_score_trigger_e2e: unittest.mock.MagicMock,
    ) -> None:
        """El aprendizaje se guarda bajo el entity_type de cada contexto."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        await client.post(
            f"/api/v1/ingestion/files/{multisheet_file.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {},
                "column_mappings": [
                    {
                        "source_column": "Valor",
                        "target_field": "amount",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "transaction_date",
                        "context_id": "sheet:Ventas",
                        "entity_type": "sale",
                    },
                    {
                        "source_column": "Importe",
                        "target_field": "amount",
                        "context_id": "sheet:Gastos",
                        "entity_type": "expense",
                    },
                    {
                        "source_column": "Fecha",
                        "target_field": "expense_date",
                        "context_id": "sheet:Gastos",
                        "entity_type": "expense",
                    },
                ],
                "context_confirmed": {"sheet:Ventas": True, "sheet:Gastos": True},
            },
        )
        learned = (
            await db_session.execute(
                select(TenantColumnMapping).where(
                    TenantColumnMapping.tenant_id == multisheet_file.tenant_id
                )
            )
        ).scalars().all()
        by_entity = {(m.entity_type, m.source_column): m.target_field for m in learned}
        assert by_entity.get(("sale", "valor")) == "amount"
        assert by_entity.get(("expense", "importe")) == "amount"
