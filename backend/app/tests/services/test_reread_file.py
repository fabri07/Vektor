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
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    RISK_REF_KEY,
    _load_import_fingerprints,
    _persist_import_fingerprints,
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
from app.integrations.s3 import S3Client
from app.persistence.models.customer import Customer
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    REREAD_STATUS_APPLIED,
    REREAD_STATUS_AUTO_APPLIED,
    REREAD_STATUS_NEEDS_REVIEW,
    UploadedFile,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.tests.conftest import add_business_profile


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """El reread encola el recálculo de score (`_trigger_score` → `.delay()`).

    Sin broker en tests, kombu reintenta la conexión con backoff: ~4,75s de
    `time.sleep` POR llamada, ~38s en los tests que aplican y deshacen varias
    veces (medido con cProfile: 76 sleeps = 38,2s de los ~40s del test). El
    servicio ya traga el error (fail-safe), así que ningún assert dependía del
    encolado real. Mismo patrón que `test_file_deletion_end_to_end.py`.
    """


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
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id)
    await db_session.commit()
    return t


def _patch_s3(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return content

    async def _fake_head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"fake"', "size": len(content), "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(S3Client, "download", _fake_download)
    monkeypatch.setattr(S3Client, "head", _fake_head)


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


async def test_reread_wrong_tenant_not_found(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    other_tenant = uuid.uuid4()
    with pytest.raises(FileNotFoundError):
        await reread_service.preview_reread(db_session, file.id, other_tenant)


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


async def test_background_apply_run_status_and_guard(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El apply en background: ``start_background_apply`` deja el run QUEUED, el
    worker lo ejecuta con ``apply_reread(run=...)`` dejándolo APPLIED, y
    ``get_reread_run`` lo devuelve. El guard bloquea una 2ª relectura concurrente."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    run = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    assert run.status == "QUEUED"
    await db_session.commit()

    # Guard: una 2ª relectura mientras hay una QUEUED reciente → ValueError.
    with pytest.raises(ValueError, match="en curso"):
        await reread_service.start_background_apply(
            db_session, file.id, tenant.tenant_id
        )

    # El "worker" ejecuta el apply reusando el run pre-creado.
    result = await reread_service.apply_reread(
        db_session, file.id, tenant.tenant_id, run=run
    )
    await db_session.commit()
    assert result.voided == 2  # los 2 no-editados del import inicial

    fetched = await reread_service.get_reread_run(
        db_session, run.id, tenant.tenant_id, file.id
    )
    assert fetched is not None
    assert fetched.status == "APPLIED"
    assert (fetched.details_json or {}).get("voided") == 2

    # Otro tenant no ve el run.
    assert (
        await reread_service.get_reread_run(db_session, run.id, uuid.uuid4(), file.id)
        is None
    )


async def test_stale_running_run_gets_marked_failed_not_left_forever(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caso real (ASTERIA): un run que nunca salió de QUEUED (el worker nunca
    lo tomó) antes solo se IGNORABA al decidir si bloquear una relectura
    nueva — quedaba en ese estado para siempre en la auditoría. Ahora
    ``start_background_apply`` lo cierra como FAILED."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    stale = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    assert stale.status == "QUEUED"
    # Envejecerlo más allá del umbral de "colgado" sin tocar nada más.
    # `updated_at` (no `created_at`): el guard mide desde la última transición
    # real de estado, no desde que se creó la sesión de revisión.
    stale.updated_at = datetime.now(UTC) - timedelta(
        seconds=reread_service._STALE_RUNNING_AFTER_SECONDS + 1
    )
    await db_session.commit()

    # Un segundo intento ya NO debe bloquearse por el guard — y de paso cierra
    # el run viejo, en vez de solo ignorarlo.
    fresh = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()
    assert fresh.id != stale.id

    await db_session.refresh(stale)
    assert stale.status == "FAILED"
    assert stale.completed_at is not None
    assert (stale.details_json or {})["reason"] == "stale_never_picked_up"


async def test_get_reread_run_rechaza_file_id_incompatible(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El polling con el ``file_id`` de OTRO archivo (aunque el ``run_id`` sea
    válido y del mismo tenant) debe devolver None — evita que la respuesta
    mezcle el ``file_id`` de la URL con un run que en realidad pertenece a
    otro archivo."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file_a = await _make_file(db_session, tenant, _CSV_BASE)
    file_b = await _make_file(db_session, tenant, _CSV_BASE)

    run = await reread_service.start_background_apply(db_session, file_a.id, tenant.tenant_id)
    await db_session.commit()

    # Pedirlo con el file_id de OTRO archivo debe devolver None (404 en el endpoint).
    result = await reread_service.get_reread_run(db_session, run.id, tenant.tenant_id, file_b.id)
    assert result is None

    # Con el file_id correcto, sí lo devuelve.
    result_ok = await reread_service.get_reread_run(
        db_session, run.id, tenant.tenant_id, file_a.id
    )
    assert result_ok is not None


# ── Inventario: la relectura no debe duplicar movimientos ni inflar el stock ────
#
# El camino compra→stock es incremental. El bug: ``_reconcile`` reimportaba sin
# revertir los ``InventoryMovement`` del import previo, así que cada relectura
# duplicaba movimientos e inflaba ``stock_units`` (8,5x en prod). El fix voidea la
# lectura anterior del lado inventario (``void_movement``) antes de reimportar.
#
# La creación real de movimientos con ``source_upload_id`` la cablea A2 en
# ``ingestion_import_service._record_stock_movement`` (tarea aparte). Acá se STUBEA
# ``insert_confirmed_data`` para simular ese import post-A2 (crea un movimiento
# etiquetado + suma stock), aislando y probando la mitad de la relectura.

_STOCK_QTY = 10


async def _seed_inventory(
    session: AsyncSession, tenant: Tenant, file: UploadedFile
) -> uuid.UUID:
    """Producto + balance + movimiento del import PREVIO (etiquetado con el archivo)."""
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca Cola",
        sale_price_ars=Decimal("1500"),
        unit_cost_ars=Decimal("1000"),
        stock_units=_STOCK_QTY,
    )
    session.add(product)
    session.add(
        InventoryBalance(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            current_qty=_STOCK_QTY,
        )
    )
    session.add(
        InventoryMovement(
            tenant_id=tenant.tenant_id,
            product_id=product.id,
            movement_type="purchase",
            qty=_STOCK_QTY,
            source_upload_id=file.id,
        )
    )
    await session.commit()
    return product.id


def _patch_import_creates_movement(
    monkeypatch: pytest.MonkeyPatch, product_id: uuid.UUID
) -> None:
    """Stub de ``insert_confirmed_data``: simula el import de compra post-A2 —
    crea UN movimiento etiquetado (qty=+_STOCK_QTY, ``source_upload_id``) y suma al
    stock/balance, tal como hará ``_record_stock_movement`` cuando A2 lo cablee."""

    async def _fake_insert(
        session: AsyncSession,
        tenant_id: uuid.UUID,
        fresh: dict,  # noqa: ARG001
        confirmed_fields: dict,  # noqa: ARG001
        *,
        source: str,  # noqa: ARG001
        uploaded_file_id: uuid.UUID | None = None,
        **kwargs: object,  # tolera kwargs nuevos (p.ej. stock_treatment)  # noqa: ARG001
    ) -> dict[str, int]:
        product = await session.get(Product, product_id)
        assert product is not None
        product.stock_units += _STOCK_QTY
        session.add(
            InventoryMovement(
                tenant_id=tenant_id,
                product_id=product_id,
                movement_type="purchase",
                qty=_STOCK_QTY,
                source_upload_id=uploaded_file_id,
            )
        )
        balance = (
            await session.execute(
                select(InventoryBalance).where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id == product_id,
                )
            )
        ).scalar_one_or_none()
        if balance is not None:
            balance.current_qty += _STOCK_QTY
        await session.flush()
        return {"ventas": 0, "gastos": 0, "productos": 0}

    monkeypatch.setattr(reread_service, "insert_confirmed_data", _fake_insert)


async def _active_movement_state(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, product_id: uuid.UUID
) -> tuple[int, int]:
    """(# de movimientos vivos del archivo, stock_units del producto)."""
    active = await session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.tenant_id == tenant.tenant_id,
            InventoryMovement.source_upload_id == file.id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    product = await session.get(Product, product_id)
    assert product is not None
    return int(active or 0), product.stock_units


async def test_reread_does_not_duplicate_inventory_movements(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releer un archivo de compras N veces deja EL MISMO estado de inventario:
    1 movimiento vivo y el stock original. Sin el fix, cada relectura sumaría otro
    movimiento vivo (+_STOCK_QTY al stock)."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    product_id = await _seed_inventory(db_session, tenant, file)
    _patch_import_creates_movement(monkeypatch, product_id)

    # Estado base tras el import previo.
    assert await _active_movement_state(db_session, tenant, file, product_id) == (
        1,
        _STOCK_QTY,
    )

    # Relectura #1.
    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()
    assert await _active_movement_state(db_session, tenant, file, product_id) == (
        1,
        _STOCK_QTY,
    )

    # Relectura #2 → idempotente (NO se duplica ni infla el stock).
    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()
    assert await _active_movement_state(db_session, tenant, file, product_id) == (
        1,
        _STOCK_QTY,
    )

    # El ledger insert-only conserva los voidados: 1 (previo, voidado) + 2 (uno por
    # reread; el de #1 quedó voidado y el de #2 vivo) = 3 filas totales, pero solo 1
    # cuenta para el stock.
    total = await db_session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(InventoryMovement.source_upload_id == file.id)
    )
    assert total == 3


async def test_undo_reread_restores_inventory(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deshacer una relectura deja el inventario como estaba: el movimiento previo
    vuelve a estar vivo, el insertado por el reread queda voidado y el stock es el
    original."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    product_id = await _seed_inventory(db_session, tenant, file)
    _patch_import_creates_movement(monkeypatch, product_id)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    # Tras el apply: 1 movimiento vivo (el del reimport) y stock intacto.
    assert await _active_movement_state(db_session, tenant, file, product_id) == (
        1,
        _STOCK_QTY,
    )

    await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()

    # Sigue habiendo exactamente 1 movimiento vivo y el stock original: el previo
    # se des-anuló y el insertado por el reread quedó voidado.
    active, stock = await _active_movement_state(db_session, tenant, file, product_id)
    assert active == 1
    assert stock == _STOCK_QTY


# ── F8b (Task 5): decisiones de riesgo de columnas en la relectura ──────────────

# CSV donde la 2ª fila tiene ``monto`` vacío → ruteada a "Otros" por la decisión
# de riesgo. La variante FIXED la corrige (el reread la re-importa como gasto).
_CSV_RISK_BAD = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,Panaderia Norte\n"
)
_CSV_RISK_FIXED = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,Panaderia Norte\n"
)
_RISK_DECISION = {
    "context_id": "table",
    "source_column": "monto",
    "target_field": "amount",
    "action": "route_affected_rows_to_others",
}


async def _risk_records(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, status: str
) -> list[UnclassifiedRecord]:
    res = await session.execute(
        select(UnclassifiedRecord).where(
            UnclassifiedRecord.tenant_id == tenant.tenant_id,
            UnclassifiedRecord.uploaded_file_id == file.id,
            UnclassifiedRecord.status == status,
        )
    )
    return list(res.scalars().all())


async def _first_confirm_with_risk(
    session: AsyncSession, tenant: Tenant, content: bytes
) -> UploadedFile:
    """Simula el confirm original con una decisión ``route_affected_rows_to_others``
    sobre ``monto``: persiste la decisión en el summary (como hace el confirm),
    importa solo las filas válidas y captura la afectada en "Otros" (Task 3/4)."""
    from app.application.services.column_risk import apply_column_risk_decisions
    from app.application.services.ingestion_import_service import (
        _capture_column_risk_rows,
    )
    from app.schemas.ingestion import ColumnRiskDecision

    summary = parse_uploaded_content(content, "text/csv", "gastos.csv")
    confirmed = default_confirmed_fields(summary)
    file = UploadedFile(
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
            "confirmed_fields": confirmed,
            # El confirm persiste las decisiones efectivas (Task 5) para el reread.
            "column_risk_decisions": [_RISK_DECISION],
        },
    )
    session.add(file)
    await session.commit()

    applied = apply_column_risk_decisions(
        summary, [ColumnRiskDecision(**_RISK_DECISION)], {}
    )
    # Importar solo las válidas (el bucket ya sin las afectadas).
    await insert_confirmed_data(
        session,
        tenant.tenant_id,
        applied.summary,
        confirmed,
        source="ingestion",
        uploaded_file_id=file.id,
    )
    # Capturar las afectadas en "Otros" (con __risk_ref__ de correlación).
    for cid, rows_by_idx in applied.routed_rows.items():
        if rows_by_idx:
            await _capture_column_risk_rows(
                session,
                tenant.tenant_id,
                file.id,
                cid,
                applied.routed_entity.get(cid) or "otros",
                rows_by_idx,
                source="ingestion",
            )
    await session.commit()
    return file


async def test_reread_fila_corregida_importa_y_resuelve_otros(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fila antes ruteada a Otros por riesgo, ahora CORREGIDA en el reread: se
    importa como gasto normal Y su UnclassifiedRecord previo queda resuelto
    (DISMISSED), sin duplicar."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)

    # Estado inicial: 1 gasto (la válida) + 1 Otros de riesgo pendiente (la mala).
    assert len(await _active_expenses(db_session, tenant, file)) == 1
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 1

    # Reread con la fila corregida.
    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    # Ahora 2 gastos activos (la válida reimportada + la corregida).
    assert len(await _active_expenses(db_session, tenant, file)) == 2
    # El Otros previo quedó resuelto: 0 pendientes, exactamente 1 DISMISSED
    # (no se duplicó ni quedó vivo).
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 0
    dismissed = await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_DISMISSED)
    assert len(dismissed) == 1
    assert dismissed[0].resolved_at is not None


async def test_reread_conserva_decision_y_no_duplica_otros(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fila que SIGUE mal en el reread: la decisión se conserva (no se importa) y
    su Otros previo no se duplica (huella risk:* idempotente)."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)
    assert len(await _active_expenses(db_session, tenant, file)) == 1
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 1

    # Reread con el MISMO contenido (la fila sigue con monto vacío).
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    # La decisión se honra: la fila mala NO se importó (sigue 1 gasto) y su Otros
    # no se duplicó (sigue 1 pendiente).
    assert len(await _active_expenses(db_session, tenant, file)) == 1
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 1


async def test_build_reread_sheets_flags_required_risk_as_requiere_revision(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """F-RR Fase 8 (backend): una hoja con un requerido (``amount``) que tiene
    una fila afectada (monto vacío) queda ``requiere_revision`` — no
    ``completa`` (hay riesgo real sin resolver) ni ``ambigua`` (una sola
    acción legal posible, el requerido no tiene columna de reemplazo)."""
    fresh = parse_uploaded_content(_CSV_RISK_BAD, "text/csv", "gastos.csv")
    draft = {
        "column_mappings": [
            {"source_column": "fecha", "target_field": "expense_date"},
            {"source_column": "producto", "target_field": "ignore"},
            {"source_column": "monto", "target_field": "amount"},
            {"source_column": "proveedor", "target_field": "supplier_name"},
        ],
        "context_entities": {"table": "expense"},
        "confirmed_fields": {"gastos": True},
        "context_confirmed": {},
    }

    sheets, risk = await reread_service.build_reread_sheets(
        db_session, tenant.tenant_id, fresh, draft, {"gastos": True}
    )

    assert len(sheets) == 1
    sheet = sheets[0]
    assert sheet["context_id"] == "table"
    assert sheet["entity_type"] == "expense"
    assert sheet["row_count"] == 2
    # Las 4 columnas ya tienen mapeo explícito en el borrador (una "ignore").
    assert sheet["columns_mapped"] == 3  # expense_date, amount, supplier_name
    assert sheet["columns_pending"] == 0
    assert sheet["status"] == "requiere_revision"
    assert any(r["source_column"] == "monto" for r in risk)


async def test_reread_reapplied_outcome_bumps_version_and_status(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un archivo F8b+ (con ``column_risk_decisions`` guardadas) resuelve a
    ``REAPPLIED`` — el ÚNICO outcome que bumpea ``ingestion_version`` y deja
    ``reread_status=APPLIED`` (no ``NEEDS_REVIEW``)."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "REAPPLIED"
    assert file.ingestion_version == INGESTION_VERSION
    assert file.reread_status == REREAD_STATUS_APPLIED
    assert file.reread_summary is not None
    assert file.reread_summary["outcome"] == "REAPPLIED"
    assert file.reread_summary["algorithm_version"] == INGESTION_VERSION


async def test_reread_user_reviewed_outcome_wins_over_stored_decisions(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-RR Fase 6: un borrador de sesión con ``column_risk_decisions`` propias
    resuelve a ``USER_REVIEWED`` — más autoritativo que ``REAPPLIED`` (que
    reaplicaría lo guardado en el confirm ORIGINAL, potencialmente el mapeo
    mal resuelto que motivó la relectura). Mismo efecto que REAPPLIED sobre
    versionado/estado: bumpea ``ingestion_version`` y deja ``APPLIED``."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    summary = parse_uploaded_content(_CSV_RISK_BAD, "text/csv", "gastos.csv")
    confirmed = default_confirmed_fields(summary)
    file = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="gastos.csv",
        s3_key=f"tenants/{tenant.tenant_id}/gastos.csv",
        content_type="text/csv",
        size_bytes=len(_CSV_RISK_BAD),
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
        # Nunca pasó por F8b: sin `column_risk_decisions` guardadas — sin
        # borrador, esto resolvería a NO_RISK_FOUND/AMBIGUOUS, nunca REAPPLIED.
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmed,
        },
    )
    db_session.add(file)
    await db_session.commit()

    run = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="READY_TO_APPLY",
        dry_run=True,
        details_json={
            "file_id": str(file.id),
            "draft_version": 1,
            "draft": {
                "column_mappings": [
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "context_entities": {"table": "expense"},
                "confirmed_fields": confirmed,
                "context_confirmed": {},
                "column_risk_decisions": [_RISK_DECISION],
                "stock_treatment": None,
                "master_column_mappings": None,
            },
        },
    )
    db_session.add(run)
    await db_session.commit()

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id, run=run)
    await db_session.commit()

    assert result.column_risk_outcome == "USER_REVIEWED"
    assert file.ingestion_version == INGESTION_VERSION
    assert file.reread_status == REREAD_STATUS_APPLIED
    assert file.reread_summary["outcome"] == "USER_REVIEWED"
    # Ambas filas de `_CSV_RISK_FIXED` tienen `monto` — ninguna afectada por la
    # decisión del borrador (rutear a Otros solo lo que siga faltando `monto`),
    # así que las dos se importan como gasto.
    assert len(await _active_expenses(db_session, tenant, file)) == 2


_CSV_CUSTOM_AMOUNT_COLUMN = (
    b"fecha,proveedor,valor_facturado\n2026-01-05,Distribuidora Sur,1500\n"
)


async def test_apply_reread_uses_draft_column_mapping_to_import_correctly(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correccion C1 (revision externa 2026-08-19): un borrador con mapeo
    explicito de columnas (``draft.column_mappings``) tiene que llegar hasta
    el import real — antes, ``apply_reread`` solo usaba las decisiones de
    riesgo del borrador; el reimport seguia detectando columnas 100% por
    heuristica sobre el contenido re-leido, asi que corregir el mapeo en
    pantalla no cambiaba nada de lo efectivamente importado."""
    _patch_s3(monkeypatch, _CSV_CUSTOM_AMOUNT_COLUMN)
    file_sin_draft = await _make_file(db_session, tenant, _CSV_CUSTOM_AMOUNT_COLUMN)

    # Sin borrador: "valor_facturado" no matchea ningun keyword de monto —
    # documenta el bug de base (la fila no se importa como gasto).
    await reread_service.apply_reread(db_session, file_sin_draft.id, tenant.tenant_id)
    await db_session.commit()
    assert len(await _active_expenses(db_session, tenant, file_sin_draft)) == 0

    file_con_draft = await _make_file(db_session, tenant, _CSV_CUSTOM_AMOUNT_COLUMN)
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file_con_draft.id, tenant.tenant_id
    )
    draft = {
        "column_mappings": [
            {"source_column": "fecha", "target_field": "expense_date"},
            {"source_column": "proveedor", "target_field": "supplier_name"},
            {"source_column": "valor_facturado", "target_field": "amount"},
        ],
        "context_entities": {"table": "expense"},
        "confirmed_fields": {"gastos": True},
        "context_confirmed": {},
        "column_risk_decisions": [],
        "stock_treatment": None,
        "master_column_mappings": None,
    }
    details = dict(run.details_json or {})
    details["draft"] = draft
    details["draft_version"] = 1
    run.details_json = details
    run.status = "READY_TO_APPLY"
    await db_session.flush()

    await reread_service.apply_reread(
        db_session, file_con_draft.id, tenant.tenant_id, run=run, fresh_override=fresh
    )
    await db_session.commit()

    activos = await _active_expenses(db_session, tenant, file_con_draft)
    assert len(activos) == 1
    assert activos[0].amount == Decimal("1500")


_CSV_RISK_AMBIGUOUS_TWO_COLS = (
    b"fecha,producto,monto,monto2,proveedor\n"
    b"2026-01-05,Coca Cola,1500,,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,800,Panaderia Norte\n"
)


async def test_preview_reread_downgrades_user_reviewed_when_other_column_still_ambiguous(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correccion C2 (revision externa 2026-08-19): un borrador que trae
    ``column_mappings`` no alcanza para READY_TO_APPLY si mapear esas columnas
    deja OTRA columna con riesgo ambiguo (2+ acciones legales) sin una decision
    explicita en ``column_risk_decisions`` — antes, alcanzaba con que
    ``column_mappings`` no estuviera vacio para resolver a USER_REVIEWED sin
    mirar el resto de las columnas riesgosas."""
    _patch_s3(monkeypatch, _CSV_RISK_AMBIGUOUS_TWO_COLS)
    file = await _make_file(db_session, tenant, _CSV_RISK_AMBIGUOUS_TWO_COLS)

    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    draft = {
        "column_mappings": [
            {"source_column": "fecha", "target_field": "expense_date"},
            {"source_column": "producto", "target_field": "ignore"},
            {"source_column": "monto", "target_field": "amount"},
            {"source_column": "monto2", "target_field": "amount"},
            {"source_column": "proveedor", "target_field": "supplier_name"},
        ],
        "context_entities": {"table": "expense"},
        "confirmed_fields": {"gastos": True},
        "context_confirmed": {},
        # Solo cubre "monto" — "monto2" queda con riesgo ambiguo sin decision.
        "column_risk_decisions": [
            {
                "context_id": "table",
                "source_column": "monto",
                "target_field": "amount",
                "action": "route_affected_rows_to_others",
            }
        ],
        "stock_treatment": None,
        "master_column_mappings": None,
    }
    details = dict(run.details_json or {})
    details["draft"] = draft
    details["draft_version"] = 1
    run.details_json = details
    await db_session.flush()

    preview = await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh, run=run
    )
    reread_service.mark_session_ready_to_apply(
        run, column_risk_outcome=preview.column_risk_outcome
    )
    await db_session.commit()

    assert preview.column_risk_outcome == "AMBIGUOUS"
    assert any(r["source_column"] == "monto2" for r in preview.column_risk_ambiguous)
    assert run.status == "NEEDS_REVIEW"


async def test_undo_reread_reverts_ingestion_version_and_status(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round post-review (hallazgo Important #2): ``undo_reread`` debe
    revertir el stamping de versionado que ``apply_reread`` hizo sobre el
    archivo. Antes de este fix, deshacer una relectura REAPPLIED dejaba el
    archivo con ``ingestion_version`` bumpeado y ``reread_status=APPLIED``
    para siempre, aunque sus datos hubieran vuelto al estado pre-reread —
    excluyéndolo PARA SIEMPRE de ``select_candidate_files`` (filtra por
    ``ingestion_version < to_version``)."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)
    original_version = file.ingestion_version
    assert original_version < INGESTION_VERSION  # precondición: hay algo que bumpear

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "REAPPLIED"
    assert file.ingestion_version == INGESTION_VERSION
    assert file.reread_status == REREAD_STATUS_APPLIED

    undo = await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()
    assert undo["status"] == "REVERTED"

    # El archivo vuelve a su ingestion_version previa y deja de "decir" APPLIED
    # — necesita revisión de nuevo, y select_candidate_files debe poder
    # encontrarlo otra vez (ingestion_version < to_version).
    assert file.ingestion_version == original_version
    assert file.reread_status not in (REREAD_STATUS_APPLIED, REREAD_STATUS_AUTO_APPLIED)
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW


# ── F9a (Task 3): outcomes explícitos para archivos pre-F8 (mapeo re-derivado) ──
#
# Invariante de seguridad: para un archivo confirmado ANTES de F8 (sin
# ``column_risk_decisions`` guardadas), el mapeo que ``derive_context_mapping_
# entries`` deriva es un GUESS sobre datos ya importados — NUNCA el que el
# usuario eligió. Por eso NINGUNO de estos outcomes (NO_RISK_FOUND,
# FORCED_UNVERIFIED, AMBIGUOUS) toca el summary ni se auto-aplica, aunque una
# acción sea la única legal (FORCED_UNVERIFIED).

# "monto" (requerido → amount) vacío en TODAS las filas, sin columna de
# reemplazo: la única acción legal es ``route_affected_rows_to_others`` (un
# requerido con una sola columna mapeada no se puede dropear) → FORCED_UNVERIFIED.
_CSV_FORCED_UNVERIFIED = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,Panaderia Norte\n"
)

# "monto" e "importe" mapean AMBOS a ``amount`` (requerido, 2 columnas → hay
# reemplazo): "importe" vacío en una fila habilita 2 acciones legales
# (route_affected_rows_to_others + drop_column) → AMBIGUOUS.
_CSV_AMBIGUOUS_RISK = (
    b"fecha,producto,monto,importe,proveedor\n"
    b"2026-01-05,Coca Cola,1500,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,,Panaderia Norte\n"
)


async def test_reread_forced_unverified_does_not_auto_apply(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archivo pre-F8 (sin ``column_risk_decisions``) con una columna requerida
    100% nula y SIN reemplazo: ``preview_reread`` devuelve outcome
    ``FORCED_UNVERIFIED`` — la única acción legal existe, pero NO se aplica.
    Tras ``apply_reread``, ``ingestion_version`` NO cambia y ``reread_status``
    queda ``NEEDS_REVIEW`` (nunca ``APPLIED``). Documenta explícitamente que
    "acción no ambigua" no implica auto-apply."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    original_version = file.ingestion_version

    # Verificación LITERAL del contrato del brief ("el summary resultante es
    # idéntico al de entrada, nada se dropea/rutea"): no basta con inferirlo por
    # ausencia de efectos colaterales (0 capturas en "Otros", versión sin
    # cambios) — eso es necesario pero no prueba la igualdad del summary en sí.
    # Se re-parsea el mismo contenido de forma independiente (misma llamada que
    # usa el servicio internamente vía ``_fresh_summary``), se toma una copia
    # profunda ANTES de pasarlo por ``_resolve_risk_decisions`` y se compara con
    # el diccionario después de la llamada: para FORCED_UNVERIFIED,
    # ``resolved.applied`` debe ser ``None`` y el dict de entrada no debe haber
    # sido mutado (ninguna columna dropeada, ninguna fila ruteada).
    input_summary = parse_uploaded_content(_CSV_FORCED_UNVERIFIED, "text/csv", "gastos.csv")
    summary_before = deepcopy(input_summary)
    confirmed_fields_direct = reread_service._confirmed_fields_for(file, input_summary)
    resolved_direct = await reread_service._resolve_risk_decisions(
        db_session, tenant.tenant_id, file, input_summary, confirmed_fields_direct
    )
    assert resolved_direct.outcome == "FORCED_UNVERIFIED"
    assert resolved_direct.applied is None
    assert input_summary == summary_before

    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)
    assert preview.column_risk_outcome == "FORCED_UNVERIFIED"
    assert preview.column_risk_ambiguous == []
    assert len(preview.column_risk_forced_unverified) == 1
    forced = preview.column_risk_forced_unverified[0]
    assert forced["source_column"] == "monto"
    assert forced["target_field"] == "amount"
    assert forced["action"] == "route_affected_rows_to_others"

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "FORCED_UNVERIFIED"
    # Ni se dropeó ni se ruteó nada: ningún registro viene del protocolo de
    # riesgo (eso solo ocurre en el outcome REAPPLIED, vía
    # ``_reconcile_column_risk``), que es lo que este test vigila. Se distingue
    # por la clave de correlación que SOLO escribe esa vía: desde F-H4 las dos
    # filas caen igual en "Otros", pero por no tener monto ni con qué calcularlo
    # —el archivo tiene la columna `monto` entera vacía—, que es un motivo
    # distinto y una vía distinta.
    pendientes = await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)
    assert [r for r in pendientes if RISK_REF_KEY in (r.row_data or {})] == []
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_DISMISSED)) == 0
    # Y la relectura pudo guardarlas: `source="reread"` no es un valor válido de
    # la columna, así que capturar durante una relectura reventaba la CHECK y se
    # llevaba puesta la transacción entera del apply.
    assert len(pendientes) == 2
    assert {r.source for r in pendientes} == {"reanalysis"}
    assert all("sin monto" in (r.context_label or "").lower() for r in pendientes)
    assert file.ingestion_version == original_version
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW
    assert file.reread_summary is not None
    assert file.reread_summary["outcome"] == "FORCED_UNVERIFIED"
    # Shape único de ``reread_summary`` (fix round post-review, hallazgo
    # Important #3): ``risk_columns`` anidado, no keys top-level sueltas.
    assert len(file.reread_summary["risk_columns"]["forced_unverified"]) == 1
    assert file.reread_summary["risk_columns"]["ambiguous"] == []


