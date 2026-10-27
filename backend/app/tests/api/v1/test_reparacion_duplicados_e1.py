"""E-1: DATA_QUALITY_REPAIR no anula renglones repetidos legítimos de un archivo.

El detector de duplicados tomaba como señal que dos filas compartieran
``source_upload_id``: cinco «Coca 500» del mismo día en un mismo libro de ventas
son cinco ventas, y el apply anulaba cuatro sin reversa. Ahora un duplicado es
sólo la copia que entró por re-subir el MISMO archivo (``content_hash`` igual):
ni dos renglones de una carga, ni dos archivos distintos, ni filas sin archivo.
Se prueba por HTTP (POST /admin/repairs/misclassified-product-imports/apply) y
mirando la base.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import UploadedFile
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_APPLY = "/api/v1/admin/repairs/misclassified-product-imports/apply"
_FECHA = datetime(2026, 6, 1, 12)


async def _superadmin_headers(db: AsyncSession, tid: uuid.UUID) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tid,
        email="super-e1@vektor.app",
        full_name="Super Admin",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tid), "role_code": "SUPERADMIN"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _archivo(
    db: AsyncSession, tid: uuid.UUID, huella: str | None, minuto: int
) -> UploadedFile:
    """Un libro importado; ``huella`` es el ``content_hash`` del crudo."""
    autor = (
        await db.execute(select(User.user_id).where(User.tenant_id == tid))
    ).scalars().first()
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        uploaded_by=autor,
        original_filename="libro.xlsx",
        s3_key=f"test/libro-{minuto}.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=1024,
        purpose="ingestion",
        status="uploaded",
        processing_status="DONE",
        parsed_summary_json={"file_type": "spreadsheet"},
        content_hash=huella,
    )
    f.created_at = datetime(2026, 6, 2, 10, minuto, tzinfo=UTC)
    db.add(f)
    await db.commit()
    return f


async def _ventas(
    db: AsyncSession,
    tid: uuid.UUID,
    upload_id: uuid.UUID | None,
    cuantas: int,
    *,
    notas: str = "Coca 500",
) -> list[SaleEntry]:
    filas = []
    for i in range(cuantas):
        v = SaleEntry(
            tenant_id=tid, amount=Decimal("1200.00"), quantity=1,
            transaction_date=_FECHA, payment_method="cash", notes=notas,
            provenance="REAL", source_upload_id=upload_id,
            source_row_ref=f"{upload_id}:{i}" if upload_id else None,
        )
        db.add(v)
        filas.append(v)
    await db.commit()
    return filas


async def _gastos(
    db: AsyncSession, tid: uuid.UUID, upload_id: uuid.UUID | None, cuantos: int
) -> list[ExpenseEntry]:
    filas = []
    for i in range(cuantos):
        g = ExpenseEntry(
            tenant_id=tid, amount=Decimal("800.00"), category="OTHER",
            transaction_date=_FECHA, description="Flete", payment_method="cash",
            provenance="REAL", source_upload_id=upload_id,
            source_row_ref=f"{upload_id}:{i}" if upload_id else None,
        )
        db.add(g)
        filas.append(g)
    await db.commit()
    return filas


async def _aplicar(client: AsyncClient, db: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    resp = await client.post(
        _APPLY,
        json={"tenant_id": str(tid)},
        headers=await _superadmin_headers(db, tid),
    )
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def _anuladas(
    db: AsyncSession, tid: uuid.UUID
) -> tuple[list[SaleEntry], list[ExpenseEntry]]:
    ventas = (
        await db.execute(
            select(SaleEntry).where(
                SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_not(None)
            ).execution_options(populate_existing=True)
        )
    ).scalars().all()
    gastos = (
        await db.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid, ExpenseEntry.voided_at.is_not(None)
            ).execution_options(populate_existing=True)
        )
    ).scalars().all()
    return list(ventas), list(gastos)


async def _items_de_duplicado(db: AsyncSession, run_id: uuid.UUID) -> list[DataRepairItem]:
    return list(
        (
            await db.execute(
                select(DataRepairItem).where(
                    DataRepairItem.run_id == run_id, DataRepairItem.action == "VOID_DUPLICATE"
                )
            )
        ).scalars()
    )


async def test_renglones_repetidos_del_mismo_archivo_no_se_anulan(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El escenario de E-1: cinco ventas y tres gastos idénticos del mismo libro."""
    tid = sample_tenant.tenant_id
    libro = await _archivo(db_session, tid, "h-libro", 0)
    await _ventas(db_session, tid, libro.id, 5)
    await _gastos(db_session, tid, libro.id, 3)

    run_id = await _aplicar(client, db_session, tid)

    assert await _anuladas(db_session, tid) == ([], [])
    assert await _items_de_duplicado(db_session, run_id) == []


