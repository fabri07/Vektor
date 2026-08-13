"""F-O.1 — releer un archivo no puede borrar lo que clasificaste desde «Otros».

Medido antes de tocar nada, sobre un CSV de dos filas donde la segunda tiene la
fecha ilegible:

| momento                          | ventas vivas          |
|----------------------------------|-----------------------|
| import                           | $1500 + fila 2 a Otros|
| el usuario clasifica la fila 2   | $1500 + **$900**      |
| relectura                        | **$1500 sola**        |

La de $900 quedaba anulada con `REREAD_REIMPORT` y nadie la reponía: para el
parser esa fila SIGUE sin poder leerse —por eso había caído a «Otros»— y su
`UnclassifiedRecord` ya estaba en `IMPORTED`, así que tampoco volvía a la
bandeja. Se perdía el trabajo del usuario y el dato.

El registro nacido de «Otros» se preserva por la misma razón que una fila editada
a mano: es una decisión humana sobre una fila que el archivo no explica solo.

**Lo que esta fase NO cierra** (es F-O.2): si la relectura AHORA sí sabe leer esa
fila, la importa además y quedan las dos. Emparejarlas necesita un vínculo
fila↔registro que hoy no se persiste.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.inventory_replay_service import run_inventory_replay
from app.domain.inventory_effect import HISTORICAL_REPLAY
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_IMPORTED,
    UnclassifiedRecord,
    unclassified_row_ref,
)
from app.tests.conftest import add_business_profile

#: La segunda fila tiene fecha ilegible: va a «Otros» (F6-A2). La primera entra
#: normal y es el control — si las dos cayeran, el test no distinguiría "se
#: preservó lo de Otros" de "la relectura no tocó nada".
_CSV = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"cuando pueda,Vela aromatica,2,900,Luis\n"
)
_PRODUCTO = "Vela aromatica"
_STOCK_PREVIO = 10


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada apply paga los reintentos de kombu."""


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


@pytest_asyncio.fixture
async def producto(db_session: AsyncSession, tenant: Tenant) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=_PRODUCTO,
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=_STOCK_PREVIO,
    )
    db_session.add(p)
    await db_session.commit()
    return p


@pytest_asyncio.fixture
async def archivo(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> UploadedFile:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return _CSV

    monkeypatch.setattr(S3Client, "download", _fake_download)
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.csv",
        s3_key=f"tenants/{tenant.tenant_id}/ventas.csv",
        content_type="text/csv",
        size_bytes=len(_CSV),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": {"ventas": True},
        },
    )
    db_session.add(f)
    await db_session.commit()
    return f


async def _importar(
    session: AsyncSession, tenant: Tenant, file: UploadedFile
) -> dict[str, Any]:
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    counts = await insert_confirmed_data(
        session,
        tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
        uploaded_file_id=file.id,
        inventory_effect={"": HISTORICAL_REPLAY},
    )
    await session.flush()
    await run_inventory_replay(
        session, tenant.tenant_id, file.id, context_ids=None, apply=True
    )
    await session.commit()
    return counts


async def _clasificar_desde_otros(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, *, product_id: uuid.UUID | None
) -> SaleEntry:
    """Lo mismo que hace `others.reclassify_record` al clasificar como venta.

    Se replica en vez de pegarle al endpoint porque lo que importa acá es la
    FORMA del registro que queda —`source_upload_id` + `source_row_ref` con el
    prefijo de «Otros»—, que es lo único que la relectura mira.
    """
    pendiente = (
        (await session.execute(select(UnclassifiedRecord))).scalars().one()
    )
    venta = SaleEntry(
        tenant_id=tenant.tenant_id,
        amount=Decimal("900"),
        quantity=2,
        transaction_date=datetime(2026, 1, 6),
        product_id=product_id,
        provenance="REAL",
        source_upload_id=file.id,
        source_row_ref=unclassified_row_ref(pendiente.id),
    )
    session.add(venta)
    pendiente.status = UNCLASSIFIED_STATUS_IMPORTED
    await session.commit()
    return venta


async def _ventas_vivas(session: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None)
        )
    )
    return list(res.scalars().all())