async def test_reread_ambiguous_does_not_touch_summary(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archivo pre-F8 con una columna requerida con reemplazo disponible (2+
    acciones legales): outcome ``AMBIGUOUS`` — mismo resultado que
    ``FORCED_UNVERIFIED``, nada se toca ni se auto-aplica."""
    _patch_s3(monkeypatch, _CSV_AMBIGUOUS_RISK)
    file = await _make_file(db_session, tenant, _CSV_AMBIGUOUS_RISK)
    original_version = file.ingestion_version

    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)
    assert preview.column_risk_outcome == "AMBIGUOUS"
    assert len(preview.column_risk_ambiguous) == 1
    ambiguous = preview.column_risk_ambiguous[0]
    assert ambiguous["source_column"] == "importe"
    assert ambiguous["target_field"] == "amount"
    assert set(ambiguous["allowed_actions"]) == {
        "route_affected_rows_to_others",
        "drop_column",
    }

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "AMBIGUOUS"
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 0
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_DISMISSED)) == 0
    assert file.ingestion_version == original_version
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW


async def test_reread_no_risk_found_does_not_bump_without_confirmation(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture existente sin columnas riesgosas (``_CSV_BASE``): outcome
    ``NO_RISK_FOUND``. No bumpea ``ingestion_version`` sin una confirmación
    explícita (F8b+) — ni siquiera la ausencia de riesgo alcanza."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)
    original_version = file.ingestion_version

    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)
    assert preview.column_risk_outcome == "NO_RISK_FOUND"
    assert preview.column_risk_ambiguous == []
    assert preview.column_risk_forced_unverified == []

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "NO_RISK_FOUND"
    assert file.ingestion_version == original_version
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW


async def test_resolve_risk_decisions_degrades_on_derive_context_mapping_failure(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hallazgo post-review: ``_resolve_risk_decisions`` llama a
    ``derive_context_mapping_entries`` SIN try/except, a diferencia del mismo
    llamado en ``get_file_preview`` (api/v1/ingestion.py), que lo envuelve en
    un try/except best-effort porque la derivación puede fallar de forma
    transitoria (ej. DB en ``ColumnMappingService.suggest_mappings``). Sin
    protección acá, ese mismo fallo transitorio rompía el reread preview/apply
    con un 500 genérico en vez de degradar con gracia como su hermano.

    Simula el fallo monkeypatcheando ``derive_context_mapping_entries`` (tal
    como el módulo lo importó) para que lance, y confirma que tanto
    ``_resolve_risk_decisions`` como ``preview_reread``/``apply_reread``
    devuelven ``NO_RISK_FOUND`` en vez de propagar la excepción."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)
    original_version = file.ingestion_version

    async def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("fallo transitorio simulado de DB")

    monkeypatch.setattr(reread_service, "derive_context_mapping_entries", _boom)

    summary = parse_uploaded_content(_CSV_BASE, "text/csv", "gastos.csv")
    confirmed_fields = reread_service._confirmed_fields_for(file, summary)
    resolved = await reread_service._resolve_risk_decisions(
        db_session, tenant.tenant_id, file, summary, confirmed_fields
    )
    assert resolved.outcome == "NO_RISK_FOUND"
    assert resolved.applied is None

    preview = await reread_service.preview_reread(db_session, file.id, tenant.tenant_id)
    assert preview.column_risk_outcome == "NO_RISK_FOUND"

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.column_risk_outcome == "NO_RISK_FOUND"
    assert file.ingestion_version == original_version
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW


# ── F9a (Task 3): ``file_has_user_edits`` ───────────────────────────────────────


async def _make_bare_file(session: AsyncSession, tenant: Tenant) -> UploadedFile:
    """Archivo mínimo, sin parseo — alcanza para las pruebas de
    ``file_has_user_edits`` (no leen ``parsed_summary_json``)."""
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="dummy.csv",
        s3_key=f"tenants/{tenant.tenant_id}/dummy.csv",
        content_type="text/csv",
        size_bytes=1,
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
    )
    session.add(f)
    await session.commit()
    return f


async def test_file_has_user_edits_false_without_any_edit(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    file = await _make_bare_file(db_session, tenant)
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            source_upload_id=file.id,
            amount=Decimal("100"),
            category="OTHER",
            transaction_date=datetime.now(UTC),
            description="Gasto sin editar",
            has_user_edits=False,
        )
    )
    await db_session.commit()

    assert (
        await reread_service.file_has_user_edits(db_session, file.id, tenant.tenant_id)
        is False
    )


async def test_file_has_user_edits_true_for_sale_entry(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    file = await _make_bare_file(db_session, tenant)
    db_session.add(
        SaleEntry(
            tenant_id=tenant.tenant_id,
            source_upload_id=file.id,
            amount=Decimal("1500"),
            transaction_date=datetime.now(UTC),
            has_user_edits=True,
        )
    )
    await db_session.commit()

    assert (
        await reread_service.file_has_user_edits(db_session, file.id, tenant.tenant_id)
        is True
    )


async def test_file_has_user_edits_true_for_expense_entry(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    file = await _make_bare_file(db_session, tenant)
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            source_upload_id=file.id,
            amount=Decimal("800"),
            category="OTHER",
            transaction_date=datetime.now(UTC),
            description="Gasto editado a mano",
            has_user_edits=True,
        )
    )
    await db_session.commit()

    assert (
        await reread_service.file_has_user_edits(db_session, file.id, tenant.tenant_id)
        is True
    )


async def test_file_has_user_edits_true_for_product_via_expense_link(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """``Product`` no tiene ``source_upload_id`` propio — el vínculo es
    INDIRECTO vía ``ExpenseEntry.source_upload_id`` + ``ExpenseEntry.product_id``
    (compra de mercadería que creó/actualizó el producto)."""
    file = await _make_bare_file(db_session, tenant)
    product = Product(
        tenant_id=tenant.tenant_id,
        name="Coca Cola",
        sale_price_ars=Decimal("1500"),
        unit_cost_ars=Decimal("1000"),
        stock_units=10,
        has_user_edits=True,
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            source_upload_id=file.id,
            product_id=product.id,
            amount=Decimal("1000"),
            category="INVENTORY",
            expense_type="COGS",
            transaction_date=datetime.now(UTC),
            description="Compra de mercaderia",
            has_user_edits=False,
        )
    )
    await db_session.commit()

    assert (
        await reread_service.file_has_user_edits(db_session, file.id, tenant.tenant_id)
        is True
    )


async def test_file_has_user_edits_ignores_voided_records(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Una edición manual en un registro YA ANULADO no cuenta: ``voided_at`` debe
    ser ``NULL`` para que la edición sea evidencia vigente."""
    file = await _make_bare_file(db_session, tenant)
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant.tenant_id,
            source_upload_id=file.id,
            amount=Decimal("800"),
            category="OTHER",
            transaction_date=datetime.now(UTC),
            description="Gasto editado pero anulado",
            has_user_edits=True,
            voided_at=datetime.now(UTC),
            void_reason="USER_CANCELLED",
        )
    )
    await db_session.commit()

    assert (
        await reread_service.file_has_user_edits(db_session, file.id, tenant.tenant_id)
        is False
    )


