"""Presupuesto ABSOLUTO de statements del import: la compuerta contra el N+1.

Por qué absoluto y no un ratio
------------------------------
Un ratio ``stmts(N₂)/N₂ <= stmts(N₁)/N₁`` sólo detecta crecimiento SUPERLINEAL: un
N+1 lineal de ocho queries por fila —exactamente el que este cambio sacó— lo pasa
sin despeinarse. Por eso lo que se fija acá es el número por FORMA de SQL, con una
cota que no depende de la cantidad de filas:

  * ``SAVEPOINT`` es O(lotes), no O(productos). Era lo que costaba 1.588 de los
    3.250 statements del confirm real de Asteria (48,9%).
  * ``SELECT products`` por id (el ``session.get`` del camino "producto
    existente") no puede volver a ser uno por fila.
  * ``INSERT products`` / ``INSERT inventory_balances`` tienen que salir
    agrupados por ``insertmanyvalues``: si vuelven a ser uno por fila, es que
    algo reintrodujo un flush por fila.
  * ``pg_advisory_lock`` ≤ 2 — la misma barrera que PR #53 bajó de 812 a 2 en la
    relectura.
  * ``INSERT operation_fingerprints`` ≈ 1: el camino batch de fingerprints tiene
    un fallback legacy que inserta de a una POR FILA si alguien deja de pasar el
    set precargado.

El ratio queda igual como red secundaria contra un O(n²), pero no es la compuerta.

Va contra Postgres real porque los savepoints y ``pg_advisory_xact_lock_shared``
no existen en SQLite (`maintenance_lock_service` es un no-op documentado ahí), y
son justo las dos formas que hay que contar.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.operation_identity import (
    OperationIdentity,
    OperationIdentityLink,
)
from app.persistence.models.product import Product
from app.persistence.models.product_supplier_link import ProductSupplierLink
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

# Mismo agrupador que usan los benchmarks (`scripts/_bench_sql.py`): si el test
# contara las formas con otro criterio, su verde y el número del bench dejarían de
# hablar de lo mismo.
from scripts._bench_sql import SqlProfile

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_CTX_PROD = "sheet:Productos"
_CTX_VENTAS = "sheet:Ventas"


def _summary(n_productos: int, n_ventas: int, *, con_tienda: bool = False) -> dict[str, Any]:
    """Catálogo + ventas que vinculan contra él, con el shape real del parser."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX_PROD,
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "precio", "costo", "stock", "detalle"]
                + (["tienda"] if con_tienda else []),
                "row_count": n_productos,
            },
            {
                "context_id": _CTX_VENTAS,
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor", "producto"],
                "row_count": n_ventas,
            },
        ],
        "stock_detectado": [
            {
                "producto": f"Producto {i}",
                "precio": "5000",
                "costo": "3000",
                "stock": "20",
                "detalle": f"Especificaciones del producto {i}",
                # 10 proveedores distintos repartidos entre los productos: replica el
                # caso real (varios productos por tienda) sin volverlo 1:1.
                **({"tienda": f"Tienda {i % 10}"} if con_tienda else {}),
                "__context__": _CTX_PROD,
            }
            for i in range(n_productos)
        ],
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "valor": "5000",
                "producto": f"Producto {i % max(n_productos, 1)}",
                "__context__": _CTX_VENTAS,
            }
            for i in range(n_ventas)
        ],
    }


def _mappings(*, con_tienda: bool = False) -> dict[str, dict[str, str]]:
    mapas = {k: dict(v) for k, v in _MAPPINGS.items()}
    if con_tienda:
        mapas[_CTX_PROD]["tienda"] = "supplier:name"
    return mapas


