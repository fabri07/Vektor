"""Revertir un archivo no deja stock negativo (I4-5).

Revisión profunda 2026-10-24. ``void_movement`` resta sin piso a propósito (un
paso intermedio de la relectura puede bajar de 0 y el reimport volver a subir),
y ningún llamador ponía el piso en el estado final. Escenario: el archivo A trae
una compra de 2 u.; otra compra anterior (archivo B) respalda las ventas, así
que ``FILE_BACKS_SALES`` no frena; se venden 2 u. por otro lado. Borrar A, o
deshacer / aplicar una relectura que saca esas unidades, dejaba el stock en
negativo, persistido y sin aviso.

Decisión del dueño: se BLOQUEA. Los tres caminos (borrado F11, deshacer y apply
de la relectura) se frenan enteros —nada escrito— con 409
``REVERT_STOCK_NEGATIVO`` y un mensaje con el conteo y las muestras; el apply,
que corre en el worker, cierra el run FAILED con ese código. Sin conflicto,
siguen andando.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.services import reread_service, stock_service
from app.application.services.file_deletion_service import (
    build_master_details,
    record_import_ledger,
    snapshot_masters_before_import,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.business_time import today_ar
from app.domain.ingestion_version import INGESTION_VERSION
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.conftest import add_business_profile
from app.tests.integration import _reread_scenario as esc

#: Archivo B: la compra ANTERIOR que respalda las ventas (FILE_BACKS_SALES no frena).
_FILAS_B = [esc.fila(1, esc.PRODUCTO, "1", "1200")]
#: Archivo A: el que se borra, se relee o se deshace.
_FILAS_A = [
    esc.fila(5, esc.PRODUCTO, "2", "2400"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]
#: Lo que devuelve «volver a leer» A. Lo cambia cada test.
_RELEIDO: dict[str, list[dict[str, str]]] = {"filas": _FILAS_A}


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker en tests: ver el mismo fixture en ``test_reread_file.py``."""


@pytest.fixture(autouse=True)
def _s3_con_la_relectura(monkeypatch: pytest.MonkeyPatch) -> None:
    _RELEIDO["filas"] = _FILAS_A

    async def _fresh(_file: UploadedFile, _s3: S3Client) -> dict[str, Any]:
        return esc.summary(_RELEIDO["filas"])

    async def _head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"fijo"', "size": 1, "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(reread_service, "_fresh_summary", _fresh)
    monkeypatch.setattr(S3Client, "head", _head)


async def _importar(
    s: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, str]], nombre: str
) -> uuid.UUID:
    """Archivo de compras confirmado como el confirm real: efectos + ledger."""
    file_id = uuid.uuid4()
    s.add(
        UploadedFile(
            id=file_id,
            tenant_id=tenant_id,
            original_filename=nombre,
            s3_key=f"tenants/{tenant_id}/{nombre}",
            content_type="application/vnd.ms-excel",
            size_bytes=1,
            purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json=esc.summary(filas),
            ingestion_version=INGESTION_VERSION,
        )
    )
    await s.commit()
    antes_c, antes_p = await snapshot_masters_before_import(s, tenant_id)
    counts = await insert_confirmed_data(
        s,
        tenant_id,
        esc.summary(filas),
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


async def _producto(s: AsyncSession, tenant_id: uuid.UUID, nombre: str = esc.PRODUCTO) -> Product:
    s.expire_all()
    p = (
        await s.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == nombre)
        )
    ).scalar_one()
    return p


async def _stock(s: AsyncSession, tenant_id: uuid.UUID, nombre: str = esc.PRODUCTO) -> int:
    return int((await _producto(s, tenant_id, nombre)).stock_units)