# ── F9b: auditoría before/after de maestros (clientes/proveedores) ─────────────


async def _make_master_file(
    session: AsyncSession, tenant: Tenant, master_column_mappings: dict[str, Any] | None
) -> UploadedFile:
    """Archivo con ``master_column_mappings`` guardado en el summary — mismo
    shape que ``api/v1/ingestion.py::confirm_file`` persiste (ver
    ``test_ingestion_counters_preview_f7d.py::_make_reread_file``, reusado acá
    en vez de inventar el shape de nuevo)."""
    parsed: dict[str, Any] = {"confirmed_fields": {"clientes": True}}
    if master_column_mappings is not None:
        parsed["master_column_mappings"] = master_column_mappings
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="clientes.csv",
        s3_key=f"tenants/{tenant.tenant_id}/clientes.csv",
        content_type="text/csv",
        size_bytes=10,
        purpose="clientes",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json=parsed,
    )
    session.add(f)
    await session.commit()
    return f


def _patch_reread_fresh_summary(
    monkeypatch: pytest.MonkeyPatch, fresh: dict[str, Any]
) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return b""

    monkeypatch.setattr(S3Client, "download", _fake_download)
    monkeypatch.setattr(reread_service, "parse_uploaded_content", lambda *a, **k: fresh)


async def test_reread_audita_masters_creados_y_actualizados(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9b: la relectura de una hoja de clientes genera un ``DataRepairItem``
    por cada cliente creado o actualizado (before/after), igual que ventas/
    gastos, para poder revertirlos vía ``undo_reread`` (Task 7)."""
    existing = Customer(tenant_id=tenant.tenant_id, name="Juan Pérez", dni="30111222")
    db_session.add(existing)
    await db_session.flush()
    existing_id = existing.id

    file = await _make_master_file(
        db_session,
        tenant,
        {"flat": {"nombre": "name", "documento": "dni"}, "context": {}},
    )

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "documento"],
            }
        ],
        "clientes_detectados": [
            # matchea a `existing` por DNI -> actualiza el nombre.
            {"nombre": "Juan Perez", "documento": "30111222"},
            # sin match -> crea un cliente nuevo.
            {"nombre": "Maria Lopez", "documento": "40987654"},
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.clientes == 2

    items_res = await db_session.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == result.run_id,
            DataRepairItem.action.in_(["REREAD_MASTER_CREATE", "REREAD_MASTER_UPDATE"]),
        )
    )
    items = items_res.scalars().all()
    assert len(items) == 2
    update_item = next(i for i in items if i.action == "REREAD_MASTER_UPDATE")
    assert update_item.before_json is not None
    assert update_item.before_json["name"] == "Juan Pérez"
    assert update_item.after_json is not None
    assert update_item.after_json["id"] == str(existing_id)
    assert update_item.after_json["name"] == "Juan Perez"
    create_item = next(i for i in items if i.action == "REREAD_MASTER_CREATE")
    assert create_item.before_json is None
    assert create_item.after_json is not None
    assert create_item.after_json["name"] == "Maria Lopez"


async def test_reread_audita_masters_no_duplica_create_y_update_para_misma_fila(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9b (fix post-review): dos filas del MISMO archivo con el mismo DNI —
    ninguna matchea a un cliente preexistente, así que la primera CREA y la
    segunda, al re-resolver contra el índice de dedup del propio batch
    (``apply_import`` registra el recién creado ahí para no duplicar dentro del
    mismo archivo), resuelve como "matched" contra ESE cliente recién creado y
    termina en ``updated_ids``. Sin el dedup entre ``*_creados_ids`` y
    ``*_actualizados_ids``, esto generaba DOS ``DataRepairItem`` para la MISMA
    entidad: un REREAD_MASTER_UPDATE con ``before_json=None`` (mal etiquetado,
    no hubo estado previo real) y un REREAD_MASTER_CREATE redundante. Debe
    generarse UN solo item, REREAD_MASTER_CREATE."""
    file = await _make_master_file(
        db_session,
        tenant,
        {"flat": {"nombre": "name", "documento": "dni"}, "context": {}},
    )

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "documento"],
            }
        ],
        "clientes_detectados": [
            # crea el cliente.
            {"nombre": "Juan Perez", "documento": "30111222"},
            # mismo DNI que la fila anterior -> "actualiza" lo que la primera
            # fila del MISMO archivo acaba de crear (no un cliente preexistente).
            {"nombre": "Juan Perez Corregido", "documento": "30111222"},
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1  # una sola entidad física, no dos

    items_res = await db_session.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == result.run_id,
            DataRepairItem.action.in_(["REREAD_MASTER_CREATE", "REREAD_MASTER_UPDATE"]),
        )
    )
    items = items_res.scalars().all()
    assert len(items) == 1  # NO dos (create + update fantasma) para la misma entidad
    assert items[0].action == "REREAD_MASTER_CREATE"
    assert items[0].before_json is None
    assert items[0].after_json is not None
    assert items[0].after_json["id"] == str(customers[0].id)
    # El name final refleja la ÚLTIMA fila que la tocó (la segunda, "corregida"),
    # aunque la auditoría la trate como parte del mismo CREATE.
    assert items[0].after_json["name"] == "Juan Perez Corregido"


