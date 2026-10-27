"""T3 (C-4, B-9) — editar una venta rehace el stock según el LEDGER, no según su origen.

Hasta T3, `PATCH /sales/{id}` decidía con `source_upload_id is None`: sólo una venta
en vivo reponía y redescontaba. Desde F-F.4 el confirm descuenta las ventas que
importa (movimiento 'sale' con `sale:{id}` y el archivo como procedencia), así que
corregirle el producto o la cantidad a una importada dejaba el descuento viejo vivo
sobre el producto viejo. Y al revés: una venta en vivo legada que nunca descontó
empezaba a descontar la cantidad ENTERA por una corrección de +1.

Lo que se prueba acá, por HTTP y mirando la base:

- importada y descontada: cambiar el producto repone A y descuenta B; cambiar la
  cantidad deja descontado lo nuevo; y en los dos casos **borrar el archivo deja el
  stock como antes de importar** (el movimiento rehecho hereda el archivo);
- importada a la que le sacaron el producto después del confirm (su movimiento
  quedó anulado): asociarle otro vuelve a descontar —sin esto, editarla en dos
  pasos perdía el descuento—;
- el descuento rehecho conserva el ``source_type`` del que reemplaza;
- sin movimiento y con producto (en vivo legada o importada sin historia aplicada):
  editar la cantidad no toca el stock.

Asociar un producto a una venta en vivo que no tenía sigue descontando: lo cubre
`test_sales.py::test_patch_associate_product_decrements_when_enough`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.inventory_movement_origin import SOURCE_HISTORICAL_REPLAY
from app.application.services.stock_service import sale_source_event_id
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests._respaldo import producto_con_compra

_CTX = "sheet:Ventas"
_PRODUCTO_A = "Vela aromatica 200g"
_STOCK_A = 10
_STOCK_B = 20
_VENDIDAS = 4


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm/PATCH/DELETE paga los reintentos de kombu."""


def _summary() -> dict[str, Any]:
    filas = [
        {
            "fecha": "2024-03-10",
            "producto": _PRODUCTO_A,
            "cantidad": str(_VENDIDAS),
            "monto": "8400",
            "__context__": _CTX,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": True,
        "has_venta": True,
        "row_count": 1,
        "ventas_detectadas": filas,
        "preview_rows": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


_PAYLOAD_CONFIRM = {
    "column_mappings": [
        _map("fecha", "transaction_date"),
        _map("producto", "product_name"),
        _map("cantidad", "quantity"),
        _map("monto", "amount"),
    ],
    "confirmed_fields": {"ventas": True},
    "context_confirmed": {_CTX: True},
}


@pytest_asyncio.fixture
async def producto_a(db_session: AsyncSession, sample_tenant: Tenant) -> Product:
    """El que vende el archivo. Preexistente: si lo creara el archivo, el borrado se
    lo llevaría entero y no se vería la reversa del descuento."""
    return await producto_con_compra(
        db_session,
        sample_tenant.tenant_id,
        nombre=_PRODUCTO_A,
        stock=_STOCK_A,
        costo=Decimal("1200"),
        precio=Decimal("2100"),
    )


@pytest_asyncio.fixture
async def producto_b(db_session: AsyncSession, sample_tenant: Tenant) -> Product:
    """El producto al que se corrige la venta (con compra previa: B·5e)."""
    return await producto_con_compra(
        db_session, sample_tenant.tenant_id, nombre="Difusor 100ml", stock=_STOCK_B
    )


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    registro = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas_marzo.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas_marzo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(registro)
    await db_session.commit()
    return registro


async def _stock(db_session: AsyncSession, producto: Product) -> int:
    await db_session.refresh(producto)
    return int(producto.stock_units)


async def _importar(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    archivo: UploadedFile,
    producto_a: Product,
) -> SaleEntry:
    """Confirma el archivo y devuelve la venta importada, ya descontada."""
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json=_PAYLOAD_CONFIRM,
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert await _stock(db_session, producto_a) == _STOCK_A - _VENDIDAS
    venta = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.source_upload_id == archivo.id, SaleEntry.voided_at.is_(None)
            )
        )
    ).scalar_one()
    return venta