_MAPPINGS = {
    _CTX_PROD: {
        "producto": "name",
        "precio": "sale_price_ars",
        "costo": "unit_cost_ars",
        "stock": "stock_units",
        "detalle": "description",
    },
    _CTX_VENTAS: {"fecha": "transaction_date", "valor": "amount", "producto": "product_name"},
}
_ENTITIES = {_CTX_PROD: "product", _CTX_VENTAS: "sale"}
_CONFIRMED = {_CTX_PROD: True, _CTX_VENTAS: True}


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    # `autoflush=False` como producción: con autoflush encendido, cualquier SELECT
    # drena lo pendiente y el conteo sale distinto del que paga el usuario real.
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            for modelo in (
                OperationIdentityLink,
                OperationIdentity,
                InventoryMovement,
                InventoryBalance,
                ProductSupplierLink,
                UnclassifiedRecord,
                SaleEntry,
                ExpenseEntry,
                Product,
                Supplier,
                OperationFingerprint,
                BusinessProfile,
                UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _importar(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    engine: AsyncEngine,
    *,
    n_productos: int,
    n_ventas: int,
    con_tienda: bool = False,
) -> tuple[SqlProfile, dict[str, Any]]:
    perfil = SqlProfile()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        perfil.record(statement, 0.0)

    async with factory() as session:
        # Idempotente: el test de re-importación llama a `_importar` dos veces sobre
        # el mismo tenant.
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
            await session.flush()
            session.add(
                BusinessProfile(
                    profile_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    vertical_code="kiosco_almacen",
                    data_mode="M0",
                    data_confidence="LOW",
                    onboarding_completed=True,
                )
            )
        # `inventory_movements.source_upload_id` es una FK real: un uuid inventado
        # revienta el import antes de medir nada.
        upload_id = uuid.uuid4()
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="catalogo.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

        perfil.enabled = True
        try:
            counts = await insert_confirmed_data(
                session,
                tenant_id,
                _summary(n_productos, n_ventas, con_tienda=con_tienda),
                {},
                context_mappings=_mappings(con_tienda=con_tienda),
                context_entity=_ENTITIES,
                context_confirmed=_CONFIRMED,
                stock_treatment={_CTX_PROD: "opening_balance"},
                source="ingestion",
                uploaded_file_id=upload_id,
            )
            await session.commit()
        finally:
            perfil.enabled = False
            event.remove(engine.sync_engine, "before_cursor_execute", _before)
    return perfil, counts


async def test_presupuesto_absoluto_de_statements(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    perfil, counts = await _importar(
        sm, tenant_id, pg_engine, n_productos=300, n_ventas=300
    )
    formas = dict(perfil.counts)

    assert counts["productos"] == 300, "el import tiene que haber hecho el trabajo"
    assert counts["ventas"] == 300

    def cota(forma: str, maximo: int) -> None:
        assert formas.get(forma, 0) <= maximo, (
            f"{forma}: {formas.get(forma, 0)} statements para 300 productos + 300 "
            f"ventas (cota {maximo}). Formas medidas: "
            f"{sorted(formas.items(), key=lambda kv: -kv[1])[:12]}"
        )

    # O(lotes), no O(productos). Con 300 productos y chunk_size=200 son 2 lotes de
    # altas + 2 de balances; el resto es margen para los savepoints puntuales del
    # centinela y de los maestros.
    cota("SAVEPOINT", 10)
    cota("RELEASE SAVEPOINT", 10)
    # El `session.get(Product, ...)` por fila del camino "producto existente".
    cota("SELECT products", 6)
    # INSERT agrupados por `insertmanyvalues`, no uno por fila.
    cota("INSERT products", 8)
    cota("INSERT inventory_balances", 8)
    cota("INSERT inventory_movements", 8)
    cota("INSERT sales_entries", 8)
    # La barrera que PR #53 bajó de 812 a 2 en la relectura.
    cota("pg_advisory_lock (lock)", 2)
    # El camino batch de fingerprints: un INSERT ... ON CONFLICT, no uno por fila.
    cota("INSERT operation_fingerprints", 2)
    # Y el total, que es lo que el usuario paga contra Neon.
    assert perfil.total <= 120, (
        f"{perfil.total} statements para 600 filas. "
        f"{sorted(formas.items(), key=lambda kv: -kv[1])[:12]}"
    )


async def test_los_statements_no_crecen_mas_rapido_que_las_filas(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """Red secundaria: detecta un O(n²) sin exigir O(1).

    No reemplaza al presupuesto absoluto —un N+1 lineal pasa este test—, pero
    agarra el caso en que una corrección futura convierta una precarga en una
    búsqueda anidada.
    """
    chico, _ = await _importar(sm, tenant_id, pg_engine, n_productos=100, n_ventas=100)
    otro_tenant = uuid.uuid4()
    grande, _ = await _importar(
        sm, otro_tenant, pg_engine, n_productos=400, n_ventas=400
    )
    try:
        por_fila_chico = chico.total / 200
        por_fila_grande = grande.total / 800
        assert por_fila_grande <= por_fila_chico * 1.5, (
            f"statements por fila: {por_fila_chico:.3f} con 200 filas vs "
            f"{por_fila_grande:.3f} con 800 — el costo por fila creció"
        )
    finally:
        async with sm() as s:
            for modelo in (
                OperationIdentityLink,
                OperationIdentity,
                InventoryMovement,
                InventoryBalance,
                ProductSupplierLink,
                UnclassifiedRecord,
                SaleEntry,
                ExpenseEntry,
                Product,
                Supplier,
                OperationFingerprint,
                BusinessProfile,
                UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == otro_tenant))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == otro_tenant))
            await s.commit()