# ── F9b (Task 6): auditoría before/after de productos en la relectura ──────────
#
# `_insert_confirmed_data_impl` soportaba `return_details=True` desde antes de F9,
# pero solo capturaba `sale_price_ars`/`stock_units`, y el reread nunca lo
# activaba (0 auditoría de productos hasta acá). Dos caminos de import de
# productos coexisten en el pipeline (ver `test_ingestion_product_identity.py`,
# "camino A"/"camino B"): el single-sheet in-place (`_insert_confirmed_data_impl`)
# y el multi-hoja (`_insert_multisheet_data._add_product`). Antes de esta task, el
# segundo NUNCA poblaba `product_details` (ni siquiera con precio/stock) — se
# cubren los dos acá.


async def _make_stock_file(
    session: AsyncSession, tenant: Tenant, *, purpose: str = "productos"
) -> UploadedFile:
    """Archivo con `confirmed_fields={"productos": True}` — mismo patrón que
    `_make_master_file`, adaptado a catálogo. El contenido real no importa (el
    test parchea `parse_uploaded_content` vía `_patch_reread_fresh_summary`)."""
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="catalogo.csv",
        s3_key=f"tenants/{tenant.tenant_id}/catalogo.csv",
        content_type="text/csv",
        size_bytes=10,
        purpose=purpose,
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={"confirmed_fields": {"productos": True}},
    )
    session.add(f)
    await session.commit()
    return f


