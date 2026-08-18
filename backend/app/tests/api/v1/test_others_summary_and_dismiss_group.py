"""F-O.3 — «Otros» dice por qué está cada fila, y agrupa.

Medido sobre ASTERIA en producción: de 2.282 filas pendientes, el 99,6% eran
DOS hojas volcadas fila por fila. `GET /others/summary` agrupa por
`uploaded_file_id × source × context_label × suggested_entity × status` (no
solo archivo+motivo, para no mezclar filas que no deberían recibir la misma
acción) y `POST /others/dismiss-group` descarta un grupo entero de una vez,
con `expected_count` como defensa contra una relectura que cambie el grupo
entre que el usuario lo vio y confirmó el descarte.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

pytestmark = pytest.mark.asyncio


async def _archivo(db_session: AsyncSession, tenant: Tenant, filename: str) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=filename,
        s3_key=f"uploads/test/{uuid.uuid4()}/{filename}",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=512,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
    )
    db_session.add(record)
    await db_session.flush()
    return record


async def _filas(
    db_session: AsyncSession,
    tenant: Tenant,
    archivo: UploadedFile,
    *,
    n: int,
    context_label: str,
    suggested_entity: str | None = None,
    source: str = "ingestion",
    status: str = UNCLASSIFIED_STATUS_PENDING,
) -> list[UnclassifiedRecord]:
    rows = [
        UnclassifiedRecord(
            tenant_id=tenant.tenant_id,
            uploaded_file_id=archivo.id,
            source=source,
            context_label=context_label,
            headers=["detalle", "monto"],
            row_data={"detalle": f"fila {i}", "monto": "500"},
            suggested_entity=suggested_entity,
            status=status,
        )
        for i in range(n)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    return rows


class TestSummary:
    async def test_dos_archivos_dos_motivos_dan_cuatro_grupos(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        a = await _archivo(db_session, sample_tenant, "Ganancias.xlsx")
        b = await _archivo(db_session, sample_tenant, "libro_diario.xlsx")
        await _filas(db_session, sample_tenant, a, n=3, context_label="Ganancias")
        await _filas(db_session, sample_tenant, a, n=2, context_label="ganancias 2")
        await _filas(db_session, sample_tenant, b, n=4, context_label="Movimientos ambiguos")
        await _filas(db_session, sample_tenant, b, n=1, context_label="Fila sin fecha")

        resp = await client.get("/api/v1/others/summary", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        groups = resp.json()
        assert len(groups) == 4
        counts = {g["context_label"]: g["count"] for g in groups}
        assert counts == {
            "Ganancias": 3,
            "ganancias 2": 2,
            "Movimientos ambiguos": 4,
            "Fila sin fecha": 1,
        }
        filenames = {g["context_label"]: g["original_filename"] for g in groups}
        assert filenames["Ganancias"] == "Ganancias.xlsx"
        assert filenames["Movimientos ambiguos"] == "libro_diario.xlsx"

    async def test_mismo_archivo_y_motivo_pero_distinta_sugerencia_no_se_mezclan(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant, "mixto.xlsx")
        await _filas(
            db_session, sample_tenant, archivo, n=2, context_label="Hoja 1",
            suggested_entity="sale",
        )
        await _filas(
            db_session, sample_tenant, archivo, n=5, context_label="Hoja 1",
            suggested_entity="expense",
        )

        resp = await client.get("/api/v1/others/summary", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        groups = resp.json()
        assert len(groups) == 2
        by_entity = {g["suggested_entity"]: g["count"] for g in groups}
        assert by_entity == {"sale": 2, "expense": 5}

    async def test_registros_dismissed_no_aparecen_por_default(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant, "viejo.xlsx")
        await _filas(
            db_session, sample_tenant, archivo, n=3, context_label="Descartado",
            status=UNCLASSIFIED_STATUS_DISMISSED,
        )

        resp = await client.get("/api/v1/others/summary", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == []


class TestListFilters:
    async def test_filtra_por_archivo_y_motivo(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        a = await _archivo(db_session, sample_tenant, "a.xlsx")
        b = await _archivo(db_session, sample_tenant, "b.xlsx")
        await _filas(db_session, sample_tenant, a, n=2, context_label="Hoja X")
        await _filas(db_session, sample_tenant, b, n=3, context_label="Hoja X")

        resp = await client.get(
            "/api/v1/others",
            params={"uploaded_file_id": str(a.id), "context_label": "Hoja X"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 2
        assert all(r["uploaded_file_id"] == str(a.id) for r in rows)


class TestDismissGroup:
    async def test_descarta_el_grupo_entero_y_audita(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant, "Ganancias.xlsx")
        await _filas(db_session, sample_tenant, archivo, n=1840, context_label="Ganancias")
        # Otro grupo, no debe tocarse.
        await _filas(db_session, sample_tenant, archivo, n=2, context_label="Otro motivo")

        resp = await client.post(
            "/api/v1/others/dismiss-group",
            json={
                "uploaded_file_id": str(archivo.id),
                "source": "ingestion",
                "context_label": "Ganancias",
                "suggested_entity": None,
                "status": "PENDING",
                "expected_count": 1840,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dismissed"] == 1840

        remaining_pending = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id,
                    UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
                )
            )
        ).scalars().all()
        assert len(remaining_pending) == 2
        assert all(r.context_label == "Otro motivo" for r in remaining_pending)

        audit = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "UNCLASSIFIED_GROUP_DISMISSED"
                )
            )
        ).scalar_one()
        assert audit.decision_data["count"] == 1840
        assert audit.decision_data["context_label"] == "Ganancias"
        assert audit.actor_user_id is not None

    async def test_grupo_cambiado_desde_el_snapshot_rechaza_con_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant, "archivo.xlsx")
        await _filas(db_session, sample_tenant, archivo, n=5, context_label="Motivo")

        resp = await client.post(
            "/api/v1/others/dismiss-group",
            json={
                "uploaded_file_id": str(archivo.id),
                "source": "ingestion",
                "context_label": "Motivo",
                "status": "PENDING",
                "expected_count": 3,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "GROUP_CHANGED"
        assert resp.json()["detail"]["current"] == 5

        # Nada se descartó.
        still_pending = (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id,
                    UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
                )
            )
        ).scalars().all()
        assert len(still_pending) == 5

    async def test_grupo_sin_archivo_ni_motivo_matchea_por_null(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        """Filas sin `uploaded_file_id`/`context_label` (carga manual) forman su
        propio grupo — `NULL` se trata como valor de grupo, nunca se confunde
        con "cualquier archivo"."""
        record = UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            uploaded_file_id=None,
            source="chat",
            context_label=None,
            row_data={"detalle": "sin archivo"},
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/others/dismiss-group",
            json={
                "uploaded_file_id": None,
                "source": "chat",
                "context_label": None,
                "status": "PENDING",
                "expected_count": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dismissed"] == 1
