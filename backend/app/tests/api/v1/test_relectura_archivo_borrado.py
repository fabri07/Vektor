"""La relectura y su «Deshacer» respetan el borrado del archivo (I3-1, I4-1, I4-2).

Revisión profunda 2026-10-24. El borrado de un archivo (F11) anula sus ventas y
gastos y devuelve el stock, pero la relectura cargaba el archivo sin mirar
``deleted_at``:

- una sesión abierta en otra pestaña se aplicaba igual y reimportaba el archivo
  ENTERO —las huellas ya estaban liberadas—, con ``source_upload_id`` de un
  archivo que no figura en la lista y que ya no se puede volver a borrar (404);
- un run en la cola del worker hacía lo mismo, en background;
- «Deshacer relectura» des-anulaba los originales que la relectura había
  anulado y el borrado no tocó.

Ahora los tres caminos responden 409 ``REREAD_FILE_DELETED`` y no escriben nada;
el worker cierra el run como FAILED con motivo ``archivo_borrado``. Lo que corre
con candados de verdad (el borrado en el medio de un apply) está en
``integration/test_relectura_archivo_borrado_pg.py``.
"""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.services import reread_service
from app.application.services.file_deletion_service import (
    build_master_details,
    record_import_ledger,
    revert_file_data,
    snapshot_masters_before_import,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.ingestion_version import INGESTION_VERSION
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.conftest import add_business_profile
from app.tests.integration import _reread_scenario as esc

_FILAS = [
    esc.fila(5, esc.PRODUCTO, "2", "2400"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]
#: La relectura trae lo mismo con una cantidad corregida: reemplaza, no pierde.
_RELEIDO = [
    esc.fila(5, esc.PRODUCTO, "4", "4800"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker en tests: ver el mismo fixture en ``test_reread_file.py``."""


@pytest.fixture(autouse=True)
def _s3_con_la_relectura(monkeypatch: pytest.MonkeyPatch) -> None:
    """El crudo «vuelto a leer» es ``_RELEIDO``; el etag no cambia."""

    async def _fresh(_file: UploadedFile, _s3: S3Client) -> dict[str, Any]:
        return esc.summary(_RELEIDO)

    async def _head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"fijo"', "size": 1, "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(reread_service, "_fresh_summary", _fresh)
    monkeypatch.setattr(S3Client, "head", _head)


async def _importar(s: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Archivo de compras confirmado como el confirm real: efectos + ledger."""
    file_id = uuid.uuid4()
    s.add(
        UploadedFile(
            id=file_id,
            tenant_id=tenant_id,
            original_filename="compras.xlsx",
            s3_key=f"tenants/{tenant_id}/compras.xlsx",
            content_type="application/vnd.ms-excel",
            size_bytes=1,
            purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json=esc.summary(_FILAS),
            ingestion_version=INGESTION_VERSION,
        )
    )
    await s.commit()
    antes_c, antes_p = await snapshot_masters_before_import(s, tenant_id)
    counts = await insert_confirmed_data(
        s,
        tenant_id,
        esc.summary(_FILAS),
        {"gastos": True},
        return_details=True,
        uploaded_file_id=file_id,
    )
    detalles = counts.pop("product_details", []) or []
    await record_import_ledger(
        s,
        tenant_id=tenant_id,
        file_id=file_id,
        product_details=detalles,
        master_details=await build_master_details(s, counts, antes_c, antes_p),
    )
    await s.commit()
    return file_id


async def _vivos(s: AsyncSession, file_id: uuid.UUID) -> tuple[int, int]:
    """``(gastos, movimientos de stock)`` vivos colgando del archivo."""
    gastos = await s.scalar(
        select(func.count())
        .select_from(ExpenseEntry)
        .where(ExpenseEntry.source_upload_id == file_id, ExpenseEntry.voided_at.is_(None))
    )
    movimientos = await s.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.source_upload_id == file_id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    return int(gastos or 0), int(movimientos or 0)


async def _borrar(client: AsyncClient, headers: dict[str, Any], file_id: uuid.UUID) -> None:
    r = await client.delete(f"/api/v1/ingestion/files/{file_id}?confirm=true", headers=headers)
    assert r.status_code == 200, r.text


def _es_archivo_borrado(r: Any) -> None:
    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert detalle["code"] == reread_service.REREAD_FILE_DELETED
    assert "se eliminó" in detalle["message"]


async def _runs_del_archivo(s: AsyncSession, file_id: uuid.UUID) -> list[DataRepairRun]:
    s.expire_all()
    runs = (
        await s.execute(
            select(DataRepairRun).where(
                DataRepairRun.repair_type == reread_service.REPAIR_TYPE_REREAD
            )
        )
    ).scalars()
    return [r for r in runs if (r.details_json or {}).get("file_id") == str(file_id)]


# ── Preview ────────────────────────────────────────────────────────────────────


async def test_releer_un_archivo_borrado_da_409_y_no_abre_sesion(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    fid = await _importar(db_session, sample_tenant.tenant_id)
    assert await _vivos(db_session, fid) == (2, 2)
    await _borrar(client, auth_headers, fid)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)

    _es_archivo_borrado(r)
    assert await _runs_del_archivo(db_session, fid) == [], "no queda sesión abierta"
    assert await _vivos(db_session, fid) == (0, 0)


# ── Apply (I3-1 caso A / I4-1): la sesión de otra pestaña ──────────────────────


async def test_aplicar_una_sesion_abierta_despues_del_borrado_da_409_y_no_resucita_nada(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.jobs import reread_worker

    fid = await _importar(db_session, sample_tenant.tenant_id)
    pre = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    assert pre.status_code == 200, pre.text
    body = pre.json()
    assert body["status"] == "READY_TO_APPLY"

    # La otra pestaña borra el archivo.
    await _borrar(client, auth_headers, fid)

    # Retomar la sesión (recargar la página) tampoco la deja seguir.
    _es_archivo_borrado(
        await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    )
    sin_worker = unittest.mock.patch(
        "app.application.services.reread_diagnostics_service.check_ingestion_workers_available",
        return_value=None,
    )
    with sin_worker, unittest.mock.patch.object(reread_worker.reread_apply, "delay") as delay:
        r = await client.post(
            f"/api/v1/ingestion/files/{fid}/reread/apply",
            headers=auth_headers,
            json={"run_id": body["run_id"], "draft_version": body["draft_version"]},
        )
    _es_archivo_borrado(r)
    delay.assert_not_called()
    (run,) = await _runs_del_archivo(db_session, fid)
    assert run.status == "READY_TO_APPLY", "no se encoló"
    assert await _vivos(db_session, fid) == (0, 0)


# ── El worker (I4-1): el run ya estaba en la cola cuando se borró ──────────────


class _EngineFalso:
    async def dispose(self) -> None:
        return None


async def test_el_worker_no_aplica_un_run_cuyo_archivo_se_borro_en_la_cola(
    isolated_db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base aislada: el worker commitea y hace rollback de verdad."""
    from app.jobs import reread_worker

    factory = async_sessionmaker(isolated_db_engine, expire_on_commit=False)
    monkeypatch.setattr(
        reread_worker, "_build_async_session", lambda _url: (_EngineFalso(), factory)
    )
    tid = uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T", currency="ARS"))
        await s.flush()
        await add_business_profile(s, tid)
        await s.commit()
        fid = await _importar(s, tid)

    async with factory() as s:
        run, fresh = await reread_service.start_or_resume_preview_session(s, fid, tid)
        await reread_service.preview_reread(s, fid, tid, fresh_override=fresh, run=run)
        reread_service.mark_session_ready_to_apply(run)
        run = await reread_service.start_background_apply(s, fid, tid, existing_run=run)
        await s.commit()
        run_id = run.id

    # El borrado, como el endpoint: marca el archivo y revierte en la misma
    # transacción.
    async with factory() as s:
        await s.execute(
            update(UploadedFile).where(UploadedFile.id == fid).values(deleted_at=func.now())
        )
        await revert_file_data(s, fid, tid)
        await s.commit()
        assert await _vivos(s, fid) == (0, 0)

    salida = await reread_worker.aplicar_relectura(str(run_id), str(fid), str(tid))

    assert salida["status"] == "FAILED"
    async with factory() as s:
        assert await _vivos(s, fid) == (0, 0), "el worker resucitó el archivo borrado"
        cerrado = await s.get(DataRepairRun, run_id)
        assert cerrado is not None and cerrado.status == "FAILED"
        assert (cerrado.details_json or {})["reason"] == reread_service.MOTIVO_ARCHIVO_BORRADO
        assert "se eliminó" in (cerrado.details_json or {})["error"]


# ── Deshacer (I3-1 caso B / I4-2) ──────────────────────────────────────────────


async def test_deshacer_la_relectura_de_un_archivo_borrado_da_409_y_no_desanula_nada(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    resultado = await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    anulados_por_la_relectura = await db_session.scalar(
        select(func.count())
        .select_from(ExpenseEntry)
        .where(ExpenseEntry.voided_by_repair_run_id == resultado.run_id)
    )
    assert anulados_por_la_relectura, "la relectura tiene que haber anulado originales"

    await _borrar(client, auth_headers, fid)
    assert await _vivos(db_session, fid) == (0, 0)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    _es_archivo_borrado(r)
    assert await _vivos(db_session, fid) == (0, 0), "el deshacer des-anuló los originales"
    db_session.expire_all()
    run = await db_session.get(DataRepairRun, resultado.run_id)
    assert run is not None and run.status == "APPLIED"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ExpenseEntry)
            .where(ExpenseEntry.voided_by_repair_run_id == resultado.run_id)
        )
        == anulados_por_la_relectura
    )


async def test_con_el_archivo_vivo_releer_y_deshacer_siguen_andando(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El control: lo mismo sin borrar el archivo sí escribe."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    pre = await client.post(f"/api/v1/ingestion/files/{fid}/reread/preview", headers=auth_headers)
    assert pre.status_code == 200, pre.text
    run_id = pre.json()["run_id"]
    await client.post(f"/api/v1/ingestion/files/{fid}/reread/{run_id}/cancel", headers=auth_headers)

    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _vivos(db_session, fid) == (2, 2)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERTED"
    assert await _vivos(db_session, fid) == (2, 2)