async def test_reread_audita_productos_creados_y_actualizados(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camino B (single-sheet, `_insert_confirmed_data_impl` in-place): una fila
    actualiza un producto existente (matchea por SKU) y otra crea uno nuevo — cada
    una debe dejar un `DataRepairItem` con el before/after AMPLIADO (no solo
    precio/stock)."""
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca 500ml",
        sku="COC500",
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=5,
    )
    db_session.add(existing)
    await db_session.commit()

    file = await _make_stock_file(db_session, tenant)
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "stock_detectado": [
            # matchea `existing` por SKU -> actualiza precio/costo/stock/sku.
            {
                "producto": "Coca 500ml",
                "sku": "COC500",
                "precio": "650",
                "costo": "400",
                "stock": "20",
            },
            # SKU nuevo -> crea un producto.
            {
                "producto": "Sprite 500ml",
                "sku": "SPR500",
                "precio": "600",
                "costo": "380",
                "stock": "15",
            },
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    items_res = await db_session.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == result.run_id,
            DataRepairItem.action.in_(["CREATE_PRODUCT", "UPDATE_PRODUCT"]),
        )
    )
    items = items_res.scalars().all()
    assert len(items) == 2

    update_item = next(i for i in items if i.action == "UPDATE_PRODUCT")
    assert update_item.product_id == existing.id
    assert update_item.before_json is not None
    assert update_item.before_json["sale_price_ars"] == "500"
    assert update_item.before_json["sku"] == "COC500"  # campo ampliado, no solo precio/stock
    assert update_item.after_json is not None
    assert update_item.after_json["sale_price_ars"] == "650"
    assert update_item.after_json["stock_units"] == 20
    assert update_item.after_json["sku"] == "COC500"
    # updated_at poblado post-flush (Task 7 lo usa para el touched-since check).
    assert update_item.after_json["updated_at"] is not None
    assert "updated_at" not in update_item.before_json  # timing de flush: solo after

    create_item = next(i for i in items if i.action == "CREATE_PRODUCT")
    assert create_item.before_json is None
    assert create_item.after_json is not None
    assert create_item.after_json["sale_price_ars"] == "600"
    assert create_item.after_json["sku"] == "SPR500"
    assert create_item.after_json["updated_at"] is not None


async def test_reread_multisheet_audita_productos_creados_y_actualizados(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camino A (multi-hoja, `_insert_multisheet_data._add_product`): MISMA
    cobertura que el test anterior, pero por la vía `mapping_contexts`/
    `multi_sheet` — antes de esta task, esta función NUNCA poblaba
    `product_details` (gap propio, no cubierto por el brief original de esta
    task: los dos caminos de producto son funciones DISTINTAS con lógica
    duplicada, no una sola)."""
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca 500ml",
        sku="COC500",
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=5,
    )
    db_session.add(existing)
    await db_session.commit()

    file = await _make_stock_file(db_session, tenant)
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Productos",
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "sku", "precio", "costo", "stock"],
                "fields": None,
                "preview_rows": [],
                "row_count": 2,
            }
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [
            {
                "producto": "Coca 500ml",
                "sku": "COC500",
                "precio": "650",
                "costo": "400",
                "stock": "20",
                "__context__": "sheet:Productos",
            },
            {
                "producto": "Sprite 500ml",
                "sku": "SPR500",
                "precio": "600",
                "costo": "380",
                "stock": "15",
                "__context__": "sheet:Productos",
            },
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    items_res = await db_session.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == result.run_id,
            DataRepairItem.action.in_(["CREATE_PRODUCT", "UPDATE_PRODUCT"]),
        )
    )
    items = items_res.scalars().all()
    assert len(items) == 2

    update_item = next(i for i in items if i.action == "UPDATE_PRODUCT")
    assert update_item.product_id == existing.id
    assert update_item.before_json is not None
    assert update_item.before_json["sale_price_ars"] == "500"
    assert update_item.before_json["sku"] == "COC500"
    assert update_item.after_json is not None
    assert update_item.after_json["sale_price_ars"] == "650"
    assert update_item.after_json["sku"] == "COC500"
    assert update_item.after_json["updated_at"] is not None

    create_item = next(i for i in items if i.action == "CREATE_PRODUCT")
    assert create_item.before_json is None
    assert create_item.after_json is not None
    assert create_item.after_json["sale_price_ars"] == "600"
    assert create_item.after_json["sku"] == "SPR500"
    assert create_item.after_json["updated_at"] is not None


# ── Task 7: undo_reread restaura maestros/productos con política touched-since ─