async def test_el_import_deja_una_fila_en_otros(
    db_session: AsyncSession, tenant: Tenant, producto: Product, archivo: UploadedFile
) -> None:
    """Control del escenario: sin la fila en «Otros» el resto no prueba nada."""
    counts = await _importar(db_session, tenant, archivo)

    assert counts["ventas"] == 1
    assert counts["otros"] == 1


async def test_la_venta_clasificada_desde_otros_sobrevive_a_la_relectura(
    db_session: AsyncSession, tenant: Tenant, producto: Product, archivo: UploadedFile
) -> None:
    """La regresión que F-O.1 cierra: antes quedaba UNA venta, no dos."""
    await _importar(db_session, tenant, archivo)
    clasificada = await _clasificar_desde_otros(
        db_session, tenant, archivo, product_id=None
    )
    clasificada_id = clasificada.id
    assert len(await _ventas_vivas(db_session, tenant)) == 2

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivas = await _ventas_vivas(db_session, tenant)
    assert clasificada_id in {v.id for v in vivas}, "la relectura se llevó la clasificada"
    # Y la fila normal SÍ se re-importó (id nuevo): la preservación es de la de
    # «Otros», no un apagón de la relectura entera.
    assert len(vivas) == 2
    assert {Decimal(v.amount) for v in vivas} == {Decimal("1500"), Decimal("900")}


async def test_se_reporta_por_qué_se_preservó(
    db_session: AsyncSession, tenant: Tenant, producto: Product, archivo: UploadedFile
) -> None:
    """Preservar por edición manual y preservar por venir de «Otros» son dos
    motivos distintos, y el informe tiene que poder decir cuál."""
    await _importar(db_session, tenant, archivo)
    await _clasificar_desde_otros(db_session, tenant, archivo, product_id=None)

    resultado = await reread_service.apply_reread(
        db_session, archivo.id, tenant.tenant_id
    )
    await db_session.commit()

    assert resultado.preserved == 1
    assert resultado.preserved_from_others == 1


async def test_el_descuento_de_la_venta_preservada_no_se_revierte(
    db_session: AsyncSession, tenant: Tenant, producto: Product, archivo: UploadedFile
) -> None:
    """La lección de V28, aplicada al otro motivo de preservación.

    Preservar la fila y no su movimiento le devuelve las unidades al stock: el
    void alcanza a todo movimiento vivo del archivo, y el de una venta se
    identifica por `source_event_id`, no por `source_row_ref`.
    """
    await _importar(db_session, tenant, archivo)
    await _clasificar_desde_otros(
        db_session, tenant, archivo, product_id=producto.id
    )
    # El panel de impacto aplica la venta que quedó pendiente (la clasificada):
    # entra al replay como cualquier venta del archivo con producto.
    await run_inventory_replay(
        db_session, tenant.tenant_id, archivo.id, context_ids=None, apply=True
    )
    await db_session.commit()
    await db_session.refresh(producto)
    stock_antes = int(producto.stock_units)
    assert stock_antes == _STOCK_PREVIO - 3 - 2

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    await db_session.refresh(producto)
    assert int(producto.stock_units) == stock_antes


# ── F-O.2: cuando la relectura SÍ sabe leer la fila ────────────────────────────
#
# El CSV de arriba tiene la fecha ilegible a propósito: el parser no la lee ni
# ahora ni después, así que F-O.1 la preserva para siempre. Acá se simula el
# escenario que F-O.2 existe para cerrar — el archivo de S3 cambia y la relectura
# ahora sí puede importar esa fila—, que es exactamente lo que el usuario pidió:
# *«al realizar relectura también tiene que modificarse»*.

_CSV_CORREGIDO = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"2026-01-06,Vela aromatica,2,900,Luis\n"
)


