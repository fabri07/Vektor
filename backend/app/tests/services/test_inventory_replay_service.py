"""F-H3.d.4 — aplicar al inventario la historia de ventas de un archivo.

El confirm no toca stock (F-H3.c: confirmar → revisar → aplicar). Estos tests
cubren el segundo paso y las tres cosas que lo hacen seguro: que aplicar dos veces
no descuente dos veces, que el número se recalcule contra el stock de AHORA y no
contra el que devolvió el confirm, y que una venta que ya no se puede cubrir quede
pendiente en vez de anularse.
"""

from __future__ import annotations

import unittest.mock
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import SOURCE_HISTORICAL_REPLAY
from app.application.services.inventory_replay_service import (
    CONTEXTO_DESCONOCIDO,
    run_inventory_replay,
)
from app.application.services.stock_service import sale_source_event_id
from app.domain.inventory_effect import IMPORT_CONTEXT_FIELD
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

_HOJA = "sheet:ventas"


@pytest.fixture(autouse=True)
def _no_event_bus() -> Generator[None, None, None]:
    # `decrement_stock` emite STOCK_DECREASED vía EventBus → Celery → Redis. Mismo
    # patrón que `test_stock_live_sale.py`: no depender del broker para probar stock.
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _producto(db: AsyncSession, tenant: Tenant, stock: int) -> Product:
    producto = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Vela aromática 200g",
        sale_price_ars=Decimal("1050"),
        unit_cost_ars=Decimal("600"),
        stock_units=stock,
    )
    db.add(producto)
    await db.flush()
    return producto


async def _venta_importada(
    db: AsyncSession,
    tenant: Tenant,
    producto: Product,
    file_id: uuid.UUID,
    qty: int,
    day: int = 10,
    hoja: str | None = _HOJA,
) -> SaleEntry:
    venta = SaleEntry(
        tenant_id=tenant.tenant_id,
        product_id=producto.id,
        amount=Decimal("2100"),
        quantity=qty,
        transaction_date=datetime(2024, 3, day, tzinfo=UTC),
        source_upload_id=file_id,
        custom_fields={IMPORT_CONTEXT_FIELD: hoja} if hoja else {},
    )
    db.add(venta)
    await db.flush()
    return venta