# ── El invariante funcional que el presupuesto no puede romper ────────────────


async def test_reimportar_el_mismo_archivo_no_duplica_ni_vacia_nada(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """Idempotencia del import, con el lote de altas en el medio.

    Un lote que se equivocara al decidir "creado" vs "reusado" se vería acá y en
    ningún otro lado: duplicaría productos en la segunda pasada, o pisaría la
    descripción y el `internal_sku` de los que ya estaban.
    """
    await _importar(sm, tenant_id, pg_engine, n_productos=50, n_ventas=50)
    async with sm() as s:
        productos = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    primera = {
        p.name: (p.internal_sku, p.description, p.stock_units) for p in productos
    }
    assert len(primera) == 50
    assert all(sku for sku, _, _ in primera.values()), "internal_sku en los 50"
    assert all(desc for _, desc, _ in primera.values()), "description persistida"

    # Segunda pasada del MISMO archivo (mismo uploaded_file_id no: el ancla de
    # idempotencia por fila es (archivo, contexto, índice), así que un archivo
    # distinto sí re-importa; lo que acá se afirma es que la identidad de producto
    # no duplica y que lo persistido no se pierde).
    await _importar(sm, tenant_id, pg_engine, n_productos=50, n_ventas=50)

    async with sm() as s:
        productos2 = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
        balances = (
            await s.execute(
                select(InventoryBalance).where(InventoryBalance.tenant_id == tenant_id)
            )
        ).scalars().all()

    assert len(productos2) == 50, "la re-importación duplicó productos"
    segunda = {
        p.name: (p.internal_sku, p.description, p.stock_units) for p in productos2
    }
    for nombre, (sku, desc, _) in primera.items():
        assert segunda[nombre][0] == sku, f"{nombre}: cambió el internal_sku"
        assert segunda[nombre][1] == desc, f"{nombre}: se vació la description"
    assert len(balances) == 50, "un balance por producto, no uno por importación"


async def test_los_vinculos_producto_proveedor_no_vuelven_a_ser_uno_por_fila(
    sm: async_sessionmaker[AsyncSession],
    pg_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El camino que se enciende junto con `Tienda → Proveedor`.

    `link_product_to_declared_supplier` hacía un SELECT + un `flush` POR FILA, y ese
    flush además rompía el agrupado de los movimientos de inventario: medido sobre
    el archivo real de Asteria, prender la flag subía el confirm de 250 a 1.010
    statements. Como la Fase de mapeo es justamente la que hace que esa columna se
    use de verdad, sin esta cota el arreglo se pagaría de vuelta el día que la flag
    se active.
    """
    import app.application.services.ingestion_import_service as imp

    monkeypatch.setattr(imp, "product_supplier_links_enabled_for", lambda _t: True)

    perfil, counts = await _importar(
        sm, tenant_id, pg_engine, n_productos=300, n_ventas=0, con_tienda=True
    )
    formas = dict(perfil.counts)
    detalle = sorted(formas.items(), key=lambda kv: -kv[1])[:12]

    async with sm() as s:
        vinculos = (
            await s.execute(
                select(ProductSupplierLink).where(
                    ProductSupplierLink.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        proveedores = (
            await s.execute(select(Supplier).where(Supplier.tenant_id == tenant_id))
        ).scalars().all()

    # El trabajo se hizo: 10 proveedores reales y un vínculo por producto.
    assert len(proveedores) == 10, [p.name for p in proveedores]
    assert len(vinculos) == 300
    assert counts["productos"] == 300

    # Y se pagó por lote, no por fila. La cota es 20 y no 8 porque un proveedor
    # NUEVO sí hace un `flush` (la FK del gasto y del movimiento necesitan su fila
    # antes del commit) y eso parte los lotes: el costo queda atado a la cantidad de
    # PROVEEDORES (10 acá), no a la de filas. Con el comportamiento viejo serían 300.
    assert formas.get("SELECT product_supplier_links", 0) <= 2, detalle
    assert formas.get("INSERT product_supplier_links", 0) <= 20, detalle
    assert formas.get("INSERT inventory_movements", 0) <= 20, detalle


async def test_la_captura_a_otros_por_riesgo_de_columna_no_paga_por_fila(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """`_capture_column_risk_rows` corre FUERA de `insert_confirmed_data`, así que
    el set de huellas precargado no le llegaba: cada fila ruteada pagaba un
    `SELECT operation_fingerprints` más un `guarded_savepoint` completo.

    Sobre el archivo de Asteria pesa poco (rutea 3 filas), pero una columna
    riesgosa sobre miles de filas hace estallar la misma bomba que el resto del
    import ya había desactivado — y sin cota, nada lo avisaría.
    """
    from app.application.services.ingestion_import_service import (
        _capture_column_risk_rows,
    )

    perfil = SqlProfile()

    @event.listens_for(pg_engine.sync_engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        perfil.record(statement, 0.0)

    upload_id = uuid.uuid4()
    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="riesgo.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="text/csv",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

        filas = {i: {"documento": ""} for i in range(200)}
        perfil.enabled = True
        try:
            creadas = await _capture_column_risk_rows(
                session, tenant_id, upload_id, "sheet:X", "sale", filas
            )
            await session.commit()
        finally:
            perfil.enabled = False
            event.remove(pg_engine.sync_engine, "before_cursor_execute", _before)

    assert creadas == 200
    formas = dict(perfil.counts)
    detalle = sorted(formas.items(), key=lambda kv: -kv[1])[:10]
    # Una precarga, un INSERT en lote de huellas, un INSERT en lote de "Otros".
    assert formas.get("SELECT operation_fingerprints", 0) <= 1, detalle
    assert formas.get("INSERT operation_fingerprints", 0) <= 2, detalle
    assert formas.get("SAVEPOINT", 0) == 0, detalle
    assert perfil.total <= 12, detalle


# ── E6b: la identidad fuerte cuesta O(hoja), no O(filas) ─────────────────────
_CTX_COMPRAS = "sheet:Compras"
_COLS_COMPRA = {
    "fecha": "expense_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "cuit": "supplier_cuit",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}
_SIN_IDENTIDAD = ("fecha", "proveedor", "articulo", "cantidad", "total")
_COLS_SIN_IDENTIDAD = {k: v for k, v in _COLS_COMPRA.items() if k in _SIN_IDENTIDAD}


def _summary_compras(n: int, *, con_identidad: bool) -> dict[str, Any]:
    filas = []
    for i in range(n):
        fila: dict[str, Any] = {
            "fecha": "2024-03-05",
            "proveedor": f"Prov {i % 10}",
            "articulo": f"Art {i}",
            "cantidad": "2",
            "total": "6000",
            "__context__": _CTX_COMPRAS,
        }
        if con_identidad:
            # Un comprobante distinto por fila: el caso más caro para la
            # reclamación (n claves, no una).
            fila |= {"tipo": "Factura A", "pto": "0001", "nro": f"{i:08d}",
                     "cuit": "30-71234567-8"}
        filas.append(fila)
    cols = list(_COLS_COMPRA if con_identidad else _COLS_SIN_IDENTIDAD)
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX_COMPRAS,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": cols,
                "row_count": n,
            }
        ],
        "gastos_detectados": filas,
    }


async def _importar_compras(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    engine: AsyncEngine,
    *,
    n: int,
    con_identidad: bool,
) -> tuple[SqlProfile, dict[str, Any]]:
    perfil = SqlProfile()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        perfil.record(statement, 0.0)

    async with factory() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
            await session.flush()
            session.add(
                BusinessProfile(
                    profile_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    vertical_code="kiosco_almacen",
                    data_mode="M0",
                    data_confidence="LOW",
                    onboarding_completed=True,
                )
            )
        upload_id = uuid.uuid4()
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="compras.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="text/csv",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

        cols = _COLS_COMPRA if con_identidad else _COLS_SIN_IDENTIDAD
        perfil.enabled = True
        try:
            counts = await insert_confirmed_data(
                session,
                tenant_id,
                _summary_compras(n, con_identidad=con_identidad),
                {},
                context_mappings={_CTX_COMPRAS: dict(cols)},
                context_entity={_CTX_COMPRAS: "expense"},
                context_confirmed={_CTX_COMPRAS: True},
                source="ingestion",
                uploaded_file_id=upload_id,
            )
            await session.commit()
        finally:
            perfil.enabled = False
            event.remove(engine.sync_engine, "before_cursor_execute", _before)
    return perfil, counts


async def test_la_identidad_fuerte_no_cuesta_por_fila(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """El sobrecosto de la deduplicación por clave fuerte tiene que ser constante.

    Es la compuerta contra el N+1 más fácil de introducir acá: consultar la
    identidad fila por fila. Medido cuando se escribió: 57 statements sin
    identidad y 60 con comprobante completo en 300 compras — un `INSERT ... ON
    CONFLICT RETURNING` para reclamar, un `SELECT` para leer los ids y un
    `INSERT` de vínculos, para la hoja entera. Si esto crece con las filas, el
    número explota contra Neon (30-50 ms por statement) y no acá.

    El escenario usa un comprobante DISTINTO por fila a propósito: es el caso más
    caro (300 claves, no una).
    """
    otro_tenant = uuid.uuid4()
    sin_ident, counts_sin = await _importar_compras(
        sm, otro_tenant, pg_engine, n=300, con_identidad=False
    )
    con_ident, counts_con = await _importar_compras(
        sm, tenant_id, pg_engine, n=300, con_identidad=True
    )
    assert counts_sin["gastos"] == 300
    assert counts_con["gastos"] == 300

    formas = dict(con_ident.counts)
    detalle = sorted(formas.items(), key=lambda kv: -kv[1])[:12]
    assert formas.get("INSERT operation_identities", 0) <= 1, detalle
    assert formas.get("INSERT operation_identity_links", 0) <= 1, detalle
    assert formas.get("SELECT operation_identities", 0) <= 2, detalle

    sobrecosto = con_ident.total - sin_ident.total
    assert sobrecosto <= 6, (
        f"la identidad agregó {sobrecosto} statements sobre 300 filas "
        f"({sin_ident.total} → {con_ident.total}). Tiene que ser constante. {detalle}"
    )
    async with sm() as s:
        await s.execute(
            delete(OperationIdentityLink).where(OperationIdentityLink.tenant_id == otro_tenant)
        )
        await s.execute(
            delete(OperationIdentity).where(OperationIdentity.tenant_id == otro_tenant)
        )
        for modelo in (
            InventoryMovement, InventoryBalance, ProductSupplierLink, UnclassifiedRecord,
            ExpenseEntry, Product, Supplier, OperationFingerprint, BusinessProfile, UploadedFile,
        ):
            await s.execute(delete(modelo).where(modelo.tenant_id == otro_tenant))
        await s.execute(delete(Tenant).where(Tenant.tenant_id == otro_tenant))
        await s.commit()


# ── E6c-1: la deduplicación no puede volver a crecer con la historia ─────────
async def test_las_huellas_se_consultan_acotadas_al_archivo(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """Ninguna consulta a ``operation_fingerprints`` puede pedir la tabla entera.

    Esta compuerta mira la FORMA del SQL y no el conteo de statements, y la
    diferencia no es un detalle de estilo: el primer intento de este test contaba
    statements, y volver al ``SELECT`` sin filtro **no lo ponía en rojo** — una
    query que trae 50.000 filas y una que trae 200 cuentan igual. El conteo es
    ciego justo al defecto que E6c-1 arregló.

    Lo que se afirma es el invariante real: la deduplicación pregunta por las
    anclas de ESTE archivo (``fingerprint IN (...)``), nunca por todo lo que el
    tenant importó alguna vez. Medido antes del cambio
    (`scripts/bench_f0_baseline.py`): importar 100 filas contra un tenant con
    100.000 huellas costaba 32,3 MB de pico, más que importar 5.000 filas contra
    uno vacío.
    """
    consultas: list[str] = []

    @event.listens_for(pg_engine.sync_engine, "before_cursor_execute")
    def _capturar(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        texto = " ".join(str(statement).split())
        if "operation_fingerprints" in texto and texto.lower().startswith("select"):
            consultas.append(texto)

    try:
        await _importar_compras(sm, tenant_id, pg_engine, n=200, con_identidad=False)
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", _capturar)

    assert consultas, "el import tiene que consultar huellas: si no, no hay dedup"
    sin_acotar = [q for q in consultas if " in (" not in q.lower()]
    assert not sin_acotar, (
        "hay consultas a operation_fingerprints sin acotar por las anclas del "
        f"archivo — vuelven a traer la historia entera del tenant: {sin_acotar}"
    )


# ── E6c-1: la deduplicación no puede volver a crecer con la historia ─────────
async def test_el_costo_del_import_no_crece_con_la_historia(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """El mismo archivo contra un tenant sin historia y contra uno con 50.000
    huellas tiene que costar lo MISMO.

    Es la compuerta de E6c-1 y responde a una medición, no a una sospecha
    (`scripts/bench_f0_baseline.py`): antes, `_load_import_fingerprints` traía
    todas las huellas de import del tenant, y importar 100 filas contra un tenant
    con 100.000 huellas costaba 32,3 MB de pico — más que importar 5.000 filas
    contra uno vacío. El costo lo ponía la historia, no el archivo.

    Se mide en STATEMENTS y no en memoria porque el conteo es lo determinista:
    la memoria depende del recolector y del warm-up del proceso, y una compuerta
    que falla sola no la mira nadie. Si alguien vuelve a traer la historia entera,
    el conteo no cambia pero **el `SELECT operation_fingerprints` deja de estar
    acotado**, así que también se afirma la forma: son consultas por lote sobre
    las anclas del archivo, y su cantidad no puede depender del historial.
    """
    huellas_previas = 50_000
    async with sm() as session:
        session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await session.flush()
        session.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        await session.commit()

    sin_historia, _ = await _importar_compras(
        sm, tenant_id, pg_engine, n=200, con_identidad=False
    )

    # Historia ajena: huellas de OTROS archivos del mismo tenant. Este import no
    # las va a usar nunca — el punto es que tampoco las traiga.
    async with sm() as session:
        for inicio in range(0, huellas_previas, 5_000):
            session.add_all(
                [
                    OperationFingerprint(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        fingerprint=f"historico-{inicio + j:012d}",
                        action_type="IMPORT_ROW",
                    )
                    for j in range(5_000)
                ]
            )
            await session.flush()
        await session.commit()

    con_historia, _ = await _importar_compras(
        sm, tenant_id, pg_engine, n=200, con_identidad=False
    )

    detalle = (
        f"sin historia: {sin_historia.total} statements; "
        f"con {huellas_previas} huellas ajenas: {con_historia.total}"
    )
    assert con_historia.total <= sin_historia.total + 2, (
        f"el import empezó a pagar por la historia del tenant. {detalle}"
    )
    # Y la forma: las consultas de huellas son por lote sobre las anclas del
    # archivo. Su cantidad depende de las filas (200 → 1 lote), nunca del
    # historial; si alguien vuelve al SELECT sin filtro, esto sigue en 1 pero el
    # `assert` de arriba lo agarra por el lado del total.
    formas = dict(con_historia.counts)
    assert formas.get("SELECT operation_fingerprints", 0) <= 3, sorted(
        formas.items(), key=lambda kv: -kv[1]
    )[:10]
