"""F7d — superficie API de contadores/warnings/preview de maestros.

Cubre lo que F7c dejó en `counts` pero todavía no se veía desde afuera:

  - `warnings` del confirm: "anonimo" (venta de mostrador / compra sin
    proveedor informado) NUNCA avisa; "no_resuelto" (referencia que no
    matcheó) SÍ; maestros con needs_review/invalidos también avisan (nunca se
    persisten, así que el aviso es la única señal).
  - `message` incluye clientes/proveedores importados.
  - `rows_out` del evento de pipeline suma clientes/proveedores.
  - `GET /files/{id}/preview` expone `master_previews` (conteos + muestra
    diagnóstica) con PII minimizada.
"""

from __future__ import annotations

import json
import unittest.mock
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import STAGE_CONFIRM, PipelineEvent
from app.persistence.models.tenant import Tenant

_VALID_DNI = "30111222"
_OTHER_DNI = "40987654"


def _mixed_summary() -> dict[str, Any]:
    """Libro mixto: hoja de Clientes (1 válida, 1 needs_review, 1 inválida) +
    hoja de Ventas (1 matched, 1 unresolved, 1 anónima)."""
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "entity_type": "customer",
                "headers": ["nombre", "documento"],
                "row_count": 3,
            },
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "headers": ["fecha", "valor", "doc_cliente"],
                "row_count": 3,
            },
        ],
        "clientes_detectados": [
            {
                "nombre": "Juan Perez",
                "documento": _VALID_DNI,
                "__context__": "sheet:Clientes",
            },
            {"nombre": "Sin Documento", "__context__": "sheet:Clientes"},
            {"nombre": "Doc Invalido", "documento": "abc", "__context__": "sheet:Clientes"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "valor": "5000",
                "doc_cliente": _VALID_DNI,
                "__context__": "sheet:Ventas",
            },
            {
                "fecha": "2024-01-16",
                "valor": "3000",
                "doc_cliente": _OTHER_DNI,
                "__context__": "sheet:Ventas",
            },
            {"fecha": "2024-01-17", "valor": "1200", "__context__": "sheet:Ventas"},
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
    }


def _mixed_column_mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_column": "nombre",
            "target_field": "name",
            "context_id": "sheet:Clientes",
            "entity_type": "customer",
        },
        {
            "source_column": "documento",
            "target_field": "dni",
            "context_id": "sheet:Clientes",
            "entity_type": "customer",
        },
        {
            "source_column": "fecha",
            "target_field": "transaction_date",
            "context_id": "sheet:Ventas",
            "entity_type": "sale",
        },
        {
            "source_column": "valor",
            "target_field": "amount",
            "context_id": "sheet:Ventas",
            "entity_type": "sale",
        },
        {
            "source_column": "doc_cliente",
            "target_field": "customer_dni",
            "context_id": "sheet:Ventas",
            "entity_type": "sale",
        },
    ]


class TestConfirmMasterWarningsAndCounts:
    async def test_confirm_reporta_needs_review_invalido_no_resuelto_pero_no_anonimo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """También verifica que needs_review/invalido NUNCA se persisten vía
        /confirm — el aviso es la única señal de que esas filas quedaron afuera."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto.xlsx",
            s3_key="uploads/test/uuid/mixto.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=2048,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=_mixed_summary(),
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True, "clientes": True},
                "column_mappings": _mixed_column_mappings(),
                "context_confirmed": {"sheet:Clientes": True, "sheet:Ventas": True},
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()

        # message: clientes importados (1 creado) + ventas.
        assert "1 cliente" in data["message"]
        assert "3 venta" in data["message"]

        warnings_text = " ".join(data["warnings"])
        # no_resuelto (1 venta con doc que no matcheó) SÍ avisa.
        assert "no se pudo identificar" in warnings_text
        # needs_review (1 fila de clientes) y inválido (1 fila) SÍ avisan.
        assert "no tenían un dato fuerte" in warnings_text
        assert "datos inválidos o ambiguos" in warnings_text
        # anonimo (1 venta sin ninguna columna de cliente) NUNCA avisa.
        assert "anonimo" not in warnings_text.lower()
        assert "anónima" not in warnings_text.lower()

        # El confirm SALTEA needs_review/invalido — nunca se persisten. De las 3
        # filas de la hoja de Clientes (Juan Perez válido, Sin Documento
        # needs_review, Doc Invalido inválido) solo existe la válida — más el
        # sentinela "Local" (la venta unresolved/anonymous se asigna ahí).
        from app.persistence.models.customer import Customer  # noqa: PLC0415

        customer_names = {
            c.name
            for c in (await db_session.execute(select(Customer))).scalars().all()
        }
        assert customer_names == {"Juan Perez", "Local"}

    async def test_confirm_no_persiste_pii_cruda_de_maestros_en_parsed_summary(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Review 7d (Important): `compact_summary` limpiaba ventas_detectadas/
        gastos_detectados/stock_detectado/otros_detectados/preview_rows/
        mapping_contexts pero NO clientes_detectados/proveedores_detectados —
        esos buckets traen nombre/DNI/CUIT/email/teléfono crudos y quedaban
        at-rest en el JSONB, re-servidos por GET /files/{id}/preview."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto_pii.xlsx",
            s3_key="uploads/test/uuid/mixto_pii.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=2048,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=_mixed_summary(),
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True, "clientes": True},
                "column_mappings": _mixed_column_mappings(),
                "context_confirmed": {"sheet:Clientes": True, "sheet:Ventas": True},
            },
        )
        assert response.status_code == 200, response.text

        await db_session.refresh(record)
        stored = record.parsed_summary_json or {}
        assert "clientes_detectados" not in stored
        assert "proveedores_detectados" not in stored
        # Ningún DNI crudo de la hoja de Clientes sobrevive al confirm.
        assert _VALID_DNI not in json.dumps(stored)
        assert _OTHER_DNI not in json.dumps(stored)

        # El GET /preview re-sirve exactamente ese summary guardado — verificar
        # ahí también cubre el otro consumidor del mismo dato at-rest.
        preview_response = await client.get(
            f"/api/v1/ingestion/files/{record.id}/preview",
            headers=auth_headers,
        )
        assert preview_response.status_code == 200, preview_response.text
        assert _VALID_DNI not in preview_response.text
        assert _OTHER_DNI not in preview_response.text

    async def test_confirm_venta_anonima_sola_no_genera_ningun_warning_de_cliente(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        """Un archivo de solo ventas de mostrador (sin ninguna columna de cliente)
        no debe generar NINGÚN warning relacionado a cliente — es el caso normal."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mostrador.xlsx",
            s3_key="uploads/test/uuid/mostrador.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "row_count": 1,
                "ventas_detectadas": [
                    {"fecha": "2024-01-15", "monto": "50000", "descripcion": "Venta"}
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={"confirmed_fields": {"ventas": True}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["warnings"] == []

    async def test_confirm_rows_out_incluye_clientes_y_proveedores(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="mixto2.xlsx",
            s3_key="uploads/test/uuid/mixto2.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=2048,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=_mixed_summary(),
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"ventas": True, "clientes": True},
                "column_mappings": _mixed_column_mappings(),
                "context_confirmed": {"sheet:Clientes": True, "sheet:Ventas": True},
            },
        )
        assert response.status_code == 200, response.text

        event = (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.tenant_id == sample_tenant.tenant_id,
                    PipelineEvent.file_id == record.id,
                    PipelineEvent.stage == STAGE_CONFIRM,
                )
            )
        ).scalar_one()
        # 3 ventas + 1 cliente creado (needs_review/invalido no cuentan, no se
        # persistieron).
        assert event.rows_out == 3 + 1