async def _movimientos(db: AsyncSession) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(InventoryMovement.voided_at.is_(None))
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
class TestAplicarElReplay:
    async def test_descuenta_y_deja_el_movimiento_con_su_archivo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El `source_upload_id` no es decorativo: es lo que hace que borrar el
        archivo revierta el descuento (la reversa voidea por archivo, V15).
        """
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        venta = await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.flush()
        await db_session.refresh(producto)

        assert resultado.aplicadas == 1
        assert producto.stock_units == 6
        movimientos = await _movimientos(db_session)
        assert len(movimientos) == 1
        assert movimientos[0].qty == -4
        assert movimientos[0].source_upload_id == file_id
        assert movimientos[0].source_type == SOURCE_HISTORICAL_REPLAY
        assert movimientos[0].source_event_id == sale_source_event_id(venta.id)

    async def test_aplicar_dos_veces_no_descuenta_dos_veces(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)

        await run_inventory_replay(db_session, sample_tenant.tenant_id, file_id, apply=True)
        segunda = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.flush()
        await db_session.refresh(producto)

        assert segunda.aplicadas == 0
        assert segunda.ya_aplicadas == 1
        assert producto.stock_units == 6

    async def test_una_venta_ya_descontada_en_vivo_no_se_vuelve_a_descontar(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La clave compartida `sale:{id}` es lo que evita el doble conteo (V13)."""
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        venta = await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)
        db_session.add(
            InventoryMovement(
                tenant_id=sample_tenant.tenant_id,
                product_id=producto.id,
                movement_type="sale",
                qty=-4,
                source_event_id=sale_source_event_id(venta.id),
            )
        )
        await db_session.flush()

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)

        assert resultado.aplicadas == 0
        assert resultado.ya_aplicadas == 1
        assert producto.stock_units == 10

    async def test_dry_run_calcula_y_no_escribe(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=False
        )
        await db_session.refresh(producto)

        assert resultado.aplicadas == 0
        assert producto.stock_units == 10
        assert await _movimientos(db_session) == []
        assert [(p.saldo_inicial, p.saldo_final) for p in resultado.impacto.productos] == [
            (10, 6)
        ]

    async def test_el_saldo_es_el_de_ahora_no_el_del_confirm(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Entre confirmar y aplicar el stock puede cambiar, y manda el de ahora.

        Es la regla que F-H3.d hereda de F-H3.c y la más fácil de perder: cachear
        el número del confirm mostraría 10 → 6 cuando la operación real es 20 → 16.
        """
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)
        # Llegó mercadería entre el confirm y el apply.
        producto.stock_units = 20
        await db_session.flush()

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)

        assert [(p.saldo_inicial, p.saldo_final) for p in resultado.impacto.productos] == [
            (20, 16)
        ]
        assert producto.stock_units == 16


@pytest.mark.asyncio
class TestCuandoYaNoAlcanzaElStock:
    async def test_la_venta_queda_pendiente_y_no_se_anula(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Acá la venta ya está en los libros: anularla cambiaría facturación.

        Es la diferencia con el gate del confirm, donde la fila todavía no era una
        venta y por eso sí se podía derivar a "Otros".
        """
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=3)
        venta = await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)
        await db_session.refresh(venta)

        assert resultado.aplicadas == 0
        assert [(p.quantity, p.disponible) for p in resultado.sin_stock] == [(4, 3)]
        assert producto.stock_units == 3
        assert venta.voided_at is None

    async def test_despues_de_cargar_el_stock_el_reintento_la_aplica(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El camino que el usuario tiene que poder recorrer sin ayuda."""
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=3)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4)

        await run_inventory_replay(db_session, sample_tenant.tenant_id, file_id, apply=True)
        producto.stock_units = 10
        await db_session.flush()
        segunda = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)

        assert segunda.aplicadas == 1
        assert segunda.sin_stock == []
        assert producto.stock_units == 6

    async def test_la_que_no_entra_no_bloquea_a_las_que_si(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=5)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=20, day=3)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=5, day=10)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)

        assert resultado.aplicadas == 1
        assert len(resultado.sin_stock) == 1
        assert producto.stock_units == 0


@pytest.mark.asyncio
class TestAlcancePorHoja:
    async def test_solo_aplica_las_hojas_pedidas(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(
            db_session, sample_tenant, producto, file_id, qty=3, hoja="sheet:viejas"
        )
        await _venta_importada(
            db_session, sample_tenant, producto, file_id, qty=4, hoja="sheet:del-mes"
        )

        resultado = await run_inventory_replay(
            db_session,
            sample_tenant.tenant_id,
            file_id,
            context_ids=["sheet:del-mes"],
            apply=True,
        )
        await db_session.refresh(producto)

        assert resultado.aplicadas == 1
        assert producto.stock_units == 6

    async def test_una_venta_sin_hoja_registrada_lo_declara(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Importada antes de que se guardara la hoja: el alcance fue el archivo.

        Un alcance distinto del declarado que no se dice se lee como el declarado,
        y ahí el usuario cree haber aplicado una hoja cuando aplicó todo.
        """
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, file_id, qty=4, hoja=None)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )

        assert resultado.alcance_por_hoja is False
        assert resultado.hojas == [CONTEXTO_DESCONOCIDO]

    async def test_una_venta_de_otro_archivo_no_se_toca(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        file_id = uuid.uuid4()
        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, uuid.uuid4(), qty=4)

        resultado = await run_inventory_replay(
            db_session, sample_tenant.tenant_id, file_id, apply=True
        )
        await db_session.refresh(producto)

        assert resultado.aplicadas == 0
        assert producto.stock_units == 10


@pytest.mark.asyncio
class TestBorrarElArchivoDeshaceElReplay:
    """La reversa por borrado ya voidea por archivo (V15) y esto lo comprueba.

    Es la razón por la que el movimiento lleva `source_upload_id`: sin ese dato el
    descuento quedaría aplicado para siempre después de borrar el import, y el
    stock diría algo que ningún dato vivo respalda.
    """

    async def test_el_stock_vuelve_a_donde_estaba(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.application.services.file_deletion_service import revert_file_data
        from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

        archivo = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="ventas.xlsx",
            s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
            content_type="application/vnd.ms-excel",
            size_bytes=1024,
            purpose="general",
            status="uploaded",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={},
        )
        db_session.add(archivo)
        await db_session.flush()

        producto = await _producto(db_session, sample_tenant, stock=10)
        await _venta_importada(db_session, sample_tenant, producto, archivo.id, qty=4)
        await run_inventory_replay(
            db_session, sample_tenant.tenant_id, archivo.id, apply=True
        )
        await db_session.flush()
        await db_session.refresh(producto)
        assert producto.stock_units == 6

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
        await db_session.flush()
        await db_session.refresh(producto)

        assert producto.stock_units == 10
        assert await _movimientos(db_session) == []