async def test_la_misma_venta_en_dos_archivos_distintos_no_se_anula(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos libros distintos (semanas que se pisan) con la misma venta del día:
    coincidir en contenido no prueba que sea la misma operación."""
    tid = sample_tenant.tenant_id
    semana_1 = await _archivo(db_session, tid, "h-semana-1", 0)
    semana_2 = await _archivo(db_session, tid, "h-semana-2", 5)
    await _ventas(db_session, tid, semana_1.id, 1)
    await _ventas(db_session, tid, semana_2.id, 1)
    await _gastos(db_session, tid, semana_1.id, 1)
    await _gastos(db_session, tid, semana_2.id, 1)

    run_id = await _aplicar(client, db_session, tid)

    assert await _anuladas(db_session, tid) == ([], [])
    assert await _items_de_duplicado(db_session, run_id) == []


async def test_archivos_sin_huella_y_filas_sin_archivo_no_se_anulan(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin ``content_hash`` no hay cómo saber que es el mismo archivo; y el sufijo
    «— importado» de una fila sin archivo no dice de qué carga viene."""
    tid = sample_tenant.tenant_id
    viejo_1 = await _archivo(db_session, tid, None, 0)
    viejo_2 = await _archivo(db_session, tid, None, 5)
    await _ventas(db_session, tid, viejo_1.id, 1)
    await _ventas(db_session, tid, viejo_2.id, 1)
    await _ventas(db_session, tid, None, 3, notas="Alfajor — importado")

    run_id = await _aplicar(client, db_session, tid)

    assert await _anuladas(db_session, tid) == ([], [])
    assert await _items_de_duplicado(db_session, run_id) == []


async def test_el_archivo_resubido_anula_sus_copias_y_conserva_la_carga_original(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El mismo libro subido dos veces: las copias de la re-subida se anulan, la
    carga original queda entera (con sus renglones repetidos) y la venta a mano
    idéntica no se toca."""
    tid = sample_tenant.tenant_id
    original = await _archivo(db_session, tid, "h-libro", 0)
    resubido = await _archivo(db_session, tid, "h-libro", 5)
    ventas_originales = await _ventas(db_session, tid, original.id, 3)
    ventas_copia = await _ventas(db_session, tid, resubido.id, 3)
    gastos_originales = await _gastos(db_session, tid, original.id, 2)
    gastos_copia = await _gastos(db_session, tid, resubido.id, 2)
    (a_mano,) = await _ventas(db_session, tid, None, 1)

    run_id = await _aplicar(client, db_session, tid)

    ventas, gastos = await _anuladas(db_session, tid)
    assert {v.id for v in ventas} == {v.id for v in ventas_copia}
    assert {g.id for g in gastos} == {g.id for g in gastos_copia}
    assert {v.void_reason for v in ventas} | {g.void_reason for g in gastos} == {"DUPLICATE"}
    vivas = {
        v.id
        for v in (
            await db_session.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None)
                ).execution_options(populate_existing=True)
            )
        ).scalars()
    }
    assert vivas == {v.id for v in ventas_originales} | {a_mano.id}
    items = await _items_de_duplicado(db_session, run_id)
    assert len(items) == 5
    conservadas = {i.after_json["kept_id"] for i in items if i.after_json}
    originales = {str(v.id) for v in ventas_originales} | {str(g.id) for g in gastos_originales}
    assert conservadas <= originales


async def test_la_resubida_no_pierde_mas_filas_que_las_que_tiene_la_original(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La original quedó con dos renglones vivos y la re-subida trae tres: se
    anulan dos (una copia por renglón de la original), el tercero queda."""
    tid = sample_tenant.tenant_id
    original = await _archivo(db_session, tid, "h-libro", 0)
    resubido = await _archivo(db_session, tid, "h-libro", 5)
    await _ventas(db_session, tid, original.id, 2)
    copia = await _ventas(db_session, tid, resubido.id, 3)

    await _aplicar(client, db_session, tid)

    ventas, _ = await _anuladas(db_session, tid)
    assert len(ventas) == 2
    assert {v.id for v in ventas} < {v.id for v in copia}
