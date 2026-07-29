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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.tests.conftest import add_business_profile

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


@pytest.mark.asyncio
async def test_background_apply_run_status_and_guard(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El apply en background: ``start_background_apply`` crea el run RUNNING, el
    worker lo ejecuta con ``apply_reread(run=...)`` dejándolo APPLIED, y
    ``get_reread_run`` lo devuelve. El guard bloquea una 2ª relectura concurrente."""
    _patch_s3(monkeypatch, _CSV_BASE)
    file = await _make_file(db_session, tenant, _CSV_BASE)
    await _initial_import(db_session, tenant, file, _CSV_BASE)

    run = await reread_service.start_background_apply(
        db_session, file.id, tenant.tenant_id
    )
    assert run.status == "RUNNING"
    await db_session.commit()

    # Guard: una 2ª relectura mientras hay una RUNNING reciente → ValueError.
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    # Ni se dropeó ni se ruteó nada: no se creó ningún registro en "Otros" (eso
    # solo ocurre en el outcome REAPPLIED, vía ``_reconcile_column_risk``).
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_PENDING)) == 0
    assert len(await _risk_records(db_session, tenant, file, UNCLASSIFIED_STATUS_DISMISSED)) == 0
    assert file.ingestion_version == original_version
    assert file.reread_status == REREAD_STATUS_NEEDS_REVIEW
    assert file.reread_summary is not None
    assert file.reread_summary["outcome"] == "FORCED_UNVERIFIED"
    # Shape único de ``reread_summary`` (fix round post-review, hallazgo
    # Important #3): ``risk_columns`` anidado, no keys top-level sueltas.
    assert len(file.reread_summary["risk_columns"]["forced_unverified"]) == 1
    assert file.reread_summary["risk_columns"]["ambiguous"] == []


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
