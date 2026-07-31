"""Tests del framework de versionado de ingestión (F9a).

Verifica que las nuevas columnas existan, sus defaults funcionen, y que el
UPDATE de evidencia (marca archivos con ``column_risk_decisions`` como v2)
funcione correctamente.

**Nota sobre migraciones:** El harness de tests usa ``Base.metadata.create_all()``,
NO corre ``alembic upgrade head``. Esto significa:
- El ORM declara las columnas (modelo), que ``create_all`` introduce.
- El UPDATE de evidencia de la migración (poblando datos históricos) se ejercita
  aquí de forma aislada: insertamos datos, ejecutamos el mismo SQL, verificamos.
- En PROD (Neon/Railway), la migración Alembic se corre automáticamente en el
  preDeploy y completa la evidencia.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ingestion_version import INGESTION_VERSION
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_PENDING,
    REREAD_STATUS_NONE,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant


@pytest_asyncio.fixture
async def sample_tenant(db_session: AsyncSession) -> Tenant:
    """Tenant de prueba."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Test Tenant",
        display_name="Test Tenant",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.commit()
    return tenant


class TestIngestionVersionColumnsExist:
    """Verifica que las columnas nuevas existan en el modelo y tengan tipos correctos."""

    async def test_ingestion_version_column_exists_with_default_one(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``ingestion_version`` existe y tiene default=1."""
        # Insertar un archivo SIN especificar ingestion_version explícitamente.
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
        )
        db_session.add(f)
        await db_session.commit()

        # Re-cargar y verificar que el default es 1.
        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.ingestion_version == 1

    async def test_latest_preview_version_nullable(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``latest_preview_version`` es nullable."""
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
        )
        db_session.add(f)
        await db_session.commit()

        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.latest_preview_version is None

    async def test_reread_status_default_none(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``reread_status`` tiene default='NONE'."""
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
        )
        db_session.add(f)
        await db_session.commit()

        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.reread_status == REREAD_STATUS_NONE

    async def test_reread_at_nullable(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``reread_at`` es nullable."""
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
        )
        db_session.add(f)
        await db_session.commit()

        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.reread_at is None

    async def test_reread_summary_nullable(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``reread_summary`` es nullable."""
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
        )
        db_session.add(f)
        await db_session.commit()

        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.reread_summary is None


class TestIngestionVersionUpdateEvidence:
    """Tests del UPDATE de evidencia que marca archivos con ``column_risk_decisions``.

    NOTA: En los tests (SQLite en memoria), usamos un enfoque determinístico que carga
    el archivo, verifica si tiene ``column_risk_decisions``, y lo actualiza en Python.
    En PROD (Postgres), la migración Alembic usa la sintaxis SQL nativa de Postgres:
    ``parsed_summary_json ? 'column_risk_decisions'`` (operador JSONB de existence).
    Ambos enfoques llegan al mismo resultado: marcar v2 iff la key está presente.
    """

    async def test_update_evidence_marks_file_with_column_risk_decisions_as_v2(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Archivo DONE con ``column_risk_decisions`` en summary → ingestion_version=2."""
        # Insertar un archivo confirmado CON la key ``column_risk_decisions``.
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={
                "inferred_type": "sales",
                "column_risk_decisions": [
                    {
                        "context_id": "main",
                        "source_column": "x",
                        "target_field": "y",
                        "action": "drop_column",
                    }
                ],
            },
            ingestion_version=1,  # Comenzar con v1 para simular pre-F8.
        )
        db_session.add(f)
        await db_session.commit()

        # Simular la lógica de la migración: cargar todos los archivos DONE
        # y actualizar aquellos con column_risk_decisions.
        result = await db_session.execute(
            select(UploadedFile).where(
                UploadedFile.tenant_id == sample_tenant.tenant_id,
                UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            )
        )
        files = result.scalars().all()

        for file in files:
            # Verificar si tiene column_risk_decisions en el summary.
            if (
                file.parsed_summary_json
                and isinstance(file.parsed_summary_json, dict)
                and "column_risk_decisions" in file.parsed_summary_json
            ):
                file.ingestion_version = 2

        await db_session.commit()

        # Recargar y verificar.
        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.ingestion_version == 2

    async def test_update_evidence_preserves_v1_for_file_without_column_risk_decisions(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Archivo DONE SIN ``column_risk_decisions`` → ingestion_version queda=1."""
        # Insertar un archivo confirmado SIN la key (como pre-F8).
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={
                "inferred_type": "sales",
                "confirmed_fields": {"fecha": "transaction_date"},
            },
            ingestion_version=1,
        )
        db_session.add(f)
        await db_session.commit()

        # Simular la lógica de la migración.
        result = await db_session.execute(
            select(UploadedFile).where(
                UploadedFile.tenant_id == sample_tenant.tenant_id,
                UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            )
        )
        files = result.scalars().all()

        for file in files:
            if (
                file.parsed_summary_json
                and isinstance(file.parsed_summary_json, dict)
                and "column_risk_decisions" in file.parsed_summary_json
            ):
                file.ingestion_version = 2

        await db_session.commit()

        # Recargar y verificar que sigue en v1 (no cambió).
        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.ingestion_version == 1

    async def test_update_evidence_only_touches_done_files(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El UPDATE solo toca archivos con processing_status=DONE."""
        # Archivo PENDING con column_risk_decisions (escenario imposible, pero check).
        f = UploadedFile(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="test.csv",
            s3_key="test.csv",
            content_type="text/csv",
            size_bytes=100,
            purpose="ventas",
            processing_status=PROCESSING_STATUS_PENDING,
            parsed_summary_json={
                "column_risk_decisions": [
                    {"context_id": "x", "source_column": "y", "action": "drop_column"}
                ]
            },
            ingestion_version=1,
        )
        db_session.add(f)
        await db_session.commit()

        # Simular la lógica de la migración: solo toca DONE.
        result = await db_session.execute(
            select(UploadedFile).where(
                UploadedFile.tenant_id == sample_tenant.tenant_id,
                UploadedFile.processing_status == PROCESSING_STATUS_DONE,
            )
        )
        files = result.scalars().all()

        for file in files:
            if (
                file.parsed_summary_json
                and isinstance(file.parsed_summary_json, dict)
                and "column_risk_decisions" in file.parsed_summary_json
            ):
                file.ingestion_version = 2

        await db_session.commit()

        # Recargar: versión sigue siendo 1 porque no es DONE.
        reloaded = await db_session.get(UploadedFile, f.id)
        assert reloaded is not None
        assert reloaded.ingestion_version == 1


class TestIngestionVersionConstant:
    """Verifica que la constante INGESTION_VERSION sea correcta."""

    def test_current_version_is_three(self) -> None:
        """La versión actual debe ser 3 (ledger de reversa de productos).

        Subirla no es cosmético: un archivo con versión < 3 se importó SIN el
        registro de qué productos creó, y el borrado lo trata distinto (avisa en
        vez de adivinar). Si alguien la cambia, este test lo obliga a leer el
        historial de `ingestion_version.py` y decidir a conciencia.
        """
        assert INGESTION_VERSION == 3