async def _movimientos_de_venta(
    db_session: AsyncSession, sale_id: uuid.UUID, *, vivos: bool = True
) -> list[InventoryMovement]:
    q = select(InventoryMovement).where(
        InventoryMovement.source_event_id == sale_source_event_id(sale_id),
        InventoryMovement.movement_type == "sale",
    )
    if vivos:
        q = q.where(InventoryMovement.voided_at.is_(None))
    return list((await db_session.execute(q)).scalars().all())


async def _borrar_archivo(
    client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
) -> None:
    resp = await client.delete(
        f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text


class TestVentaImportadaYaDescontada:
    async def test_cambiar_el_producto_repone_a_y_descuenta_b(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto_a: Product,
        producto_b: Product,
        archivo: UploadedFile,
    ) -> None:
        """C-4: antes A quedaba con 4 descontadas por una venta que ya no es suya y B
        no descontaba nada."""
        venta = await _importar(client, auth_headers, db_session, archivo, producto_a)

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}",
            json={"product_id": str(producto_b.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        assert await _stock(db_session, producto_a) == _STOCK_A
        assert await _stock(db_session, producto_b) == _STOCK_B - _VENDIDAS
        vivos = await _movimientos_de_venta(db_session, venta.id)
        assert [(m.product_id, int(m.qty)) for m in vivos] == [(producto_b.id, -_VENDIDAS)]
        # Hereda el archivo: es lo que permite que el borrado lo encuentre.
        assert vivos[0].source_upload_id == archivo.id
        assert vivos[0].source_type == SOURCE_HISTORICAL_REPLAY

        await _borrar_archivo(client, auth_headers, archivo)
        assert await _stock(db_session, producto_a) == _STOCK_A
        assert await _stock(db_session, producto_b) == _STOCK_B
        assert await _movimientos_de_venta(db_session, venta.id) == []

    async def test_cambiar_la_cantidad_deja_descontado_lo_nuevo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto_a: Product,
        archivo: UploadedFile,
    ) -> None:
        """C-4: 4 → 2. Antes el stock seguía descontado por 4."""
        venta = await _importar(client, auth_headers, db_session, archivo, producto_a)

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}", json={"quantity": 2}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text

        assert await _stock(db_session, producto_a) == _STOCK_A - 2
        vivos = await _movimientos_de_venta(db_session, venta.id)
        assert [int(m.qty) for m in vivos] == [-2]
        assert vivos[0].source_upload_id == archivo.id

        await _borrar_archivo(client, auth_headers, archivo)
        assert await _stock(db_session, producto_a) == _STOCK_A
        assert await _movimientos_de_venta(db_session, venta.id) == []

    async def test_subir_la_cantidad_mas_alla_del_stock_se_rechaza_sin_tocar_nada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        producto_a: Product,
        archivo: UploadedFile,
    ) -> None:
        """La validación también corre para la importada: repone 4 → hay 10, pide 11."""
        venta = await _importar(client, auth_headers, db_session, archivo, producto_a)

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}", json={"quantity": _STOCK_A + 1}, headers=auth_headers
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "INSUFFICIENT_STOCK"
        assert await _stock(db_session, producto_a) == _STOCK_A - _VENDIDAS
        vivos = await _movimientos_de_venta(db_session, venta.id)
        assert [int(m.qty) for m in vivos] == [-_VENDIDAS]


    async def test_en_dos_pasos_sin_producto_y_despues_otro_vuelve_a_descontar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto_a: Product,
        producto_b: Product,
        archivo: UploadedFile,
    ) -> None:
        """Revisión de T3: importada descontada → sin producto (repone A) → producto B.

        El primer paso hoy no entra por el PATCH (B·5e: 422 SALE_NEEDS_PRODUCT), así
        que se deja el estado que dejaría: movimiento anulado y venta sin producto.
        Lo que se prueba es el segundo: antes, «sin producto y no en vivo» no
        descontaba, y el descuento de la venta se perdía para siempre.
        """
        venta = await _importar(client, auth_headers, db_session, archivo, producto_a)
        primero = await client.patch(
            f"/api/v1/sales/{venta.id}", json={"product_id": None}, headers=auth_headers
        )
        assert primero.status_code == 422, primero.text
        assert await stock_service.revert_sale_stock(
            venta.id, sample_tenant.tenant_id, db_session
        )
        venta.product_id = None
        await db_session.commit()
        assert await _stock(db_session, producto_a) == _STOCK_A

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}",
            json={"product_id": str(producto_b.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        assert await _stock(db_session, producto_a) == _STOCK_A
        assert await _stock(db_session, producto_b) == _STOCK_B - _VENDIDAS
        vivos = await _movimientos_de_venta(db_session, venta.id)
        assert [(m.product_id, int(m.qty)) for m in vivos] == [(producto_b.id, -_VENDIDAS)]
        assert vivos[0].source_upload_id == archivo.id
        assert vivos[0].source_type == SOURCE_HISTORICAL_REPLAY

        await _borrar_archivo(client, auth_headers, archivo)
        assert await _stock(db_session, producto_a) == _STOCK_A
        assert await _stock(db_session, producto_b) == _STOCK_B

    async def test_el_descuento_rehecho_conserva_el_source_type_del_viejo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto_a: Product,
        archivo: UploadedFile,
    ) -> None:
        """Una venta del archivo cuyo descuento no es ``historical_replay`` (así lo
        deja hoy resolver una fila de «Otros»: archivo sí, ``source_type`` no). Al
        rehacerlo no se le inventa otra procedencia."""
        venta = SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            amount=Decimal("1500.00"),
            quantity=3,
            transaction_date=datetime(2024, 3, 10, 12),
            payment_method="cash",
            product_id=producto_a.id,
            source_upload_id=archivo.id,
        )
        db_session.add(venta)
        await db_session.flush()
        await stock_service.decrement_for_sale(
            venta, db_session, source_upload_id=archivo.id, source_type=None
        )
        await db_session.commit()
        assert await _stock(db_session, producto_a) == _STOCK_A - 3

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}", json={"quantity": 2}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text

        assert await _stock(db_session, producto_a) == _STOCK_A - 2
        vivos = await _movimientos_de_venta(db_session, venta.id)
        assert [(int(m.qty), m.source_type, m.source_upload_id) for m in vivos] == [
            (-2, None, archivo.id)
        ]