class TestMasterPreviewEndpoint:
    async def test_preview_expone_master_previews_con_pii_minimizada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="clientes_preview.xlsx",
            s3_key="uploads/test/uuid/clientes_preview.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=1024,
            purpose="clientes",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "clientes",
                "mapping_contexts": [
                    {
                        "context_id": "table",
                        "entity_type": "customer",
                        "headers": ["nombre", "dni"],
                        "preview_rows": [
                            {"nombre": "Juan Perez", "dni": _VALID_DNI},
                        ],
                    }
                ],
                "clientes_detectados": [
                    {"nombre": "Juan Perez", "dni": _VALID_DNI},
                    {"nombre": "Cliente Sin Doc"},
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/ingestion/files/{record.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        previews = data["master_previews"]
        assert len(previews) == 1
        preview = previews[0]
        assert preview["entity_type"] == "customer"
        assert preview["to_create"] == 1
        assert preview["needs_review"] == 1

        # PII minimizada: la muestra de `master_previews` lleva nombre + estado +
        # issue, nunca el DNI crudo (el `parsed_summary_json` de la respuesta SÍ
        # trae el dato crudo — es el summary tal cual se guardó, ya expuesto hoy
        # para armar la UI de mapeo; lo que F7d minimiza es SOLO la muestra nueva).
        assert _VALID_DNI not in json.dumps(preview)
        for sample in preview["samples"]:
            assert set(sample.keys()) == {
                "row_index",
                "status",
                "display_name",
                "existing_name",
                "issue",
            }

    async def test_preview_de_maestro_nunca_dispara_el_fallback_llm(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review 7d (Important): GET /files/{id}/preview puede correr en cada
        poll/reload de la página — nunca debe disparar la 4ª capa LLM de
        mapeo, ni siquiera con ENABLE_LLM_COLUMN_MAPPING=True. El fallback
        LLM sigue disponible en GET /column-mappings (el flujo real de
        mapeo que el usuario dispara explícitamente)."""
        from app.config.settings import get_settings

        monkeypatch.setattr(get_settings(), "ENABLE_LLM_COLUMN_MAPPING", True)
        llm_mock = unittest.mock.AsyncMock(return_value={})
        monkeypatch.setattr(
            "app.application.services.llm_column_mapper.suggest_with_llm", llm_mock
        )

        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="clientes_llm.xlsx",
            s3_key="uploads/test/uuid/clientes_llm.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=1024,
            purpose="clientes",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "clientes",
                "mapping_contexts": [
                    {
                        "context_id": "table",
                        "entity_type": "customer",
                        # "zzz_ambiguo" no matchea ninguna heurística/fuzzy con
                        # confianza — entraría al fallback LLM si no estuviera
                        # gateado (confidence < LLM_MAPPING_THRESHOLD).
                        "headers": ["nombre", "zzz_ambiguo"],
                        "preview_rows": [{"nombre": "Juan Perez", "zzz_ambiguo": "x"}],
                    }
                ],
                "clientes_detectados": [{"nombre": "Juan Perez", "zzz_ambiguo": "x"}],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/ingestion/files/{record.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        llm_mock.assert_not_called()

    async def test_preview_summary_legacy_sin_mapping_contexts_master_previews_vacio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Backward-compat: un summary viejo (ventas, sin ningún dato de maestro)
        no rompe el preview — `master_previews` queda vacío."""
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="legacy.xlsx",
            s3_key="uploads/test/uuid/legacy.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "3000"}],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.get(
            f"/api/v1/ingestion/files/{record.id}/preview",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["master_previews"] == []