async def test_undo_reread_restaura_master_no_tocado_y_saltea_editado(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 7: el undo de una relectura que tocó clientes distingue 3 casos —
    restaura el que la relectura actualizó y nadie tocó después (cliente_c),
    saltea (y reporta) el que alguien editó DESPUÉS de la relectura (cliente_a,
    política touched-since: nunca pisar una edición manual en silencio), y
    desactiva el que la relectura CREÓ y nadie tocó (cliente_b — no había
    "antes" al que volver)."""
    cliente_a = Customer(tenant_id=tenant.tenant_id, name="Viejo A", dni="11111111")
    cliente_c = Customer(tenant_id=tenant.tenant_id, name="Viejo C", dni="33333333")
    db_session.add_all([cliente_a, cliente_c])
    await db_session.commit()

    file = await _make_master_file(
        db_session, tenant, {"flat": {"nombre": "name", "documento": "dni"}, "context": {}}
    )
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "documento"],
            }
        ],
        "clientes_detectados": [
            {"nombre": "Actualizado A", "documento": "11111111"},
            {"nombre": "Actualizado C", "documento": "33333333"},
            {"nombre": "Nuevo B", "documento": "22222222"},
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    cliente_b = (
        await db_session.execute(select(Customer).where(Customer.dni == "22222222"))
    ).scalar_one()

    # Alguien edita cliente_a A MANO después de la relectura (simula un PATCH).
    # El bump explícito de `updated_at` evita flakiness: en SQLite `func.now()`
    # (onupdate) tiene resolución de 1 segundo, y la relectura + esta "edición"
    # ocurren en el mismo segundo de wall-clock del test.
    await db_session.refresh(cliente_a)
    cliente_a.name = "Editado Manualmente"
    cliente_a.updated_at = datetime.now(UTC) + timedelta(hours=1)
    await db_session.commit()

    undo = await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()

    assert undo["status"] == "REVERTED"

    await db_session.refresh(cliente_a)
    assert cliente_a.name == "Editado Manualmente"  # NO se pisó
    assert {
        "kind": "customer",
        "id": str(cliente_a.id),
        "reason": "edited_after_reread",
    } in undo["not_reverted_entities"]

    await db_session.refresh(cliente_c)
    assert cliente_c.name == "Viejo C"  # restaurado al estado pre-relectura
    assert cliente_c.deactivated_at is None

    await db_session.refresh(cliente_b)
    assert cliente_b.deactivated_at is not None  # creado por la relectura -> desactivado


async def test_undo_reread_restaura_supplier_no_tocado_y_saltea_editado(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 9: mismo criterio (y mismo helper compartido,
    ``_undo_master_and_product_items``) que el test de clientes de arriba, para
    PROVEEDORES — Task 5/6/7 dejaron cobertura end-to-end explícita solo de
    Customer; Supplier comparte el código pero nunca se ejercitó end-to-end
    (gap real de cobertura, confirmado por grep antes de escribir este test).
    CUILs válidos (dígito verificador módulo 11) tomados de
    ``test_supplier_import.py``/computados a mano — el validador de
    ``supplier_import_service`` rechaza (invalid, sin importar) un CUIL con
    formato o dígito verificador incorrecto."""
    proveedor_a = Supplier(tenant_id=tenant.tenant_id, name="Viejo A", cuil="20-12345678-6")
    proveedor_c = Supplier(tenant_id=tenant.tenant_id, name="Viejo C", cuil="27-23456789-1")
    db_session.add_all([proveedor_a, proveedor_c])
    await db_session.commit()

    file = await _make_master_file(
        db_session, tenant, {"flat": {"nombre": "name", "cuil": "cuil"}, "context": {}}
    )
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "proveedores",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "supplier",
                "headers": ["nombre", "cuil"],
            }
        ],
        "proveedores_detectados": [
            {"nombre": "Actualizado A", "cuil": "20-12345678-6"},
            {"nombre": "Actualizado C", "cuil": "27-23456789-1"},
            {"nombre": "Nuevo B", "cuil": "20-33333333-4"},
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert result.proveedores == 3

    proveedor_b = (
        await db_session.execute(select(Supplier).where(Supplier.cuil == "20-33333333-4"))
    ).scalar_one()

    # Alguien edita proveedor_a A MANO después de la relectura (simula un PATCH).
    # Bump explícito de `updated_at` — mismo motivo que en el test de clientes
    # (resolución de 1s de `func.now()` en SQLite).
    await db_session.refresh(proveedor_a)
    proveedor_a.name = "Editado Manualmente"
    proveedor_a.updated_at = datetime.now(UTC) + timedelta(hours=1)
    await db_session.commit()

    undo = await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()

    assert undo["status"] == "REVERTED"

    await db_session.refresh(proveedor_a)
    assert proveedor_a.name == "Editado Manualmente"  # NO se pisó
    assert {
        "kind": "supplier",
        "id": str(proveedor_a.id),
        "reason": "edited_after_reread",
    } in undo["not_reverted_entities"]

    await db_session.refresh(proveedor_c)
    assert proveedor_c.name == "Viejo C"  # restaurado al estado pre-relectura
    assert proveedor_c.deactivated_at is None

    await db_session.refresh(proveedor_b)
    assert proveedor_b.deactivated_at is not None  # creado por la relectura -> desactivado


async def test_undo_reread_restaura_producto_no_tocado_y_saltea_editado(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 7: mismo criterio que maestros, para productos. Las 3 filas del
    archivo llevan ``stock="0"`` (no-op — ``_apply_catalog_stock`` retorna
    temprano con ``delta == 0``) A PROPÓSITO: así ningún ``InventoryMovement``
    se crea y el Paso 4 preexistente del undo (reversa incremental de stock)
    no interfiere con las aserciones — aislando la aserción central: Task 7
    NUNCA restaura ``stock_units`` por ``setattr``, sea cual sea la rama.

    Revisión final F9b (Hallazgo 1): a diferencia de ``stock_units``,
    ``unit_cost_ars`` SÍ debe restaurarse por ``setattr`` — el mecanismo
    incremental de movimientos (``void_movement``/``unvoid_movement``) nunca
    lo toca, así que si no se restaura acá el undo lo deja permanentemente en
    lo que dijo el archivo releído."""
    producto_a = Product(
        tenant_id=tenant.tenant_id,
        name="Coca 500ml",
        sku="COC500",
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=5,
    )
    producto_c = Product(
        tenant_id=tenant.tenant_id,
        name="Fanta 500ml",
        sku="FAN500",
        sale_price_ars=Decimal("400"),
        unit_cost_ars=Decimal("250"),
        stock_units=8,
    )
    db_session.add_all([producto_a, producto_c])
    await db_session.commit()

    file = await _make_stock_file(db_session, tenant)
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "stock_detectado": [
            {
                "producto": "Coca 500ml",
                "sku": "COC500",
                "precio": "650",
                "costo": "400",
                "stock": "0",
            },
            {
                "producto": "Fanta 500ml",
                "sku": "FAN500",
                "precio": "450",
                "costo": "270",
                "stock": "0",
            },
            {
                "producto": "Sprite 500ml",
                "sku": "SPR500",
                "precio": "600",
                "costo": "380",
                "stock": "0",
            },
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    producto_b = (
        await db_session.execute(select(Product).where(Product.sku == "SPR500"))
    ).scalar_one()

    # La relectura SÍ pisó unit_cost_ars (270, la fila "costo" del fresh) —
    # confirma que hay algo real que restaurar más abajo, no una aserción
    # trivial sobre un valor que nunca cambió.
    await db_session.refresh(producto_c)
    assert producto_c.unit_cost_ars == Decimal("270")

    # Alguien edita producto_a a mano después de la relectura (mismo motivo del
    # bump explícito que en el test de maestros).
    await db_session.refresh(producto_a)
    producto_a.sale_price_ars = Decimal("999")
    producto_a.updated_at = datetime.now(UTC) + timedelta(hours=1)
    await db_session.commit()

    undo = await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()

    await db_session.refresh(producto_a)
    assert producto_a.sale_price_ars == Decimal("999")  # NO se pisó
    assert producto_a.stock_units == 5  # nunca tocado (ni por esto, ni por el Paso 4)
    assert {
        "kind": "product",
        "id": str(producto_a.id),
        "reason": "edited_after_reread",
    } in undo["not_reverted_entities"]

    await db_session.refresh(producto_c)
    assert producto_c.sale_price_ars == Decimal("400")  # restaurado al pre-relectura
    # Hallazgo 1: unit_cost_ars SÍ se restaura (a diferencia de stock_units) —
    # la relectura lo había pisado a 270 (fila "costo": "270" del fresh).
    assert producto_c.unit_cost_ars == Decimal("250")  # restaurado al pre-relectura
    assert producto_c.sku == "FAN500"
    assert producto_c.stock_units == 8  # nunca tocado
    assert producto_c.deactivated_at is None

    await db_session.refresh(producto_b)
    assert producto_b.deactivated_at is not None  # creado por la relectura -> desactivado
    assert producto_b.deactivation_reason == "REREAD_UNDO"
    assert producto_b.is_active is False


async def test_undo_master_and_product_items_producto_tocado_dos_veces_usa_item_mas_reciente(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Task 7: Task 6 NO dedupea productos — dos filas del MISMO archivo que
    tocan el mismo producto dejan DOS ``DataRepairItem`` ``UPDATE_PRODUCT``
    para el mismo ``product_id`` dentro del mismo run (a diferencia de
    maestros, que Task 5 ya dedupea). ``_undo_master_and_product_items`` debe
    usar el MÁS RECIENTE (el segundo, cronológicamente — el caller lo entrega
    ordenado por ``created_at``) para el touched-since check y el restore:
    nunca dejar que el item más viejo pise el resultado del más nuevo.

    Test directo sobre el helper (no pasa por ``apply_reread``/``undo_reread``
    completos) — mismo patrón que otros tests de este archivo que llaman
    funciones internas directamente (``_load_import_fingerprints`` etc.):
    aísla la lógica de dedup-por-más-reciente sin pelear con el timing/
    resolución de reloj de SQLite ni con el mecanismo de stock incremental."""
    producto = Product(
        tenant_id=tenant.tenant_id,
        name="Producto X",
        sku="FINAL",
        sale_price_ars=Decimal("999"),
        unit_cost_ars=Decimal("700"),
        stock_units=10,
    )
    db_session.add(producto)
    await db_session.commit()
    await db_session.refresh(producto)
    # El producto no fue tocado después del run -> su `updated_at` vivo debe
    # coincidir con el "after" del item MÁS RECIENTE para pasar el
    # touched-since check (y no con el del item viejo).
    current_updated_at = producto.updated_at.isoformat()

    shared_run_id = uuid.uuid4()
    item_old = DataRepairItem(
        run_id=shared_run_id,
        tenant_id=tenant.tenant_id,
        product_id=producto.id,
        action="UPDATE_PRODUCT",
        before_json={
            "sale_price_ars": "100",
            "stock_units": 3,
            "sku": "OLD",
            "barcode": None,
            "category": None,
            "acquired_at": None,
            "expiry_date": None,
        },
        after_json={
            "sale_price_ars": "500",
            "stock_units": 3,
            "sku": "MID",
            "barcode": None,
            "category": None,
            "acquired_at": None,
            "expiry_date": None,
            "updated_at": "2026-01-01T00:00:00+00:00",  # viejo, distinto al vivo
        },
        confidence="HIGH",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    item_new = DataRepairItem(
        run_id=shared_run_id,
        tenant_id=tenant.tenant_id,
        product_id=producto.id,
        action="UPDATE_PRODUCT",
        before_json={
            "sale_price_ars": "500",
            "stock_units": 3,
            "sku": "MID",
            "barcode": None,
            "category": None,
            "acquired_at": None,
            "expiry_date": None,
        },
        after_json={
            "sale_price_ars": "999",
            "stock_units": 10,
            "sku": "FINAL",
            "barcode": None,
            "category": None,
            "acquired_at": None,
            "expiry_date": None,
            "updated_at": current_updated_at,
        },
        confidence="HIGH",
        created_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
    )

    # Orden [old, new] -- mismo orden (ascendente por created_at) que
    # `undo_reread` le pasa al helper.
    not_reverted = await reread_service._undo_master_and_product_items(
        db_session, tenant.tenant_id, [item_old, item_new]
    )
    await db_session.commit()

    assert not_reverted == []  # no está "tocado" respecto al item más reciente

    await db_session.refresh(producto)
    assert producto.sale_price_ars == Decimal("500")  # before del MÁS RECIENTE, no "100"
    assert producto.sku == "MID"  # before del más reciente, no "OLD"
    assert producto.stock_units == 10  # NUNCA se toca por setattr, sea el item que sea


async def test_undo_reread_producto_creado_y_actualizado_en_mismo_run_se_desactiva(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix post-review (hallazgo Important): dos filas del MISMO archivo que
    resuelven al MISMO producto — la primera lo CREA (``CREATE_PRODUCT``), la
    segunda lo ACTUALIZA (``UPDATE_PRODUCT``, vía ``_merge_catalog_into_existing``
    contra la caché de identidad intra-corrida) — dejan DOS ``DataRepairItem``
    para el mismo ``product_id`` dentro del mismo run (Task 6 NO dedupea
    productos, a diferencia de maestros). El undo debe DESACTIVAR el producto
    (no existía antes de esta relectura), sin importar que el item MÁS
    RECIENTE del grupo sea el ``UPDATE_PRODUCT`` — "fue creado en este run"
    tiene que salir de CUALQUIER item ``CREATE_PRODUCT`` del grupo, nunca del
    más reciente (bug real: antes del fix, el ``is_create`` salía SOLO del
    item más reciente, y este producto quedaba ACTIVO tras el undo con los
    campos a mitad de camino)."""
    file = await _make_stock_file(db_session, tenant)
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "stock_detectado": [
            {
                "producto": "Producto Nuevo",
                "sku": "NEW1",
                "precio": "100",
                "costo": "60",
                "stock": "0",
            },
            {
                "producto": "Producto Nuevo Corregido",
                "sku": "NEW1",
                "precio": "120",
                "costo": "70",
                "stock": "0",
            },
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    productos = (
        await db_session.execute(select(Product).where(Product.sku == "NEW1"))
    ).scalars().all()
    assert len(productos) == 1  # una sola entidad física, no dos
    producto = productos[0]

    items_res = await db_session.execute(
        select(DataRepairItem).where(
            DataRepairItem.run_id == result.run_id,
            DataRepairItem.product_id == producto.id,
        )
    )
    items = items_res.scalars().all()
    # Las DOS, NO dedupeadas (a diferencia de maestros) — precondición del test.
    assert {i.action for i in items} == {"CREATE_PRODUCT", "UPDATE_PRODUCT"}

    undo = await reread_service.undo_reread(db_session, result.run_id, tenant.tenant_id)
    await db_session.commit()

    await db_session.refresh(producto)
    assert producto.deactivated_at is not None  # desactivado, NUNCA dejado activo
    assert producto.deactivation_reason == "REREAD_UNDO"
    assert producto.is_active is False
    assert undo["not_reverted_entities"] == []  # no estaba "tocado" -> no aparece ahí


# ── F-RR: sesión de relectura (preview persistido + control de versión) ──────


async def test_start_or_resume_preview_session_reuses_active_session(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dos llamadas seguidas (ej. el usuario reabre el modal) reusan la MISMA
    sesión — no crean un `DataRepairRun` nuevo por cada una."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)

    calls = {"download": 0}
    original_download = S3Client.download

    async def _counting_download(self: S3Client, key: str) -> bytes:
        calls["download"] += 1
        return await original_download(self, key)

    monkeypatch.setattr(S3Client, "download", _counting_download)

    run1, fresh1 = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()
    run2, fresh2 = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()

    assert run1.id == run2.id
    assert calls["download"] == 1  # NO volvió a descargar en la 2ª llamada
    assert fresh1 == fresh2
    # Cada llamada devuelve una copia propia — mutar una no afecta la otra ni
    # lo cacheado en el run.
    fresh1["mutated"] = True
    assert "mutated" not in fresh2
    assert "mutated" not in (run2.details_json or {}).get("fresh_summary", {})


async def test_start_or_resume_preview_session_different_files_independent(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El guard de sesión abierta es POR ARCHIVO: dos archivos distintos del
    mismo tenant no se bloquean entre sí."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file_a = await _make_file(db_session, tenant, _CSV_BASE)
    file_b = await _make_file(db_session, tenant, _CSV_WITH_NEW_ROW)

    run_a, _ = await reread_service.start_or_resume_preview_session(
        db_session, file_a.id, tenant.tenant_id
    )
    run_b, _ = await reread_service.start_or_resume_preview_session(
        db_session, file_b.id, tenant.tenant_id
    )
    await db_session.commit()

    assert run_a.id != run_b.id
    assert run_a.status == "PREVIEWING"
    assert run_b.status == "PREVIEWING"


async def test_validate_ready_to_apply_rejects_stale_draft_version(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh
    )
    reread_service.mark_session_ready_to_apply(run)
    await db_session.commit()

    with pytest.raises(reread_service.StaleDraftVersionError):
        await reread_service.validate_ready_to_apply(
            db_session, run.id, tenant.tenant_id, file.id, draft_version=99
        )

    # La versión correcta (0, sin correcciones en esta fase) sí pasa.
    validated = await reread_service.validate_ready_to_apply(
        db_session, run.id, tenant.tenant_id, file.id, draft_version=0
    )
    assert validated.id == run.id


async def test_validate_ready_to_apply_rejects_file_changed_since_preview(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el archivo en S3 cambió (etag/size distintos) desde que se generó el
    preview, aplicar ahora aplicaría una interpretación distinta a la que el
    usuario vio — se rechaza y hay que generar un preview nuevo."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh
    )
    reread_service.mark_session_ready_to_apply(run)
    await db_session.commit()

    async def _changed_head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"otro-hash"', "size": 999999, "last_modified": "2026-02-01T00:00:00Z"}

    monkeypatch.setattr(S3Client, "head", _changed_head)

    with pytest.raises(reread_service.FileChangedSincePreviewError):
        await reread_service.validate_ready_to_apply(
            db_session, run.id, tenant.tenant_id, file.id, draft_version=0
        )


async def test_validate_ready_to_apply_rejects_not_ready_status(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un run que todavía no terminó de previsualizarse (o ya se aplicó/
    canceló) no puede aplicarse."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, _ = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()

    assert run.status == "PREVIEWING"  # nunca se llamó mark_session_ready_to_apply
    with pytest.raises(ValueError, match="no está lista"):
        await reread_service.validate_ready_to_apply(
            db_session, run.id, tenant.tenant_id, file.id, draft_version=0
        )


async def test_cancel_preview_session_releases_file_for_new_session(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, _ = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()

    cancelled = await reread_service.cancel_preview_session(
        db_session, run.id, tenant.tenant_id, file.id
    )
    await db_session.commit()
    assert cancelled.status == "FAILED"
    assert (cancelled.details_json or {}).get("reason") == "cancelled_by_user"

    # Una nueva "Volver a leer" del mismo archivo crea una sesión NUEVA — la
    # cancelada no cuenta como abierta.
    run2, _ = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    assert run2.id != run.id


async def test_cancel_preview_session_rejects_wrong_file_id(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file_a = await _make_file(db_session, tenant, _CSV_BASE)
    file_b = await _make_file(db_session, tenant, _CSV_WITH_NEW_ROW)
    run, _ = await reread_service.start_or_resume_preview_session(
        db_session, file_a.id, tenant.tenant_id
    )
    await db_session.commit()

    with pytest.raises(ValueError, match="No hay ninguna sesión"):
        await reread_service.cancel_preview_session(
            db_session, run.id, tenant.tenant_id, file_b.id
        )


async def test_start_background_apply_reuses_existing_run_and_fresh_summary(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-RR: cuando el apply viene de una sesión de preview validada, reusa el
    MISMO run_id (no crea uno nuevo) y conserva el `fresh_summary` cacheado —
    el worker no debería tener que volver a descargar/parsear."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh
    )
    reread_service.mark_session_ready_to_apply(run)
    await db_session.commit()
    session_run_id = run.id

    validated = await reread_service.validate_ready_to_apply(
        db_session, run.id, tenant.tenant_id, file.id, draft_version=0
    )
    started = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id, existing_run=validated
    )
    await db_session.commit()

    assert started.id == session_run_id
    assert started.status == "QUEUED"
    assert (started.details_json or {}).get("fresh_summary") is not None


async def test_start_background_apply_rejects_stale_in_memory_run_copy(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la sesión se canceló (u otro request ya la aplicó) DESPUÉS de que
    ``validate_ready_to_apply`` devolvió una copia en memoria todavía
    READY_TO_APPLY, ``start_background_apply`` no puede confiar en esa copia
    ciegamente — el guard tenant-wide (RUNNING-scan) no cubre este caso
    porque el estado real nunca pasó por RUNNING. Sin el UPDATE condicionado
    por status, esto revivía una sesión cancelada."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh
    )
    reread_service.mark_session_ready_to_apply(run)
    await db_session.commit()

    # Copia en memoria "stale": válida en el momento en que se leyó.
    stale_copy = await reread_service.validate_ready_to_apply(
        db_session, run.id, tenant.tenant_id, file.id, draft_version=0
    )

    # Mientras tanto, alguien cancela la sesión (otra pestaña, u otro request).
    await reread_service.cancel_preview_session(db_session, run.id, tenant.tenant_id, file.id)
    await db_session.commit()

    with pytest.raises(ValueError, match="ya no está lista"):
        await reread_service.start_background_apply(
            db_session, file.id, tenant.tenant_id, existing_run=stale_copy
        )

    await db_session.refresh(run)
    assert run.status == "FAILED"  # sigue cancelada, NO revivida a RUNNING


# ── F-RR (Fase 4): estimate_unlinked_products — reconciliación preview↔apply ──
#
# Caso real que motiva estos tests: el resumen de reread de ASTERIA reportaba
# "sin_producto: 0" mientras la base tenía 1.403 ventas y 427 gastos/compras
# sin producto — nada en el código anterior contaba lo que se filtraba en
# silencio. Cada test corresponde a UNA de las 5 categorías mutuamente
# excluyentes.


async def test_estimate_unlinked_products_ventas_con_y_sin_producto(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca 500ml",
        sku="COC500",
        sale_price_ars=Decimal("500"),
        stock_units=10,
    )
    db_session.add(existing)
    await db_session.commit()

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "ventas_detectadas": [
            {"producto": "Coca 500ml", "sku": "COC500", "monto": "500"},
            {"producto": "Producto Desconocido XYZ", "monto": "300"},
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"ventas": True, "gastos": False}, catalog
    )

    assert result.ventas_con_producto == 1
    assert result.ventas_sin_producto == 1
    assert result.ventas_sin_producto_samples[0]["name"] == "Producto Desconocido XYZ"


async def test_estimate_unlinked_products_compra_gate_bloqueado_sin_cantidad(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """EL bug real de ASTERIA: una fila de compra con nombre de producto pero
    sin columna de cantidad mapeada (o cantidad no parseable) nunca intenta
    resolver producto — antes esto era invisible en el preview."""
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"producto": "Yerba Mate 1kg", "monto": "2200"},  # sin columna cantidad
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": True, "ventas": False}, catalog
    )

    assert result.compras_gate_bloqueado == 1
    assert result.compras_gate_bloqueado_samples[0]["name"] == "Yerba Mate 1kg"
    assert result.compras_vinculadas == 0
    assert result.compras_producto_nuevo == 0
    assert result.movimientos_sin_producto_esperado == 0


async def test_estimate_unlinked_products_compra_vinculada_a_existente(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Yerba Mate 1kg",
        sale_price_ars=Decimal("3000"),
        stock_units=5,
    )
    db_session.add(existing)
    await db_session.commit()

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"producto": "Yerba Mate 1kg", "cantidad": "10", "monto": "22000"},
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": True, "ventas": False}, catalog
    )

    assert result.compras_vinculadas == 1
    assert result.compras_gate_bloqueado == 0
    assert result.compras_producto_nuevo == 0


async def test_estimate_unlinked_products_compra_producto_nuevo(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"producto": "Alfajor Triple XYZ", "cantidad": "24", "monto": "9600"},
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": True, "ventas": False}, catalog
    )

    # No matchea catálogo (vacío) pero tiene nombre+cantidad: SE crearía y
    # quedaría vinculado — no es lo mismo que "sin producto".
    assert result.compras_producto_nuevo == 1
    assert result.compras_gate_bloqueado == 0
    assert result.compras_sin_producto == 0


async def test_estimate_unlinked_products_compra_ambigua(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Dos productos activos comparten el mismo nombre normalizado: el motor
    de identidad no puede resolver a ciegas — ambiguo, no "nuevo"."""
    db_session.add_all(
        [
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                name="Detergente 750ml",
                sku="DET-A",
                sale_price_ars=Decimal("100"),
                stock_units=1,
            ),
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                name="Detergente 750ml",
                sku="DET-B",
                sale_price_ars=Decimal("120"),
                stock_units=1,
            ),
        ]
    )
    await db_session.commit()

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"producto": "Detergente 750ml", "cantidad": "5", "monto": "500"},
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": True, "ventas": False}, catalog
    )

    assert result.compras_sin_producto == 1
    assert result.compras_vinculadas == 0
    assert result.compras_producto_nuevo == 0


