"""Tests de la relectura de archivos (reread_service).

Mockean la descarga de S3 (monkeypatch ``S3Client.download``) con bytes de un CSV
de fixture. Cubren:

  - Relectura re-importa un no-editado y PRESERVA un registro con
    ``has_user_edits=True`` (no se duplica ni se modifica).
  - Filas nuevas se insertan; filas sin cambios no duplican.
  - ``undo`` restaura el estado previo (des-anula voids, elimina inserts).
  - ``dry_run`` (preview) no escribe nada y cuenta bien.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    _load_import_fingerprints,
    _persist_import_fingerprints,
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.integrations.s3 import S3Client
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

# CSV original (2 filas). El reread vuelve a leer ESTE contenido salvo cuando el
# test pide una variante (fila extra).
_CSV_BASE = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,Panaderia Norte\n"
)
_CSV_WITH_NEW_ROW = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,Panaderia Norte\n"
    b"2026-01-07,Yerba Mate,2200,Almacen Central\n"
)


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco Test",
        display_name="Kiosco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.commit()
    return t


def _patch_s3(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return content

    monkeypatch.setattr(S3Client, "download", _fake_download)


async def _make_file(
    session: AsyncSession, tenant: Tenant, content: bytes
) -> UploadedFile:
    summary = parse_uploaded_content(content, "text/csv", "gastos.csv")
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="gastos.csv",
        s3_key=f"tenants/{tenant.tenant_id}/gastos.csv",
        content_type="text/csv",
        size_bytes=len(content),
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": default_confirmed_fields(summary),
        },
    )
    session.add(f)
    await session.commit()
    return f


async def _initial_import(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, content: bytes
) -> None:
    summary = parse_uploaded_content(content, "text/csv", "gastos.csv")
    await insert_confirmed_data(
        session,
        tenant.tenant_id,
        summary,
        default_confirmed_fields(summary),
        source="ingestion",
        uploaded_file_id=file.id,
    )
    await session.commit()


async def _active_expenses(
    session: AsyncSession, tenant: Tenant, file: UploadedFile
) -> list[ExpenseEntry]:
    res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant.tenant_id,
            ExpenseEntry.source_upload_id == file.id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    return list(res.scalars().all())


@pytest.mark.asyncio
async def test_initial_import_sets_source_row_ref(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    expenses = await _active_expenses(db_session, tenant, file)
    assert len(expenses) == 2
    for e in expenses:
        assert e.source_row_ref is not None
        assert len(e.source_row_ref) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_reread_preserves_edited_and_reimports_others(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    expenses = await _active_expenses(db_session, tenant, file)
    assert len(expenses) == 2

    # Marcar UNO como editado a mano (modificar monto + flag).
    edited = expenses[0]
    edited_id = edited.id
    edited.has_user_edits = True
    edited.amount = edited.amount + 999  # edición manual
    edited_amount = edited.amount
    edited_ref = edited.source_row_ref
    non_edited_ref = expenses[1].source_row_ref
    await db_session.commit()

    # Apply reread.
    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.preserved == 1
    assert result.voided == 1  # el no-editado
    assert result.inserted == 1  # se re-creó corregido

    after = await _active_expenses(db_session, tenant, file)
    # Sigue habiendo 2 activos: el editado preservado + el re-importado.
    assert len(after) == 2

    by_id = {e.id: e for e in after}
    # El editado NO se tocó: mismo id, mismo monto editado, sigue marcado.
    assert edited_id in by_id
    preserved = by_id[edited_id]
    assert preserved.has_user_edits is True
    assert preserved.amount == edited_amount

    # El no-editado fue reemplazado por uno nuevo con el mismo source_row_ref.
    refs = {e.source_row_ref for e in after}
    assert edited_ref in refs
    assert non_edited_ref in refs
    # No hay duplicados del ref editado.
    assert sum(1 for e in after if e.source_row_ref == edited_ref) == 1


@pytest.mark.asyncio
async def test_reread_imports_new_rows_no_duplicate(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Import inicial con 2 filas.
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)
    assert len(await _active_expenses(db_session, tenant, file)) == 2

    # Ahora el archivo en S3 tiene una fila extra (re-lectura corregida).
    _patch_s3(monkeypatch, _CSV_WITH_NEW_ROW)
    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    # 2 no-editados voldados + re-importados, 1 fila nueva.
    assert result.voided == 2
    assert result.new == 1
    after = await _active_expenses(db_session, tenant, file)
    assert len(after) == 3  # sin duplicados

    # Total de gastos (incluyendo voldados) en la DB: los 2 voldados + 3 activos.
    total = await db_session.scalar(
        select(func.count()).select_from(ExpenseEntry).where(
            ExpenseEntry.source_upload_id == file.id
        )
    )
    assert total == 5


@pytest.mark.asyncio
async def test_reread_idempotent_no_change(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    # Sin ediciones ni filas nuevas: 2 voldados + 2 re-importados, 0 nuevas.
    assert result.new == 0
    assert result.voided == 2
    after = await _active_expenses(db_session, tenant, file)
    assert len(after) == 2


@pytest.mark.asyncio
async def test_preview_does_not_write(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    before = await _active_expenses(db_session, tenant, file)
    before_ids = {e.id for e in before}
    before_total = await db_session.scalar(
        select(func.count()).select_from(ExpenseEntry)
    )

    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.to_void == 2
    assert preview.preserved == 0
    assert preview.new == 0

    # Nada cambió en la DB.
    after = await _active_expenses(db_session, tenant, file)
    assert {e.id for e in after} == before_ids
    after_total = await db_session.scalar(
        select(func.count()).select_from(ExpenseEntry)
    )
    assert after_total == before_total
    for e in after:
        assert e.voided_at is None


@pytest.mark.asyncio
async def test_preview_counts_edited_and_new(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    expenses = await _active_expenses(db_session, tenant, file)
    expenses[0].has_user_edits = True
    await db_session.commit()

    # S3 ahora tiene una fila extra.
    _patch_s3(monkeypatch, _CSV_WITH_NEW_ROW)
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.preserved == 1
    assert preview.to_void == 1  # solo el no-editado
    assert preview.new == 1  # la fila extra

    # Preview no escribió: el editado sigue sin void.
    after = await _active_expenses(db_session, tenant, file)
    assert len(after) == 2


@pytest.mark.asyncio
async def test_undo_restores_previous_state(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    original = await _active_expenses(db_session, tenant, file)
    original_ids = {e.id for e in original}
    original_amounts = sorted(str(e.amount) for e in original)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()
    run_id = result.run_id

    # Tras el apply, los ids originales fueron voldados y hay ids nuevos.
    after_apply = await _active_expenses(db_session, tenant, file)
    assert {e.id for e in after_apply}.isdisjoint(original_ids)

    # Undo.
    undo = await reread_service.undo_reread(db_session, run_id, tenant.tenant_id)
    await db_session.commit()

    assert undo["status"] == "REVERTED"
    assert undo["restored"] == 2  # los 2 originales des-anulados
    assert undo["removed"] == 2  # los 2 insertados eliminados

    restored = await _active_expenses(db_session, tenant, file)
    assert {e.id for e in restored} == original_ids
    assert sorted(str(e.amount) for e in restored) == original_amounts
    for e in restored:
        assert e.voided_at is None
        assert e.void_reason is None


@pytest.mark.asyncio
async def test_reread_wrong_tenant_not_found(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    other_tenant = uuid.uuid4()
    with pytest.raises(FileNotFoundError):
        await reread_service.preview_reread(db_session, file.id, other_tenant)


@pytest.mark.asyncio
async def test_persist_import_fingerprints_is_idempotent(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """``_persist_import_fingerprints`` usa ON CONFLICT DO NOTHING: persistir un set
    que solapa con huellas ya existentes NO levanta IntegrityError ni aborta la
    transacción (la protección que reemplaza al begin_nested por fila)."""
    await _persist_import_fingerprints(db_session, tenant.tenant_id, {"aaa", "bbb"})
    await db_session.commit()
    assert await _load_import_fingerprints(db_session, tenant.tenant_id) == {"aaa", "bbb"}

    # "bbb" ya existe → no debe romper; "ccc" se agrega.
    await _persist_import_fingerprints(db_session, tenant.tenant_id, {"bbb", "ccc"})
    await db_session.commit()
    assert await _load_import_fingerprints(db_session, tenant.tenant_id) == {
        "aaa",
        "bbb",
        "ccc",
    }


@pytest.mark.asyncio
async def test_preview_returns_before_after_sample(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El preview (estimado en memoria) devuelve un sample antes/después: voids
    con `before`, y filas nuevas con `after` y sin `before`."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    # S3 ahora tiene una fila extra → debe aparecer como "nuevo" en el sample.
    _patch_s3(monkeypatch, _CSV_WITH_NEW_ROW)
    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)

    assert preview.sample_changes, "el preview debe traer un sample de cambios"
    actions = {c["action"] for c in preview.sample_changes}
    # Hay voids (no-editados) y al menos un nuevo (la fila extra).
    assert "void" in actions
    assert "new" in actions
    new_items = [c for c in preview.sample_changes if c["action"] == "new"]
    assert new_items[0]["before"] is None
    assert new_items[0]["after"] is not None
    void_items = [c for c in preview.sample_changes if c["action"] == "void"]
    assert void_items[0]["before"] is not None


@pytest.mark.asyncio
async def test_batch_fingerprints_preloaded_and_idempotent(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El camino batch (anti-N+1) precarga las huellas y dedupea en memoria.

    Tras el import inicial, ``_load_import_fingerprints`` devuelve las 2 huellas
    registradas; reimportar el MISMO contenido con el mismo ``uploaded_file_id``
    no inserta filas nuevas (idempotencia vía el set precargado, sin SELECT/
    savepoint por fila)."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    # El helper batch ve las huellas que registró el import inicial (2 filas).
    fps = await _load_import_fingerprints(db_session, tenant.tenant_id)
    assert len(fps) == 2

    # Reimportar el mismo archivo: 0 filas nuevas (dedup por el set precargado).
    summary = parse_uploaded_content(_CSV_BASE, "text/csv", "gastos.csv")
    counts = await insert_confirmed_data(
        db_session,
        tenant.tenant_id,
        summary,
        default_confirmed_fields(summary),
        source="ingestion",
        uploaded_file_id=file.id,
    )
    await db_session.commit()

    assert counts["gastos"] == 0
    assert len(await _active_expenses(db_session, tenant, file)) == 2