async def _venta_con_producto_sin_movimiento(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    producto: Product,
    *,
    source_upload_id: uuid.UUID | None = None,
) -> SaleEntry:
    """Una venta con producto que NUNCA descontó: en vivo legada (anterior a que la
    carga manual descontara) o importada de una hoja sin historia aplicada."""
    venta = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal("1500.00"),
        quantity=3,
        transaction_date=datetime(2024, 3, 10, 12),
        payment_method="cash",
        product_id=producto.id,
        source_upload_id=source_upload_id,
    )
    db_session.add(venta)
    await db_session.commit()
    return venta


class TestVentaQueNuncaDescontó:
    @pytest.mark.parametrize("importada", [False, True], ids=["en_vivo", "importada"])
    async def test_editar_la_cantidad_no_empieza_a_descontar(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        producto_b: Product,
        importada: bool,
    ) -> None:
        """B-9: 3 → 4. Antes la en vivo descontaba 4 enteras por una corrección de +1."""
        venta = await _venta_con_producto_sin_movimiento(
            db_session,
            sample_tenant.tenant_id,
            producto_b,
            source_upload_id=uuid.uuid4() if importada else None,
        )

        resp = await client.patch(
            f"/api/v1/sales/{venta.id}", json={"quantity": 4}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["quantity"] == 4

        assert await _stock(db_session, producto_b) == _STOCK_B
        assert await _movimientos_de_venta(db_session, venta.id, vivos=False) == []
