"""F-F.4 — la relectura descuenta lo mismo que el confirm.

Hasta F-F.3 la relectura re-importaba las ventas de un archivo y **no tocaba una
unidad**. Eso no era neutral: el void que corre antes del reimport revierte todo
movimiento vivo del archivo —incluidos los `sale` que dejó el replay del
confirm—, así que releer un archivo DEVOLVÍA el stock descontado y no lo volvía a
bajar. El archivo terminaba con sus ventas cargadas y el inventario como si no se
hubieran vendido.

Lo que se prueba acá es la propiedad, no la implementación: **releer N veces deja
el mismo stock que importar una vez**. Vale para las filas que se re-importan y
para las que el usuario editó a mano, que la relectura preserva sin volver a
insertarlas.
"""

from __future__ import annotations

import uuid
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
from app.tests.conftest import add_business_profile

#: Dos ventas del mismo producto. Tres cosas de este encabezado NO son
#: decorativas, y las tres se midieron:
#:  - `cantidad`, porque sin unidades la hoja no habla de inventario;
#:  - `cliente`, porque sin él el parser clasifica `fecha`+`producto`+`cantidad`
#:    como CATÁLOGO y las filas caen en `stock_detectado`;
#:  - nombres que el importador **autodetecta**, porque la relectura re-importa
#:    sin mapeo (no guarda el de transacciones) y un import inicial con mapeo
#:    explícito compararía dos importaciones distintas.
_CSV = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"2026-01-06,Vela aromatica,1,500,Luis\n"
)
_PRODUCTO = "Vela aromatica"
_STOCK_PREVIO = 10
_VENDIDAS = 4


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada apply paga los reintentos de kombu. Ningún assert de este
    archivo depende del encolado."""


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
    """Preexistente a propósito: si lo creara el archivo, el void de la relectura
    se lo llevaría entero y la reversa del descuento no se podría distinguir de
    la del alta."""
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


def _patch_s3(monkeypatch: pytest.MonkeyPatch, content: bytes = _CSV) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return content

    monkeypatch.setattr(S3Client, "download", _fake_download)


async def _archivo(
    session: AsyncSession, tenant: Tenant, *, con_efecto: bool = True
) -> UploadedFile:
    """El archivo tal como lo deja el confirm.

    `con_efecto=False` reproduce un archivo importado ANTES de F-F.4: su summary
    no guarda el efecto porque entonces no se persistía.
    """
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    guardado: dict[str, Any] = {
        "inferred_type": summary.get("inferred_type"),
        "confirmed_fields": {"ventas": True},
    }
    if con_efecto:
        # La clave vacía es la del archivo sin hojas identificadas: es lo que el
        # confirm resuelve para un CSV plano, y lo que obliga a que el replay
        # corra sobre TODAS las ventas en vez de filtrar por una hoja que el
        # importador nunca estampó.
        guardado["inventory_effect"] = {"": HISTORICAL_REPLAY}
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
        parsed_summary_json=guardado,
    )
    session.add(f)
    await session.commit()
    return f


async def _importar_como_el_confirm(
    session: AsyncSession, tenant: Tenant, file: UploadedFile, *, con_efecto: bool = True
) -> None:
    """Importa y aplica el descuento, que es lo que hace el confirm desde F-F.3."""
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    await insert_confirmed_data(
        session,
        tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
        uploaded_file_id=file.id,
        # SIN `column_mappings` a propósito: la relectura re-importa por
        # autodetección (no guarda el mapeo de transacciones), así que pasarlo acá
        # compararía dos importaciones distintas — el import inicial resolvería
        # columnas que el reimport no, y "la relectura no re-creó las ventas" se
        # leería como un problema del descuento.
        inventory_effect={"": HISTORICAL_REPLAY} if con_efecto else {},
    )
    await session.flush()
    if con_efecto:
        await run_inventory_replay(
            session, tenant.tenant_id, file.id, context_ids=None, apply=True
        )
    await session.commit()


async def _stock(session: AsyncSession, producto: Product) -> int:
    await session.refresh(producto)
    return int(producto.stock_units)


async def _ventas_vivas(
    session: AsyncSession, tenant: Tenant, file: UploadedFile
) -> list[SaleEntry]:
    res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.source_upload_id == file.id,
            SaleEntry.voided_at.is_(None),
        )
    )
    return list(res.scalars().all())


async def test_el_import_deja_el_stock_descontado(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: sin este punto de partida, "la relectura no lo bajó" no prueba nada."""
    _patch_s3(monkeypatch)
    file = await _archivo(db_session, tenant)
    await _importar_como_el_confirm(db_session, tenant, file)

    assert len(await _ventas_vivas(db_session, tenant, file)) == 2
    assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS


async def test_releer_no_devuelve_el_stock_descontado(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La regresión que F-F.4 cierra.

    El void de la relectura revierte los movimientos del import anterior; si nada
    los vuelve a aplicar, el stock SUBE al releer — un archivo que no cambió
    dejaría el inventario como si esas ventas no hubieran existido.
    """
    _patch_s3(monkeypatch)
    file = await _archivo(db_session, tenant)
    await _importar_como_el_confirm(db_session, tenant, file)

    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS

    # Y el descuento quedó AUDITADO, que es lo que lo hace reversible por el undo.
    # Es la única forma de fijar el orden: el bloque que audita los movimientos
    # nuevos corre después del reimport, así que un replay puesto más abajo dejaría
    # el stock bien y el undo incapaz de devolverlo.
    movimientos_auditados = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.source_file_id == file.id,
                DataRepairItem.action == "REREAD_INSERT",
                DataRepairItem.sale_entry_id.is_(None),
            )
        )
    ).scalars().all()
    tipos = {(i.after_json or {}).get("movement_type") for i in movimientos_auditados}
    assert "sale" in tipos, tipos


async def test_releer_dos_veces_deja_el_mismo_stock(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La otra mitad: tampoco puede descontar de nuevo en cada pasada.

    Es el mismo invariante que ya sostenía el void de movimientos (relectura ×N =
    mismo estado), extendido al descuento de ventas.
    """
    _patch_s3(monkeypatch)
    file = await _archivo(db_session, tenant)
    await _importar_como_el_confirm(db_session, tenant, file)

    for _ in range(2):
        await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
        await db_session.commit()

    assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS
    assert len(await _ventas_vivas(db_session, tenant, file)) == 2


async def test_la_venta_editada_a_mano_conserva_su_descuento(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El caso que no se puede asumir y hay que probar.

    La relectura PRESERVA la fila editada (no la re-importa), pero su movimiento
    de descuento no lleva `source_row_ref`, así que el void SÍ se lo lleva. Quien
    lo restituye es el re-apply, por idempotencia de `source_event_id`: la venta
    sobrevive con el mismo id, así que su clave de descuento es la misma. Sin esa
    propiedad, editar una venta a mano le devolvería sus unidades al stock en la
    próxima relectura.
    """
    _patch_s3(monkeypatch)
    file = await _archivo(db_session, tenant)
    await _importar_como_el_confirm(db_session, tenant, file)

    ventas = await _ventas_vivas(db_session, tenant, file)
    editada = ventas[0]
    editada.has_user_edits = True
    await db_session.commit()

    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS
    assert len(await _ventas_vivas(db_session, tenant, file)) == 2


async def test_un_archivo_anterior_a_f_f_4_queda_al_dia_al_releerse(
    db_session: AsyncSession,
    tenant: Tenant,
    producto: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El efecto se DEDUCE de lo leído, no se lee del summary guardado.

    Este archivo se importó antes de F-F.4: sus ventas entraron y nunca
    descontaron, y su summary no guarda efecto porque entonces no se persistía.
    Al releerlo, la deducción corre sobre lo que se acaba de leer —ventas de
    mercadería con cantidad— y el inventario queda al día.

    Es la misma propiedad que hace que la relectura pueda registrar lo que la
    lectura anterior no había detectado: si el efecto saliera del dict viejo, una
    cantidad recién detectada entraría sin mover stock.
    """
    _patch_s3(monkeypatch)
    file = await _archivo(db_session, tenant, con_efecto=False)
    await _importar_como_el_confirm(db_session, tenant, file, con_efecto=False)
    assert await _stock(db_session, producto) == _STOCK_PREVIO

    await reread_service.apply_reread(db_session, file.id, tenant.tenant_id)
    await db_session.commit()

    assert await _stock(db_session, producto) == _STOCK_PREVIO - _VENDIDAS