async def test_estimate_unlinked_products_movimiento_sin_producto_esperado(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Un gasto real de servicio (sin nombre de producto, sin cantidad,
    categoría no-mercadería) legítimamente no requiere producto — no debe
    mezclarse con las filas realmente rotas (`compras_gate_bloqueado`)."""
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"detalle": "Alquiler local", "categoria": "Alquiler", "monto": "150000"},
        ],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": True, "ventas": False}, catalog
    )

    assert result.movimientos_sin_producto_esperado == 1
    assert result.compras_gate_bloqueado == 0


async def test_estimate_unlinked_products_stock_file_skips_purchase_gate(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """Un catálogo de stock (no libro de compras) no pasa por el gate de
    compra — evita falsos "gate_bloqueado" sobre filas de catálogo."""
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "stock_detectado": [{"producto": "Cualquiera", "stock": "10"}],
    }
    catalog = await reread_service._load_product_index(db_session, tenant.tenant_id)

    result = await reread_service.estimate_unlinked_products(
        db_session, tenant.tenant_id, fresh, {"gastos": False, "ventas": False}, catalog
    )

    assert result.compras_gate_bloqueado == 0
    assert result.movimientos_sin_producto_esperado == 0
    assert result.compras_vinculadas == 0


# ── F-RR (Fase 4): verificación post-apply ────────────────────────────────────


async def _run_through_preview_session(
    db_session: AsyncSession, tenant: Tenant, file: UploadedFile
) -> DataRepairRun:
    """Recorre el camino REAL completo (preview con sesión → READY_TO_APPLY →
    QUEUED → reclamo atómico del worker → APPLYING) para que
    ``run.details_json["projected_impact"]`` quede poblado con lo que
    ``preview_reread`` calculó — exactamente lo que la reconciliación post-
    apply necesita comparar contra la realidad persistida."""
    run, fresh = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await reread_service.preview_reread(
        db_session, file.id, tenant.tenant_id, fresh_override=fresh, run=run
    )
    reread_service.mark_session_ready_to_apply(run)
    await db_session.commit()

    validated = await reread_service.validate_ready_to_apply(
        db_session, run.id, tenant.tenant_id, file.id, draft_version=0
    )
    started = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id, existing_run=validated
    )
    assert started.status == "QUEUED"
    await db_session.commit()

    # Simula el reclamo atómico del worker (QUEUED -> APPLYING) — mismo
    # UPDATE condicionado que reread_worker.py, no una asignación directa.
    await db_session.execute(
        update(DataRepairRun)
        .where(DataRepairRun.id == started.id, DataRepairRun.status == "QUEUED")
        .values(status="APPLYING")
    )
    await db_session.commit()
    await db_session.refresh(started)
    assert started.status == "APPLYING"
    return started


async def test_apply_reread_reconciliation_matches_for_normal_import(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camino feliz, atravesando la sesión de preview real: lo que
    ``preview_reread`` proyectó y guardó, y lo que efectivamente queda
    persistido, deben coincidir SIEMPRE que no haya un bug."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    run = await _run_through_preview_session(db_session, tenant, file)
    fresh_override = (run.details_json or {}).get("fresh_summary")

    result = await reread_service.apply_reread(
        db_session, file.id, tenant.tenant_id, run=run, fresh_override=fresh_override
    )
    await db_session.commit()

    assert result.reconciliation_warning is None


async def test_apply_reread_reconciliation_detects_injected_mismatch(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prueba de mutación (hallazgo de code review: debe mentir durante el
    PREVIEW, no dentro de un recompute post-apply): si lo que quedó GUARDADO
    como "lo que el usuario vio" en la sesión de preview no coincide con lo
    que el apply real (sin mentiras) termina persistiendo, la reconciliación
    debe detectarlo."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    real_estimate = reread_service.estimate_unlinked_products

    async def _lying_estimate(*args: Any, **kwargs: Any) -> Any:
        real = await real_estimate(*args, **kwargs)
        real.ventas_sin_producto += 999
        return real

    # La mentira SOLO cubre la sesión de preview (lo que se guarda como
    # "lo que el usuario vio") — se restaura antes del apply real, que corre
    # con la lógica correcta y sin saber que el preview mintió.
    monkeypatch.setattr(reread_service, "estimate_unlinked_products", _lying_estimate)
    run = await _run_through_preview_session(db_session, tenant, file)
    monkeypatch.setattr(reread_service, "estimate_unlinked_products", real_estimate)

    fresh_override = (run.details_json or {}).get("fresh_summary")
    result = await reread_service.apply_reread(
        db_session, file.id, tenant.tenant_id, run=run, fresh_override=fresh_override
    )
    await db_session.commit()

    assert result.reconciliation_warning is not None
    assert "ventas_sin_producto" in result.reconciliation_warning
    assert result.reconciliation_warning["ventas_sin_producto"]["esperado"] != (
        result.reconciliation_warning["ventas_sin_producto"]["real"]
    )


# ── F-RR (Fase 5): sweep global + lineage vía source_run_id ──────────────────


async def test_sweep_stale_reread_runs_closes_stuck_apply_globally(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    """El sweep global cierra un apply colgado SIN que nadie reintente sobre
    ese archivo/tenant — a diferencia del guard reactivo de
    `start_background_apply`, que solo actúa si alguien vuelve a tocarlo."""
    stale = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="QUEUED",
        dry_run=False,
        details_json={"file_id": str(uuid.uuid4())},
    )
    db_session.add(stale)
    await db_session.commit()
    # `updated_at` (no `created_at`) — mismo criterio que el guard reactivo.
    await db_session.execute(
        update(DataRepairRun)
        .where(DataRepairRun.id == stale.id)
        .values(
            updated_at=datetime.now(UTC)
            - timedelta(seconds=reread_service._STALE_RUNNING_AFTER_SECONDS + 1)
        )
    )
    await db_session.commit()

    closed = await reread_service.sweep_stale_reread_runs(db_session)
    await db_session.commit()

    assert closed["apply_stuck"] == 1
    await db_session.refresh(stale)
    assert stale.status == "FAILED"
    assert (stale.details_json or {})["reason"] == "stale_never_picked_up"


async def test_sweep_stale_reread_runs_closes_abandoned_preview_session_globally(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    stale = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="PREVIEWING",
        dry_run=True,
        details_json={"file_id": str(uuid.uuid4()), "draft_version": 0},
    )
    db_session.add(stale)
    await db_session.commit()
    old_enough = datetime.now(UTC) - timedelta(
        seconds=reread_service._PREVIEW_SESSION_STALE_AFTER_SECONDS + 1
    )
    await db_session.execute(
        update(DataRepairRun).where(DataRepairRun.id == stale.id).values(updated_at=old_enough)
    )
    await db_session.commit()

    closed = await reread_service.sweep_stale_reread_runs(db_session)
    await db_session.commit()

    assert closed["preview_session_abandoned"] == 1
    await db_session.refresh(stale)
    assert stale.status == "FAILED"
    assert (stale.details_json or {})["reason"] == "stale_review_session"


async def test_sweep_stale_reread_runs_leaves_fresh_runs_alone(
    db_session: AsyncSession, tenant: Tenant
) -> None:
    fresh_apply = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={"file_id": str(uuid.uuid4())},
    )
    fresh_preview = DataRepairRun(
        tenant_id=tenant.tenant_id,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="PREVIEWING",
        dry_run=True,
        details_json={"file_id": str(uuid.uuid4())},
    )
    db_session.add_all([fresh_apply, fresh_preview])
    await db_session.commit()

    closed = await reread_service.sweep_stale_reread_runs(db_session)
    await db_session.commit()

    assert closed == {"apply_stuck": 0, "preview_session_abandoned": 0}
    await db_session.refresh(fresh_apply)
    await db_session.refresh(fresh_preview)
    assert fresh_apply.status == "APPLYING"
    assert fresh_preview.status == "PREVIEWING"


async def test_start_background_apply_sets_source_run_id_on_retry_after_stale(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reintentar tras un run colgado no es un evento sin historia: el run
    nuevo queda trazable de cuál reemplazó."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)

    stale = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()
    stale_id = stale.id
    # `updated_at` (no `created_at`) — mismo criterio que el guard reactivo.
    await db_session.execute(
        update(DataRepairRun)
        .where(DataRepairRun.id == stale_id)
        .values(
            updated_at=datetime.now(UTC)
            - timedelta(seconds=reread_service._STALE_RUNNING_AFTER_SECONDS + 1)
        )
    )
    await db_session.commit()

    fresh = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()

    assert fresh.id != stale_id
    assert fresh.source_run_id == stale_id


async def test_start_or_resume_preview_session_sets_source_run_id_after_expiry(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)

    stale, _ = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()
    stale_id = stale.id
    await db_session.execute(
        update(DataRepairRun)
        .where(DataRepairRun.id == stale_id)
        .values(
            updated_at=datetime.now(UTC)
            - timedelta(seconds=reread_service._PREVIEW_SESSION_STALE_AFTER_SECONDS + 1)
        )
    )
    await db_session.commit()

    fresh, _ = await reread_service.start_or_resume_preview_session(
        db_session, file.id, tenant.tenant_id
    )
    await db_session.commit()

    assert fresh.id != stale_id
    assert fresh.source_run_id == stale_id
