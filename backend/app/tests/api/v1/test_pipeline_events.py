"""FASE 0: observabilidad y preservación del pipeline de ingestión.

Verifica:
  - Upload emite un evento `upload` en pipeline_events con trace_id en el archivo.
  - Confirm emite un evento `confirm` con los conteos importados.
  - DELETE es soft (deleted_at) y NO borra el crudo de R2; el archivo se oculta
    de los listados pero el registro persiste.
  - El servicio get_trace reconstruye el ciclo de vida ordenado.

Nota: get_stats usa percentile_cont (Postgres-only) — no se testea en SQLite.
"""

import hashlib
import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import pipeline_event_service
from app.application.services.reread_service import REPAIR_TYPE_REREAD
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.pipeline_event import (
    STAGE_CONFIRM,
    STAGE_PARSE,
    STAGE_UPLOAD,
    PipelineEvent,
)
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant


async def _events_for(session: AsyncSession, tenant_id: uuid.UUID) -> list[PipelineEvent]:
    result = await session.execute(
        select(PipelineEvent).where(PipelineEvent.tenant_id == tenant_id)
    )
    return list(result.scalars().all())


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


class TestReuploadDedup:
    async def test_reupload_of_imported_file_warns(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        xlsx_bytes: bytes,
    ) -> None:
        from app.jobs.ingestion_worker import process_spreadsheet

        # Un archivo ya importado (DONE) con el hash del contenido que vamos a subir.
        prior = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas_enero.xlsx",
            s3_key="uploads/test/uuid/ventas_enero.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            size_bytes=len(xlsx_bytes),
            purpose="ventas",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
            content_hash=hashlib.sha256(xlsx_bytes).hexdigest(),
        )
        db_session.add(prior)
        await db_session.commit()

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
                params={"allow_duplicate": "true"},
            )
        assert response.status_code == 201
        data = response.json()
        assert data["duplicate_of"] == str(prior.id)
        assert data["warning"] and "importado" in data["warning"].lower()


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
            # confirm=true: el borrado ahora también revierte los datos que el
            # archivo importó, así que exige confirmación explícita. Lo que este
            # test fija sigue siendo lo mismo — el CRUDO en R2 no se toca.
            response = await client.delete(
                f"/api/v1/ingestion/files/{file_id}?confirm=true",
                headers=auth_headers,
            )
        assert response.status_code == 200
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


class TestImportReceipt:
    """Cambio 4: el STAGE_CONFIRM guarda el comprobante columna por columna —
    incluida una columna que el usuario NUNCA mapeó (no aparece en
    body.column_mappings), que solo puede verse desde el inventario completo
    de `mapping_contexts`, no desde el mapeo elegido."""

    async def test_confirm_guarda_comprobante_con_columna_nunca_mapeada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        trace_id = uuid.uuid4()
        record = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="catalogo.xlsx",
            s3_key="uploads/test/uuid/catalogo.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            size_bytes=1024,
            purpose="productos",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            trace_id=trace_id,
            parsed_summary_json={
                "confidence": "HIGH",
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "has_stock": True,
                "mapping_contexts": [
                    {
                        "context_id": "sheet:Catalogo",
                        "label": "Catálogo",
                        "entity_type": "product",
                        # "codigo_interno" nunca aparece en column_mappings.
                        "headers": ["nombre", "precio", "codigo_interno"],
                        "row_count": 1,
                    }
                ],
                "stock_detectado": [
                    {
                        "nombre": "Silla de living",
                        "precio": "5000",
                        "codigo_interno": "SKU-1",
                        "__context__": "sheet:Catalogo",
                    }
                ],
            },
        )
        db_session.add(record)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{record.id}/confirm",
            headers=auth_headers,
            json={
                "confirmed_fields": {"productos": True},
                "column_mappings": [
                    {
                        "context_id": "sheet:Catalogo",
                        "source_column": "nombre",
                        "target_field": "name",
                    },
                    {
                        "context_id": "sheet:Catalogo",
                        "source_column": "precio",
                        "target_field": "sale_price_ars",
                    },
                ],
            },
        )
        assert response.status_code == 200, response.text

        events = await pipeline_event_service.get_trace(db_session, trace_id)
        confirm_events = [e for e in events if e["stage"] == "confirm"]
        assert len(confirm_events) == 1
        receipt = confirm_events[0]["detail"]["receipt"]
        assert receipt["version"] == 1
        columns = {c["source_column"]: c for c in receipt["columns"]}

        assert columns["nombre"]["result"] == "guardado"
        assert columns["precio"]["result"] == "transformado"  # campo numérico (E4)
        # La columna nunca mapeada NO desaparece: sigue en el comprobante.
        assert columns["codigo_interno"]["result"] == "excluido"
        assert columns["codigo_interno"]["reason_kind"] == "system_rule"
        assert columns["codigo_interno"]["target_field"] is None


