"""FASE 0: observabilidad y preservación del pipeline de ingestión.

Verifica:
  - Upload emite un evento `upload` en pipeline_events con trace_id en el archivo.
  - Confirm emite un evento `confirm` con los conteos importados.
  - DELETE es soft (deleted_at) y NO borra el crudo de R2; el archivo se oculta
    de los listados pero el registro persiste.
  - El servicio get_trace reconstruye el ciclo de vida ordenado.

Nota: get_stats usa percentile_cont (Postgres-only) — no se testea en SQLite.
"""

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import pipeline_event_service
from app.persistence.models.file import (
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.pipeline_event import (
    STAGE_PARSE,
    STAGE_UPLOAD,
    PipelineEvent,
)
from app.persistence.models.tenant import Tenant


async def _events_for(session: AsyncSession, tenant_id: uuid.UUID) -> list[PipelineEvent]:
    result = await session.execute(
        select(PipelineEvent).where(PipelineEvent.tenant_id == tenant_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
class TestUploadEmitsEvent:
    async def test_upload_emits_upload_event_with_trace(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        xlsx_bytes: bytes,
    ) -> None:
        from app.jobs.ingestion_worker import process_spreadsheet

        with (
            unittest.mock.patch(
                "app.api.v1.ingestion.S3Client.upload_to_key",
                new_callable=unittest.mock.AsyncMock,
                return_value="uploads/fake/uuid/ventas.xlsx",
            ),
            unittest.mock.patch.object(process_spreadsheet, "delay"),
        ):
            response = await client.post(
                "/api/v1/ingestion/upload",
                headers=auth_headers,
                files={"file": ("ventas.xlsx", xlsx_bytes, "application/octet-stream")},
                params={"file_hint": "ventas"},
            )
        assert response.status_code == 201
        file_id = uuid.UUID(response.json()["file_id"])

        events = await _events_for(db_session, sample_tenant.tenant_id)
        upload_events = [e for e in events if e.stage == STAGE_UPLOAD]
        assert len(upload_events) == 1
        ev = upload_events[0]
        assert ev.file_id == file_id
        assert ev.detail is not None
        assert ev.detail.get("content_hash")
        assert ev.detail.get("file_hint") == "ventas"

        # El archivo guarda el mismo trace_id que agrupa sus eventos.
        record = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == file_id)
            )
        ).scalar_one()
        assert record.trace_id == ev.trace_id
        assert record.content_hash == ev.detail["content_hash"]


@pytest.mark.asyncio
class TestConfirmEmitsEvent:
    async def test_confirm_emits_confirm_event_with_counts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: object,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        trace_id = uuid.uuid4()
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key="uploads/test/uuid/ventas.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            size_bytes=1024,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            trace_id=trace_id,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
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
            json={"confirmed_fields": {"ventas": True, "gastos": False}},
        )
        assert response.status_code == 200

        events = await pipeline_event_service.get_trace(db_session, trace_id)
        confirm_events = [e for e in events if e["stage"] == "confirm"]
        assert len(confirm_events) == 1
        detail = confirm_events[0]["detail"]
        assert detail["imported_counts"]["ventas"] == 1


@pytest.mark.asyncio
class TestSoftDeletePreservesRaw:
    async def test_delete_is_soft_and_does_not_touch_s3(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="borrar.csv",
            s3_key="uploads/test/uuid/borrar.csv",
            content_type="text/csv",
            size_bytes=128,
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        )
        db_session.add(record)
        await db_session.commit()
        file_id = record.id

        with unittest.mock.patch(
            "app.integrations.s3.S3Client.delete",
            new_callable=unittest.mock.AsyncMock,
        ) as mock_s3_delete:
            response = await client.delete(
                f"/api/v1/ingestion/files/{file_id}",
                headers=auth_headers,
            )
        assert response.status_code == 204
        # El crudo NO se borra de R2.
        mock_s3_delete.assert_not_called()

        # El registro persiste con deleted_at; el crudo (s3_key) se preserva.
        refreshed = (
            await db_session.execute(
                select(UploadedFile).where(UploadedFile.id == file_id)
            )
        ).scalar_one()
        assert refreshed.deleted_at is not None
        assert refreshed.s3_key == "uploads/test/uuid/borrar.csv"

        # Ya no aparece en el listado del tenant.
        list_resp = await client.get("/api/v1/ingestion/files", headers=auth_headers)
        assert list_resp.status_code == 200
        listed_ids = {f["file_id"] for f in list_resp.json()}
        assert str(file_id) not in listed_ids


@pytest.mark.asyncio
class TestPipelineEventService:
    async def test_get_trace_returns_ordered_events(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        trace_id = uuid.uuid4()
        await pipeline_event_service.emit_event(
            db_session,
            trace_id=trace_id,
            tenant_id=sample_tenant.tenant_id,
            stage=STAGE_UPLOAD,
            rows_in=None,
        )
        await pipeline_event_service.emit_event(
            db_session,
            trace_id=trace_id,
            tenant_id=sample_tenant.tenant_id,
            stage=STAGE_PARSE,
            rows_in=120,
            confidence="HIGH",
        )
        await db_session.commit()

        events = await pipeline_event_service.get_trace(db_session, trace_id)
        assert [e["stage"] for e in events] == [STAGE_UPLOAD, STAGE_PARSE]
        assert events[1]["rows_in"] == 120
        assert events[1]["confidence"] == "HIGH"

    async def test_emit_rejections_caps_sample_and_records_total(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        trace_id = uuid.uuid4()
        rejected = [{"row_number": i, "raw_row": {"x": i}} for i in range(700)]
        await pipeline_event_service.emit_rejections(
            db_session,
            trace_id=trace_id,
            tenant_id=sample_tenant.tenant_id,
            file_id=None,
            rejected_rows=rejected,
            reason="amount_missing",
        )
        await db_session.commit()

        events = await pipeline_event_service.get_trace(db_session, trace_id)
        assert len(events) == 1
        ev = events[0]
        assert ev["stage"] == "reject"
        assert ev["rows_rejected"] == 700
        assert len(ev["detail"]["rows"]) == 500  # capped
        assert ev["detail"]["sample_truncated"] is True
        assert ev["detail"]["reason"] == "amount_missing"
