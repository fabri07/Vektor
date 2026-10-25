"""El import, el borrado y el reproceso tampoco pisan una venta del POS (T2-b).

Qué se protege
--------------
Revisión profunda 2026-10-24, segunda mitad de T2. ``stock_units`` y
``InventoryBalance.current_qty`` se escriben como valor ABSOLUTO calculado en
memoria. T2-a cerró las escrituras de a una; quedaban los caminos que escriben
muchos productos seguidos:

- el **import** (compras y catálogo) leía el producto sin candado y sumaba en
  memoria: una venta del POS que commiteaba en el medio se perdía;
- **dos imports** del mismo negocio se cruzaban: los dos leían el mismo stock
  (y uno pisaba al otro), o los dos daban de alta el mismo producto nuevo;
- el **borrado de un archivo** bloqueaba cada producto al anular su movimiento,
  en el orden del archivo: contra un ticket que toma los mismos productos en
  orden de id, se bloqueaban en cruz;
- el **reproceso** de ventas históricas calculaba el saldo antes de bloquear y
  recortaba a 0 en silencio lo que no alcanzaba.

Por qué contra Postgres: SQLite no tiene candados de fila ni advisory locks;
la suite pasaría sin probar nada (``[[feedback_sqlite_masks_postgres]]``)::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_stock_candado_t2b_pg.py -m postgres -n 0 --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import (
    ingestion_import_service as iis,
)
from app.application.services import (
    inventory_replay_service,
    reread_service,
    stock_service,
)
from app.application.services.file_deletion_service import (
    build_master_details,
    record_import_ledger,
    revert_file_data,
    snapshot_masters_before_import,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_COMPRAS = "sheet:Compras"
_CATALOGO = "sheet:Catalogo"


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: cada sesión con su conexión física, si no no hay contención."""
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # `autoflush=False` como producción.
    return async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def tenant_id(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T2b", display_name="T2b"))
        await s.flush()
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tid,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        await s.commit()
    try:
        yield tid
    finally:
        async with sm() as s:
            # Todo lo del negocio cuelga del tenant con ON DELETE CASCADE.
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _producto(
    sm: async_sessionmaker[AsyncSession], tid: uuid.UUID, nombre: str, stock: int = 10
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with sm() as s:
        s.add(
            Product(
                id=pid,
                tenant_id=tid,
                name=nombre,
                sale_price_ars=Decimal("100.00"),
                unit_cost_ars=Decimal("50.00"),
                stock_units=stock,
            )
        )
        await s.flush()
        s.add(InventoryBalance(tenant_id=tid, product_id=pid, current_qty=stock, reserved_qty=0))
        await s.commit()
    return pid


async def _archivo(sm: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    fid = uuid.uuid4()
    async with sm() as s:
        s.add(
            UploadedFile(
                id=fid,
                tenant_id=tid,
                original_filename="compras.xlsx",
                s3_key=f"tests/{fid}.xlsx",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await s.commit()
    return fid


def _compras(filas: list[tuple[str, int]], *, multi: bool) -> dict[str, Any]:
    """Compras de mercadería: cada fila suma ``cantidad`` al stock. ``multi``
    elige el camino: una hoja de un libro o una tabla suelta."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed" if multi else "gastos",
        "multi_sheet": multi,
        "row_count": len(filas),
        "mapping_contexts": [
            {
                "context_id": _COMPRAS,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [
            {
                "fecha": "2026-10-20",
                "producto": nombre,
                "cantidad": str(cantidad),
                "monto": str(50 * cantidad),
                "categoria": "Mercadería",
                "__context__": _COMPRAS,
            }
            for nombre, cantidad in filas
        ],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def _importar_compras(
    s: AsyncSession,
    tid: uuid.UUID,
    fid: uuid.UUID,
    filas: list[tuple[str, int]],
    *,
    multi: bool = True,
) -> dict[str, Any]:
    return await insert_confirmed_data(
        s,
        tid,
        _compras(filas, multi=multi),
        {"gastos": True},
        context_mappings={
            _COMPRAS: {
                "fecha": "expense_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
                "categoria": "category",
            }
        },
        context_confirmed={_COMPRAS: True},
        source="ingestion",
        uploaded_file_id=fid,
    )


async def _importar_catalogo(
    s: AsyncSession, tid: uuid.UUID, fid: uuid.UUID, nombre: str, stock: int
) -> dict[str, Any]:
    return await insert_confirmed_data(
        s,
        tid,
        {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "mapping_contexts": [
                {
                    "context_id": _CATALOGO,
                    "entity_type": "product",
                    "source_kind": "sheet",
                    "headers": ["producto", "precio", "stock"],
                    "row_count": 1,
                }
            ],
            "stock_detectado": [
                {
                    "producto": nombre,
                    "precio": "500",
                    "stock": str(stock),
                    "__context__": _CATALOGO,
                }
            ],
        },
        {},
        context_mappings={
            _CATALOGO: {"producto": "name", "precio": "sale_price_ars", "stock": "stock_units"}
        },
        context_entity={_CATALOGO: "product"},
        context_confirmed={_CATALOGO: True},
        stock_treatment={_CATALOGO: "opening_balance"},
        source="ingestion",
        uploaded_file_id=fid,
    )


async def _stock(sm: async_sessionmaker[AsyncSession], pid: uuid.UUID) -> tuple[int, int]:
    """``(stock_units, current_qty)`` vigentes, desde una conexión nueva."""
    async with sm() as s:
        producto = await s.get(Product, pid)
        balance = (
            await s.execute(select(InventoryBalance).where(InventoryBalance.product_id == pid))
        ).scalar_one()
        assert producto is not None
        return producto.stock_units, balance.current_qty


async def _caja_vende(
    sm: async_sessionmaker[AsyncSession], tid: uuid.UUID, pid: uuid.UUID, qty: int = 3
) -> AsyncSession:
    """La otra caja vende ``qty`` de ``pid`` y NO commitea: queda con el candado."""
    caja = sm()
    await stock_service.check_stock_available(pid, tid, qty, caja)
    await stock_service.decrement_stock(pid, tid, qty, f"pos:{uuid.uuid4()}", caja)
    return caja


async def _espera_un_candado(
    sm: async_sessionmaker[AsyncSession], tarea: asyncio.Task[Any], que: str
) -> None:
    """Hasta que la operación quede ESPERANDO un candado en Postgres.

    Un ``sleep`` fijo no alcanza: si la operación todavía no llegó a su candado
    cuando la otra caja sigue, el test pasa aunque falte la protección.
    """
    async with sm() as s:
        for _ in range(200):
            if tarea.done():
                tarea.result()
                raise AssertionError(f"{que} no esperó: terminó antes que la otra caja")
            esperando = await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database() AND wait_event_type = 'Lock' "
                    "AND pid <> pg_backend_pid()"
                )
            )
            if esperando:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"{que} nunca quedó esperando un candado")


# ── El import contra el POS ──────────────────────────────────────────────────


@pytest.mark.parametrize("multi", [True, False], ids=["hoja", "tabla"])
async def test_la_compra_importada_no_pisa_la_venta_del_pos(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, multi: bool
) -> None:
    """Stock 10, la caja vende 3, el archivo trae una compra de 5 → 12, no 15."""
    pid = await _producto(sm, tenant_id, "Yerba Playadito 1kg")
    fid = await _archivo(sm, tenant_id)
    caja = await _caja_vende(sm, tenant_id, pid)

    async def _import() -> None:
        async with sm() as s:
            # Como lo deja una ruta: el producto ya cargado (y retenido: el
            # identity map es débil) ANTES de que la caja commitee.
            retenido = await s.get(Product, pid)
            await _importar_compras(
                s, tenant_id, fid, [("Yerba Playadito 1kg", 5)], multi=multi
            )
            assert retenido is not None
            await s.commit()

    tarea = asyncio.create_task(_import())
    await _espera_un_candado(sm, tarea, "el import")
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, 30)

    assert await _stock(sm, pid) == (12, 12)


async def test_el_stock_del_catalogo_cuenta_la_venta_del_pos_en_el_movimiento(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El catálogo declara un absoluto (20): el movimiento tiene que ser contra el
    stock VIGENTE (7 tras la venta → +13) y el balance quedar igual al stock."""
    pid = await _producto(sm, tenant_id, "Arroz Gallo 1kg")
    fid = await _archivo(sm, tenant_id)
    caja = await _caja_vende(sm, tenant_id, pid)

    async def _import() -> None:
        async with sm() as s:
            retenido = await s.get(Product, pid)
            await _importar_catalogo(s, tenant_id, fid, "Arroz Gallo 1kg", 20)
            assert retenido is not None
            await s.commit()

    tarea = asyncio.create_task(_import())
    await _espera_un_candado(sm, tarea, "el import")
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, 30)

    assert await _stock(sm, pid) == (20, 20)
    async with sm() as s:
        delta = await s.scalar(
            select(func.sum(InventoryMovement.qty)).where(
                InventoryMovement.product_id == pid,
                InventoryMovement.source_upload_id == fid,
            )
        )
    assert delta == 13


# ── Dos imports del mismo negocio ────────────────────────────────────────────


@pytest.mark.parametrize("multi", [True, False], ids=["hoja", "tabla"])
async def test_dos_imports_con_productos_en_orden_inverso_suman_los_dos(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    multi: bool,
) -> None:
    """A→B contra B→A: ninguno aborta por deadlock y ninguno pisa al otro."""
    a = await _producto(sm, tenant_id, "Fideos Matarazzo 500g")
    b = await _producto(sm, tenant_id, "Aceite Natura 900ml")
    f1 = await _archivo(sm, tenant_id)
    f2 = await _archivo(sm, tenant_id)

    # Cada fila tarda: sin el candado de importación, los dos imports quedan
    # intercalados y cada uno suma sobre el stock que leyó antes que el otro.
    original = iis._apply_purchase_to_stock

    async def _lento(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(iis, "_apply_purchase_to_stock", _lento)

    async def _import(fid: uuid.UUID, filas: list[tuple[str, int]]) -> None:
        async with sm() as s:
            await _importar_compras(s, tenant_id, fid, filas, multi=multi)
            await s.commit()

    await asyncio.wait_for(
        asyncio.gather(
            _import(f1, [("Fideos Matarazzo 500g", 1), ("Aceite Natura 900ml", 1)]),
            _import(f2, [("Aceite Natura 900ml", 2), ("Fideos Matarazzo 500g", 2)]),
        ),
        60,
    )

    assert await _stock(sm, a) == (13, 13)
    assert await _stock(sm, b) == (13, 13)


async def test_dos_imports_no_dan_de_alta_dos_veces_el_mismo_producto_nuevo(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El nombre no tiene índice único: sin esperarse, cada import creaba el suyo."""
    f1 = await _archivo(sm, tenant_id)
    f2 = await _archivo(sm, tenant_id)

    # El primero tarda entre leer el catálogo y commitear: sin el candado, el
    # segundo lee el catálogo vacío en ese hueco.
    original = iis._load_product_identity_indexes

    async def _lento(*args: Any, **kwargs: Any) -> iis.ProductIdentityIndexes:
        indices = await original(*args, **kwargs)
        await asyncio.sleep(0.3)
        return indices

    monkeypatch.setattr(iis, "_load_product_identity_indexes", _lento)

    async def _import(fid: uuid.UUID, stock: int) -> None:
        async with sm() as s:
            await _importar_catalogo(s, tenant_id, fid, "Galletitas Terrabusi Nuevas", stock)
            await s.commit()

    await asyncio.wait_for(asyncio.gather(_import(f1, 4), _import(f2, 6)), 60)

    async with sm() as s:
        productos = (
            (
                await s.execute(
                    select(Product).where(
                        Product.tenant_id == tenant_id,
                        Product.name == "Galletitas Terrabusi Nuevas",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(productos) == 1, "los dos imports crearon su propio producto"


# ── El borrado de un archivo contra el POS ───────────────────────────────────


async def test_el_borrado_no_se_bloquea_en_cruz_con_un_ticket(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El archivo compró A y B; el ticket toma A y después B (orden de id).

    Bloqueando de a uno en el orden de los movimientos (B primero), el borrado
    tenía B y esperaba A mientras el ticket tenía A y esperaba B.
    """
    x = await _producto(sm, tenant_id, "Producto X")
    y = await _producto(sm, tenant_id, "Producto Y")
    menor, mayor = sorted([x, y], key=str)
    fid = await _archivo(sm, tenant_id)
    async with sm() as s:
        # El movimiento del de id MAYOR primero: es el que se anulaba primero.
        for pid in (mayor, menor):
            s.add(
                InventoryMovement(
                    tenant_id=tenant_id,
                    product_id=pid,
                    movement_type="purchase",
                    qty=4,
                    source_event_id="import",
                    source_upload_id=fid,
                )
            )
            await s.flush()
        for pid in (mayor, menor):
            producto = await s.get(Product, pid)
            assert producto is not None
            producto.stock_units += 4
        await s.execute(
            update(InventoryBalance)
            .where(InventoryBalance.product_id.in_([mayor, menor]))
            .values(current_qty=InventoryBalance.current_qty + 4)
        )
        await s.commit()

    # El ticket ya tiene el de id menor.
    caja = sm()
    await stock_service.bloquear_productos(caja, tenant_id, [menor])

    async def _borrar() -> None:
        async with sm() as s:
            await revert_file_data(s, fid, tenant_id)
            await s.commit()

    tarea = asyncio.create_task(_borrar())
    await _espera_un_candado(sm, tarea, "el borrado")
    # El ticket pide el segundo: con el borrado reteniéndolo, Postgres aborta a
    # uno de los dos por deadlock.
    await asyncio.wait_for(stock_service.bloquear_productos(caja, tenant_id, [mayor]), 10)
    for pid in (menor, mayor):
        await stock_service.decrement_stock(pid, tenant_id, 3, f"pos:{uuid.uuid4()}", caja)
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, 30)

    # 14 − 3 (el ticket) − 4 (la compra anulada).
    assert await _stock(sm, menor) == (7, 7)
    assert await _stock(sm, mayor) == (7, 7)


async def test_el_borrado_bloquea_tambien_lo_que_restaura_sin_movimiento(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El archivo le cambió el precio a A (sin tocar su stock: ningún movimiento)
    y compró B. Borrarlo restaura el precio de A y anula la compra de B.

    Bloqueando sólo los productos con movimiento, el borrado tenía B y al
    restaurar A esperaba al ticket, que tenía A y esperaba B.
    """
    x = await _producto(sm, tenant_id, "Detergente Magistral")
    y = await _producto(sm, tenant_id, "Lavandina Ayudín")
    menor, mayor = sorted([x, y], key=str)
    nombres = {x: "Detergente Magistral", y: "Lavandina Ayudín"}
    fid = await _archivo(sm, tenant_id)
    _cat, _com = "sheet:Precios", "sheet:Compras"
    async with sm() as s:
        antes_clientes, antes_proveedores = await snapshot_masters_before_import(s, tenant_id)
        counts = await insert_confirmed_data(
            s,
            tenant_id,
            {
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "mapping_contexts": [
                    {
                        "context_id": _cat,
                        "entity_type": "product",
                        "source_kind": "sheet",
                        "headers": ["producto", "precio", "stock"],
                        "row_count": 1,
                    },
                    {
                        "context_id": _com,
                        "entity_type": "expense",
                        "source_kind": "sheet",
                        "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
                        "row_count": 1,
                    },
                ],
                # El stock que declara es el que ya tiene: cambia sólo el precio.
                "stock_detectado": [
                    {
                        "producto": nombres[menor],
                        "precio": "999",
                        "stock": "10",
                        "__context__": _cat,
                    }
                ],
                "gastos_detectados": [
                    {
                        "fecha": "2026-10-20",
                        "producto": nombres[mayor],
                        "cantidad": "4",
                        "monto": "200",
                        "categoria": "Mercadería",
                        "__context__": _com,
                    }
                ],
            },
            {"gastos": True},
            context_mappings={
                _cat: {"producto": "name", "precio": "sale_price_ars", "stock": "stock_units"},
                _com: {
                    "fecha": "expense_date",
                    "producto": "product_name",
                    "cantidad": "quantity",
                    "monto": "amount",
                    "categoria": "category",
                },
            },
            context_entity={_cat: "product", _com: "expense"},
            context_confirmed={_cat: True, _com: True},
            stock_treatment={_cat: "opening_balance"},
            source="ingestion",
            uploaded_file_id=fid,
            return_details=True,
        )
        detalles = counts.pop("product_details", []) or []
        await record_import_ledger(
            s,
            tenant_id=tenant_id,
            file_id=fid,
            product_details=detalles,
            master_details=await build_master_details(
                s, counts, antes_clientes, antes_proveedores
            ),
        )
        await s.commit()
    async with sm() as s:
        movidos = set(
            (
                await s.execute(
                    select(InventoryMovement.product_id).where(
                        InventoryMovement.source_upload_id == fid
                    )
                )
            ).scalars()
        )
    assert movidos == {mayor}, "el escenario necesita A sin movimiento"

    caja = sm()
    await stock_service.bloquear_productos(caja, tenant_id, [menor])

    async def _borrar() -> None:
        async with sm() as s:
            await revert_file_data(s, fid, tenant_id)
            await s.commit()

    tarea = asyncio.create_task(_borrar())
    await _espera_un_candado(sm, tarea, "el borrado")
    await asyncio.wait_for(stock_service.bloquear_productos(caja, tenant_id, [mayor]), 10)
    for pid in (menor, mayor):
        await stock_service.decrement_stock(pid, tenant_id, 3, f"pos:{uuid.uuid4()}", caja)
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, 30)

    assert await _stock(sm, menor) == (7, 7)
    assert await _stock(sm, mayor) == (7, 7)
    async with sm() as s:
        precio = await s.scalar(select(Product.sale_price_ars).where(Product.id == menor))
    assert precio == Decimal("100.00"), "el borrado restauró el precio de A"


# ── El reproceso de ventas históricas ────────────────────────────────────────


async def _venta_del_archivo(
    sm: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    fid: uuid.UUID,
    pid: uuid.UUID,
    qty: int,
) -> uuid.UUID:
    """Una venta importada todavía sin descontar (sin movimiento)."""
    sale_id = uuid.uuid4()
    async with sm() as s:
        s.add(
            SaleEntry(
                id=sale_id,
                tenant_id=tid,
                product_id=pid,
                amount=Decimal("100.00") * qty,
                quantity=qty,
                transaction_date=datetime.now(UTC).replace(tzinfo=None),
                source_upload_id=fid,
            )
        )
        await s.commit()
    return sale_id


async def test_el_reproceso_lee_el_saldo_bajo_candado_y_deja_pendiente_lo_que_no_alcanza(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Stock 5, la caja vende 3, el archivo trae una venta de 4 → queda pendiente
    con stock 2. Antes leía 5, la aplicaba y recortaba el stock a 0."""
    pid = await _producto(sm, tenant_id, "Leche La Serenísima", stock=5)
    fid = await _archivo(sm, tenant_id)
    sale_id = await _venta_del_archivo(sm, tenant_id, fid, pid, 4)
    caja = await _caja_vende(sm, tenant_id, pid)

    async def _reproceso() -> inventory_replay_service.ReplayOutcome:
        async with sm() as s:
            retenido = await s.get(Product, pid)
            resultado = await inventory_replay_service.run_inventory_replay(
                s, tenant_id, fid, apply=True
            )
            assert retenido is not None
            await s.commit()
            return resultado

    tarea = asyncio.create_task(_reproceso())
    await _espera_un_candado(sm, tarea, "el reproceso")
    await caja.commit()
    await caja.close()
    resultado = await asyncio.wait_for(tarea, 30)

    assert resultado.aplicadas == 0
    assert [p.sale_id for p in resultado.sin_stock] == [sale_id]
    assert await _stock(sm, pid) == (2, 2)


async def test_el_reproceso_que_alcanza_descuenta_sobre_el_saldo_vigente(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Stock 10, la caja vende 3, el archivo trae una venta de 4 → 3, y el
    balance también (cargado viejo en la sesión, el lote lo escribía en 6)."""
    pid = await _producto(sm, tenant_id, "Harina Pureza 1kg")
    fid = await _archivo(sm, tenant_id)
    await _venta_del_archivo(sm, tenant_id, fid, pid, 4)
    caja = await _caja_vende(sm, tenant_id, pid)

    async def _reproceso() -> inventory_replay_service.ReplayOutcome:
        async with sm() as s:
            retenidos: list[object] = [
                await s.get(Product, pid),
                *(
                    await s.execute(
                        select(InventoryBalance).where(InventoryBalance.product_id == pid)
                    )
                ).scalars(),
            ]
            resultado = await inventory_replay_service.run_inventory_replay(
                s, tenant_id, fid, apply=True
            )
            assert all(r is not None for r in retenidos)
            await s.commit()
            return resultado

    tarea = asyncio.create_task(_reproceso())
    await _espera_un_candado(sm, tarea, "el reproceso")
    await caja.commit()
    await caja.close()
    resultado = await asyncio.wait_for(tarea, 30)

    assert resultado.aplicadas == 1
    assert await _stock(sm, pid) == (3, 3)


async def test_si_igual_falta_stock_el_lote_no_escribe_nada(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin el pendiente (el gate desactivado), una venta que no alcanza ya no se
    recorta a 0: el lote entero falla y ningún producto queda descontado, ni el
    que sí alcanzaba."""
    alcanza = await _producto(sm, tenant_id, "Azúcar Ledesma", stock=10)
    no_alcanza = await _producto(sm, tenant_id, "Café La Virginia", stock=2)
    fid = await _archivo(sm, tenant_id)
    await _venta_del_archivo(sm, tenant_id, fid, alcanza, 4)
    await _venta_del_archivo(sm, tenant_id, fid, no_alcanza, 5)
    monkeypatch.setattr(
        inventory_replay_service, "rows_without_stock_backing", lambda *_a, **_k: []
    )

    async with sm() as s:
        with pytest.raises(stock_service.InsufficientStockError) as error:
            await inventory_replay_service.run_inventory_replay(s, tenant_id, fid, apply=True)
        # Nada quedó escrito ni pendiente de escribir en la transacción.
        movimientos = await s.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.tenant_id == tenant_id)
        )
        assert movimientos == 0
        assert not [o for o in s.new if isinstance(o, InventoryMovement)]
        await s.rollback()

    assert error.value.product_id == no_alcanza
    assert await _stock(sm, alcanza) == (10, 10)
    assert await _stock(sm, no_alcanza) == (2, 2)


# ── La relectura contra el POS ───────────────────────────────────────────────


async def _relectura_con_dos_productos(
    sm: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    """Un archivo de compras ya importado con dos productos (stock 10 + 2 c/u),
    cuya fila del de id MAYOR va primero: es el movimiento que se anulaba primero.
    Devuelve ``(tenant, archivo, menor, mayor, relectura)``: la relectura trae las
    mismas filas, en el mismo orden, con 4 unidades en vez de 2."""
    tid = uuid.uuid4()
    nombres = {uuid.uuid4(): "Vela Citronela", uuid.uuid4(): "Vela Vainilla"}
    menor, mayor = sorted(nombres, key=str)
    filas = [esc.fila(5, nombres[mayor], "2", "2400"), esc.fila(5, nombres[menor], "2", "2400")]
    fid = await esc.preparar_tenant_y_archivo(sm, tid, filas)
    async with sm() as s:
        for pid, nombre in nombres.items():
            s.add(
                Product(
                    id=pid,
                    tenant_id=tid,
                    name=nombre,
                    sale_price_ars=Decimal("100.00"),
                    stock_units=10,
                )
            )
            await s.flush()
            s.add(InventoryBalance(tenant_id=tid, product_id=pid, current_qty=10, reserved_qty=0))
        await s.commit()
    await esc.importar_archivo(sm, tid, fid, filas)
    relectura = esc.summary(
        [esc.fila(5, nombres[mayor], "4", "4800"), esc.fila(5, nombres[menor], "4", "4800")]
    )
    return tid, fid, menor, mayor, relectura


async def _contra_un_ticket(
    sm: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    menor: uuid.UUID,
    mayor: uuid.UUID,
    operacion: Any,
) -> None:
    """El ticket toma el de id menor, la operación arranca y espera, el ticket
    pide el mayor (con la operación reteniéndolo, Postgres aborta a uno), vende
    3 de cada uno y commitea."""
    caja = sm()
    await stock_service.bloquear_productos(caja, tid, [menor])
    tarea = asyncio.create_task(operacion())
    await _espera_un_candado(sm, tarea, "la operación")
    await asyncio.wait_for(stock_service.bloquear_productos(caja, tid, [mayor]), 10)
    for pid in (menor, mayor):
        await stock_service.decrement_stock(pid, tid, 3, f"pos:{uuid.uuid4()}", caja)
    await caja.commit()
    await caja.close()
    await asyncio.wait_for(tarea, 60)


async def test_la_relectura_no_se_bloquea_en_cruz_con_un_ticket(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tid, fid, menor, mayor, relectura = await _relectura_con_dos_productos(sm)
    try:

        async def _releer() -> None:
            async with sm() as s:
                await reread_service.apply_reread(
                    s,
                    fid,
                    tid,
                    fresh_override=relectura,
                )
                await s.commit()

        await _contra_un_ticket(sm, tid, menor, mayor, _releer)

        # 10 + 2 (import) − 2 (anulado) + 4 (reimport) − 3 (ticket).
        assert await _stock(sm, menor) == (11, 11)
        assert await _stock(sm, mayor) == (11, 11)
    finally:
        async with sm() as s:
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_deshacer_la_relectura_no_se_bloquea_en_cruz_con_un_ticket(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tid, fid, menor, mayor, relectura = await _relectura_con_dos_productos(sm)
    try:
        async with sm() as s:
            resultado = await reread_service.apply_reread(
                s,
                fid,
                tid,
                fresh_override=relectura,
            )
            await s.commit()

        async def _deshacer() -> None:
            async with sm() as s:
                await reread_service.undo_reread(s, resultado.run_id, tid)
                await s.commit()

        await _contra_un_ticket(sm, tid, menor, mayor, _deshacer)

        # Vuelve al import original (12) menos el ticket.
        assert await _stock(sm, menor) == (9, 9)
        assert await _stock(sm, mayor) == (9, 9)
    finally:
        async with sm() as s:
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()