async def _vender(
    client: AsyncClient,
    headers: dict[str, str],
    s: AsyncSession,
    tenant_id: uuid.UUID,
    qty: int,
) -> None:
    """Venta a mano (otra fuente), ayer: la respaldan las compras de 2024.

    Se commitea: el fixture ``client`` comparte la sesión y no commitea, así que
    sin esto el rollback del 409 se llevaría también la venta."""
    product_id = (await _producto(s, tenant_id)).id
    hoy: date = today_ar() - timedelta(days=1)
    r = await client.post(
        "/api/v1/sales",
        json={
            "amount": "1000.00",
            "quantity": qty,
            "transaction_date": datetime(hoy.year, hoy.month, hoy.day, 12).isoformat(),
            "payment_method": "cash",
            "product_id": str(product_id),
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    await s.commit()


async def _vivos(s: AsyncSession, file_id: uuid.UUID) -> tuple[int, int]:
    """``(gastos, movimientos de stock)`` vivos colgando del archivo."""
    s.expire_all()
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


def _es_stock_negativo(r: Any, *, stock_actual: int, quedaria: int) -> None:
    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert detalle["code"] == stock_service.REVERT_STOCK_NEGATIVO
    assert detalle["productos"] == 1
    (muestra,) = detalle["muestras"]
    assert (muestra["nombre"], muestra["stock_actual"], muestra["quedaria"]) == (
        esc.PRODUCTO,
        stock_actual,
        quedaria,
    )
    assert "stock negativo" in detalle["message"]
    assert f"«{esc.PRODUCTO}»: stock actual {stock_actual}, quedaría {quedaria}" in (
        detalle["message"]
    )


# ── Borrado (F11) ──────────────────────────────────────────────────────────────


async def test_borrar_un_archivo_cuya_compra_ya_se_vendio_da_409_y_no_toca_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar(db_session, tid, _FILAS_B, "b.xlsx")
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    assert await _stock(db_session, tid) == 3
    await _vender(client, auth_headers, db_session, tid, 2)
    assert await _stock(db_session, tid) == 1

    r = await client.delete(f"/api/v1/ingestion/files/{fid}?confirm=true", headers=auth_headers)

    _es_stock_negativo(r, stock_actual=1, quedaria=-1)
    # Todo o nada: ni el archivo quedó borrado ni se anuló nada.
    assert await _stock(db_session, tid) == 1
    assert await _stock(db_session, tid, "Vela vainilla 200g") == 3
    assert await _vivos(db_session, fid) == (2, 2)
    archivo = await db_session.get(UploadedFile, fid)
    assert archivo is not None and archivo.deleted_at is None


async def test_borrar_sin_conflicto_de_stock_sigue_andando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _importar(db_session, tid, _FILAS_B, "b.xlsx")
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    await _vender(client, auth_headers, db_session, tid, 1)

    r = await client.delete(f"/api/v1/ingestion/files/{fid}?confirm=true", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _stock(db_session, tid) == 0
    assert await _vivos(db_session, fid) == (0, 0)


# ── Deshacer la relectura ──────────────────────────────────────────────────────


async def test_deshacer_una_relectura_cuya_compra_ya_se_vendio_da_409_y_no_toca_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La relectura corrige 2 → 4 u.; se venden 4; deshacerla deja -2."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    _RELEIDO["filas"] = [esc.fila(5, esc.PRODUCTO, "4", "4800"), _FILAS_A[1]]
    resultado = await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    assert await _stock(db_session, tid) == 4
    await _vender(client, auth_headers, db_session, tid, 4)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    _es_stock_negativo(r, stock_actual=0, quedaria=-2)
    assert await _stock(db_session, tid) == 0
    run = await db_session.get(DataRepairRun, resultado.run_id)
    assert run is not None and run.status == "APPLIED", "la relectura sigue aplicada"
    assert await _vivos(db_session, fid) == (2, 2)


async def test_deshacer_sin_conflicto_de_stock_sigue_andando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    _RELEIDO["filas"] = [esc.fila(5, esc.PRODUCTO, "4", "4800"), _FILAS_A[1]]
    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()
    await _vender(client, auth_headers, db_session, tid, 1)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERTED"
    assert await _stock(db_session, tid) == 1


# ── Aplicar la relectura ───────────────────────────────────────────────────────


async def test_aplicar_una_relectura_que_saca_lo_vendido_se_frena_entera(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La relectura corrige 2 → 1 u. después de vender 2: el estado final (void
    −2, reimport +1) sería -1."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    await _vender(client, auth_headers, db_session, tid, 2)
    _RELEIDO["filas"] = [esc.fila(5, esc.PRODUCTO, "1", "1200"), _FILAS_A[1]]

    with pytest.raises(stock_service.RevertirDejaStockNegativoError) as exc:
        await reread_service.apply_reread(db_session, fid, tid)
    await db_session.rollback()

    assert exc.value.productos == 1
    assert exc.value.muestras[0]["stock_actual"] == 0
    assert exc.value.muestras[0]["quedaria"] == -1
    assert await _stock(db_session, tid) == 0
    assert await _vivos(db_session, fid) == (2, 2)


async def test_aplicar_una_relectura_sin_conflicto_de_stock_sigue_andando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin cambios en el archivo, el paso intermedio baja de 0 (void −2 con
    stock 0) y el reimport lo vuelve a subir: el piso mira el estado final."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    await _vender(client, auth_headers, db_session, tid, 2)

    await reread_service.apply_reread(db_session, fid, tid)
    await db_session.commit()

    assert await _stock(db_session, tid) == 0


class _EngineFalso:
    async def dispose(self) -> None:
        return None


async def test_el_worker_cierra_el_run_failed_con_el_codigo_y_el_mensaje(
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
        fid = await _importar(s, tid, _FILAS_A, "a.xlsx")
        # La salida por otro lado: una merma de las 2 unidades.
        producto = await _producto(s, tid)
        await stock_service.register_stock_loss(producto.id, tid, 2, "rotura", None, s)
        await s.commit()

    _RELEIDO["filas"] = [esc.fila(5, esc.PRODUCTO, "1", "1200"), _FILAS_A[1]]
    async with factory() as s:
        run, fresh = await reread_service.start_or_resume_preview_session(s, fid, tid)
        await reread_service.preview_reread(s, fid, tid, fresh_override=fresh, run=run)
        reread_service.mark_session_ready_to_apply(run)
        run = await reread_service.start_background_apply(s, fid, tid, existing_run=run)
        await s.commit()
        run_id = run.id

    salida = await reread_worker.aplicar_relectura(str(run_id), str(fid), str(tid))

    assert salida["status"] == "FAILED"
    async with factory() as s:
        assert await _stock(s, tid) == 0
        assert await _vivos(s, fid) == (2, 2)
        cerrado = await s.get(DataRepairRun, run_id)
        assert cerrado is not None and cerrado.status == "FAILED"
        detalles = cerrado.details_json or {}
        assert detalles["reason"] == stock_service.REVERT_STOCK_NEGATIVO
        assert "stock negativo" in detalles["error"]
        assert detalles["negative_stock"]["muestras"][0]["quedaria"] == -1


async def test_el_estado_del_run_trae_el_codigo_de_stock_negativo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Lo que la pantalla lee del polling: ``error`` (se muestra tal cual) y
    ``error_code``."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid, _FILAS_A, "a.xlsx")
    run = DataRepairRun(
        tenant_id=tid,
        repair_type=reread_service.REPAIR_TYPE_REREAD,
        status="FAILED",
        dry_run=False,
        details_json={
            "file_id": str(fid),
            "error": "Aplicar esta relectura dejaría 1 producto(s) con stock negativo",
            "reason": stock_service.REVERT_STOCK_NEGATIVO,
        },
    )
    db_session.add(run)
    await db_session.commit()

    r = await client.get(
        f"/api/v1/ingestion/files/{fid}/reread/runs/{run.id}", headers=auth_headers
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FAILED"
    assert r.json()["error_code"] == stock_service.REVERT_STOCK_NEGATIVO
    assert "stock negativo" in r.json()["error"]


# ── El piso mira lo que la reversa empeoró ─────────────────────────────────────


async def test_un_producto_que_ya_estaba_negativo_y_no_empeora_no_frena(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un negativo previo (cola offline del POS) que la reversa no toca no es
    motivo para frenar un borrado ajeno."""
    tid = sample_tenant.tenant_id
    p = Product(
        tenant_id=tid, name="Ya negativo", sale_price_ars=Decimal("100"), stock_units=-3
    )
    db_session.add(p)
    await db_session.commit()

    await stock_service.exigir_reversa_sin_stock_negativo(
        db_session, tid, {p.id: -3}, operacion="Borrar este archivo"
    )

    p.stock_units = -4
    with pytest.raises(stock_service.RevertirDejaStockNegativoError):
        await stock_service.exigir_reversa_sin_stock_negativo(
            db_session, tid, {p.id: -3}, operacion="Borrar este archivo"
        )
    await db_session.rollback()


def test_el_mensaje_cuenta_los_que_no_entran_en_las_muestras() -> None:
    muestras = [
        {"product_id": str(uuid.uuid4()), "nombre": f"P{i}", "stock_actual": 1, "quedaria": -1}
        for i in range(5)
    ]
    exc = stock_service.RevertirDejaStockNegativoError("Borrar este archivo", 7, muestras)
    assert exc.user_message is not None
    assert exc.user_message.startswith("Borrar este archivo dejaría 7 producto(s)")
    assert "y 2 más" in exc.user_message
    assert "No se cambió nada" in exc.user_message
    assert exc.detalle_http()["code"] == "REVERT_STOCK_NEGATIVO"