class TestImportReceiptEndpoint:
    """Cambio 4: GET .../receipt combina el STAGE_CONFIRM (pipeline_events) y
    el DataRepairRun REREAD_FILE más reciente en una sola respuesta,
    distinguiendo última APLICACIÓN de último INTENTO."""

    @staticmethod
    async def _file(session: AsyncSession, tenant_id: uuid.UUID) -> UploadedFile:
        f = UploadedFile(
            tenant_id=tenant_id,
            uploaded_by=None,
            original_filename="catalogo.xlsx",
            s3_key=f"tenants/{tenant_id}/catalogo.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="productos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={},
        )
        session.add(f)
        await session.commit()
        return f

    async def test_404_si_el_archivo_no_existe(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/receipt", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_solo_confirm_es_la_ultima_aplicacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        trace_id = uuid.uuid4()
        f = await self._file(db_session, sample_tenant.tenant_id)
        db_session.add(
            PipelineEvent(
                trace_id=trace_id,
                tenant_id=sample_tenant.tenant_id,
                file_id=f.id,
                stage=STAGE_CONFIRM,
                detail={"receipt": {"version": 1, "columns": [{"source_column": "nombre"}]}},
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/ingestion/files/{f.id}/receipt", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_reverted"] is False
        assert data["last_applied"]["kind"] == "confirm"
        assert data["last_applied"]["receipt"]["historical_incomplete"] is False
        assert data["last_attempt"] is None

    async def test_reread_aplicada_despues_del_confirm_gana_la_ultima_aplicacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        f = await self._file(db_session, sample_tenant.tenant_id)
        now = datetime.now(UTC)
        db_session.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                file_id=f.id,
                stage=STAGE_CONFIRM,
                detail={"receipt": {"version": 1, "columns": []}},
                created_at=now - timedelta(hours=1),
            )
        )
        db_session.add(
            DataRepairRun(
                tenant_id=sample_tenant.tenant_id,
                repair_type=REPAIR_TYPE_REREAD,
                status="APPLIED",
                dry_run=False,
                completed_at=now,
                details_json={
                    "file_id": str(f.id),
                    "receipt": {"version": 1, "columns": [{"source_column": "reread_col"}]},
                },
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/ingestion/files/{f.id}/receipt", headers=auth_headers)
        data = resp.json()
        assert data["last_applied"]["kind"] == "reread"
        assert data["last_applied"]["receipt"]["columns"][0]["source_column"] == "reread_col"
        assert data["last_attempt"] is None

    async def test_reread_fallida_posterior_aparece_como_ultimo_intento_no_como_aplicacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Un archivo con confirm exitoso y una relectura FALLIDA más reciente
        no puede mostrar el 200 del confirm como si nada hubiera pasado
        después — el intento fallido tiene que ser visible."""
        f = await self._file(db_session, sample_tenant.tenant_id)
        now = datetime.now(UTC)
        db_session.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                file_id=f.id,
                stage=STAGE_CONFIRM,
                detail={"receipt": {"version": 1, "columns": []}},
                created_at=now - timedelta(hours=1),
            )
        )
        db_session.add(
            DataRepairRun(
                tenant_id=sample_tenant.tenant_id,
                repair_type=REPAIR_TYPE_REREAD,
                status="FAILED",
                dry_run=False,
                completed_at=now,
                details_json={"file_id": str(f.id), "error": "Producto↔Proveedor apagado"},
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/ingestion/files/{f.id}/receipt", headers=auth_headers)
        data = resp.json()
        assert data["last_applied"]["kind"] == "confirm"  # la relectura fallida no cuenta
        assert data["last_attempt"]["kind"] == "reread"
        assert data["last_attempt"]["status"] == "FAILED"
        assert "Proveedor" in data["last_attempt"]["error"]

    async def test_archivo_borrado_marca_file_reverted_sin_perder_la_explicacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        f = await self._file(db_session, sample_tenant.tenant_id)
        db_session.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                file_id=f.id,
                stage=STAGE_CONFIRM,
                detail={"receipt": {"version": 1, "columns": [{"source_column": "nombre"}]}},
            )
        )
        f.deleted_at = datetime.now(UTC)
        await db_session.commit()

        resp = await client.get(f"/api/v1/ingestion/files/{f.id}/receipt", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_reverted"] is True
        # F11 no toca pipeline_events: la explicación original sigue entera.
        assert data["last_applied"]["receipt"]["columns"][0]["source_column"] == "nombre"

    async def test_evento_historico_sin_receipt_se_reconstruye_marcado_como_incompleto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        f = await self._file(db_session, sample_tenant.tenant_id)
        db_session.add(
            PipelineEvent(
                trace_id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                file_id=f.id,
                stage=STAGE_CONFIRM,
                # Evento PRE-Cambio 4: tiene mappings pero no "receipt".
                detail={"mappings": {"flat": {"nombre": "name"}, "context": {}}},
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/ingestion/files/{f.id}/receipt", headers=auth_headers)
        data = resp.json()
        receipt = data["last_applied"]["receipt"]
        assert receipt["historical_incomplete"] is True
        assert receipt["columns"][0]["source_column"] == "nombre"
        assert receipt["columns"][0]["reason_kind"] is None