async def test_si_la_relectura_ya_puede_leer_la_fila_reemplaza_a_la_clasificada(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    archivo: UploadedFile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gana la relectura: el registro cargado a mano se anula y queda el del archivo.

    Sin esto quedarían LAS DOS —la que clasificó el usuario y la que acaba de
    entrar—, que es el límite declarado de F-O.1: duplicar la venta.
    """
    await _importar(db_session, tenant, archivo)
    clasificada = await _clasificar_desde_otros(
        db_session, tenant, archivo, product_id=None
    )
    clasificada_id = clasificada.id

    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return _CSV_CORREGIDO

    monkeypatch.setattr(S3Client, "download", _fake_download)

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    vivas = await _ventas_vivas(db_session, tenant)
    assert clasificada_id not in {v.id for v in vivas}, "quedó la vieja además de la nueva"
    assert len(vivas) == 2
    assert {Decimal(v.amount) for v in vivas} == {Decimal("1500"), Decimal("900")}


async def test_el_reemplazo_queda_auditado(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    archivo: UploadedFile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anular sin auditar deja al undo sin con qué reponerla."""
    await _importar(db_session, tenant, archivo)
    clasificada = await _clasificar_desde_otros(
        db_session, tenant, archivo, product_id=None
    )
    clasificada_id = clasificada.id

    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return _CSV_CORREGIDO

    monkeypatch.setattr(S3Client, "download", _fake_download)

    resultado = await reread_service.apply_reread(
        db_session, archivo.id, tenant.tenant_id
    )
    await db_session.commit()

    assert resultado.preserved_from_others == 0, "la reemplazada no sigue preservada"
    items = (
        (
            await db_session.execute(
                select(DataRepairItem).where(
                    DataRepairItem.run_id == resultado.run_id,
                    DataRepairItem.sale_entry_id == clasificada_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert [i.action for i in items] == ["REREAD_VOID"]


async def test_el_undo_no_se_lleva_el_stock_de_una_venta_preservada(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    archivo: UploadedFile,
) -> None:
    """Bug encontrado al escribir F-O.2, y vive en F-F.4.d.

    El bloque que audita "los movimientos nuevos" tomaba TODO movimiento vivo del
    archivo. Desde que el void preserva algunos —el de una fila editada a mano
    (V28) y el de una clasificada desde «Otros»—, esos quedan vivos sin que la
    relectura los haya creado: auditarlos como inserción hacía que el undo los
    anulara, devolviendo un stock que la relectura nunca tocó.
    """
    await _importar(db_session, tenant, archivo)
    await _clasificar_desde_otros(db_session, tenant, archivo, product_id=producto.id)
    await run_inventory_replay(
        db_session, tenant.tenant_id, archivo.id, context_ids=None, apply=True
    )
    await db_session.commit()
    await db_session.refresh(producto)
    stock_antes = int(producto.stock_units)

    resultado = await reread_service.apply_reread(
        db_session, archivo.id, tenant.tenant_id
    )
    await db_session.commit()

    await reread_service.undo_reread(db_session, resultado.run_id, tenant.tenant_id)
    await db_session.commit()

    await db_session.refresh(producto)
    assert int(producto.stock_units) == stock_antes


async def test_la_fila_que_sigue_sin_leerse_no_vuelve_a_la_bandeja(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    archivo: UploadedFile,
) -> None:
    """El costo de liberar la huella, cobrado.

    F-O.2 libera la huella de la fila clasificada para que el reimport pueda
    volver a leerla. Si sigue sin poder —el archivo no cambió—, vuelve a "Otros":
    quedaría una copia PENDING de algo que el usuario ya clasificó. Se descarta,
    porque pedirle que clasifique dos veces lo mismo es ruido, no información.
    """
    await _importar(db_session, tenant, archivo)
    await _clasificar_desde_otros(db_session, tenant, archivo, product_id=None)

    await reread_service.apply_reread(db_session, archivo.id, tenant.tenant_id)
    await db_session.commit()

    pendientes = (
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant.tenant_id,
                    UnclassifiedRecord.status == "PENDING",
                )
            )
        )
        .scalars()
        .all()
    )
    assert pendientes == [], "la fila ya clasificada volvió a la bandeja"
    # Y la clasificada sigue viva: descartar la copia no puede llevarse el original.
    assert len(await _ventas_vivas(db_session, tenant)) == 2
