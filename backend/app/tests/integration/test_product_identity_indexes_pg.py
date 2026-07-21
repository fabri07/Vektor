"""Integración PostgreSQL de los tres uniques de F5-B (semántica + migración).

Por qué contra Postgres real
----------------------------
Dos cosas que la suite SQLite NO puede probar, y son justo las que importan:

1. **El predicado parcial.** SQLite acepta ``CREATE UNIQUE INDEX ... WHERE`` pero
   con su propia semántica; el índice que corre en producción es el que compila
   Postgres. Que "activo+inactivo con el mismo SKU" sea legal es una decisión de
   negocio (el historial de bajas) que tiene que sostenerla el motor real.
2. **La migración.** ``CREATE INDEX CONCURRENTLY``, el estado ``INVALID`` que deja
   un fallo, y ``pg_get_expr`` para comparar predicados NO existen en SQLite. Ver
   ``[[feedback_sqlite_masks_postgres]]``.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_product_identity_indexes_pg.py -v --no-cov

Aislamiento bajo xdist: los tests de SEMÁNTICA usan ``tenant_id`` únicos y borran
solo sus filas. Los que mutan DDL NO tocan los índices de producción —crean uno
con nombre propio (``uq_f5b_probe_*``)—, así que no se pisan con otro worker que
esté corriendo contra los reales.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.persistence.db.base import Base
from app.persistence.models.inventory import InventoryBalance
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F50  # "VEKTOR" + F5-B


def _load_migration() -> Any:
    """Importa la migración por path: ``versions/`` no es un paquete importable."""
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20260802_0001_product_identity_unique_indexes.py"
    )
    spec = importlib.util.spec_from_file_location("_f5b_migration", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tables: list[Table] = [
        cast("Table", Tenant.__table__),
        cast("Table", Product.__table__),
        cast("Table", InventoryBalance.__table__),
    ]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        # checkfirst: en CI el schema ya lo creó `alembic upgrade head` (que es
        # quien PRUEBA la migración); acá queda no-op.
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
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
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with maker() as s:
            await s.execute(delete(InventoryBalance).where(InventoryBalance.tenant_id == tenant_id))
            await s.execute(delete(Product).where(Product.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _seed_tenant(maker: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> None:
    async with maker() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()


def _producto(tid: uuid.UUID, **kw: Any) -> Product:
    base: dict[str, Any] = {
        "tenant_id": tid,
        "name": "Producto",
        "sale_price_ars": Decimal("100"),
    }
    base.update(kw)
    return Product(**base)


# ── Semántica del índice parcial ─────────────────────────────────────────────


async def test_dos_activos_con_el_mismo_sku_son_rechazados(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        s.add(_producto(tenant_id, sku="S1", sku_normalized="s1"))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with sm() as s:
            s.add(_producto(tenant_id, sku="S1", sku_normalized="s1"))
            await s.commit()


async def test_mismo_sku_en_tenants_distintos_es_legal(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El índice es POR TENANT: dos comercios pueden usar el mismo código."""
    otro = uuid.uuid4()
    await _seed_tenant(sm, tenant_id)
    await _seed_tenant(sm, otro)
    try:
        async with sm() as s:
            s.add(_producto(tenant_id, sku="S1", sku_normalized="s1"))
            s.add(_producto(otro, sku="S1", sku_normalized="s1"))
            await s.commit()
        async with sm() as s:
            total = (
                await s.execute(
                    text("SELECT count(*) FROM products WHERE sku_normalized = 's1'")
                )
            ).scalar_one()
        assert total >= 2
    finally:
        async with sm() as s:
            await s.execute(delete(Product).where(Product.tenant_id == otro))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == otro))
            await s.commit()


async def test_activo_e_inactivo_con_la_misma_clave_es_legal(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dar de baja y volver a crear con el mismo SKU es un flujo normal."""
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        s.add(_producto(tenant_id, sku="S1", sku_normalized="s1", is_active=False))
        await s.flush()
        s.add(_producto(tenant_id, sku="S1", sku_normalized="s1", is_active=True))
        await s.commit()


async def test_dos_inactivos_con_la_misma_clave_son_legales(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El historial de bajas se acumula: N inactivos con el mismo SKU."""
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        for _ in range(3):
            s.add(_producto(tenant_id, sku="S1", sku_normalized="s1", is_active=False))
        await s.commit()


async def test_null_queda_fuera_del_parcial(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Sin clave fuerte no hay identidad que colisionar: N activos sin SKU.

    Ojo: acá NO se puede probar el ``''`` pasando ``sku=""``. El listener
    ``before_insert`` del ORM recomputa ``sku_normalized`` con ``normalize_sku``,
    que devuelve ``None`` para ``""`` y para espacios — las filas terminarían en
    NULL y el test diría "vacío" probando NULL otra vez. El ``''`` va aparte, por
    SQL crudo.
    """
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        s.add(_producto(tenant_id, sku=None))
        s.add(_producto(tenant_id, sku=None))
        await s.commit()


async def test_string_vacio_queda_fuera_del_parcial(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos activos con ``sku_normalized = ''`` conviven: el ``<> ''`` los excluye.

    Se inserta por SQL CRUDO a propósito: el ``''`` es INALCANZABLE por el ORM
    (``normalize_sku`` lo convierte en ``None``), así que la única forma de tener
    esas filas es la que las produjo en la vida real — datos legacy anteriores a
    los normalizadores, o un INSERT directo. Justamente por eso el predicado lleva
    ``AND sku_normalized <> ''``: sin esa cláusula, dos filas legacy vacías
    colisionarían entre sí y romperían el deploy de una base histórica.

    Medido por mutación contra Postgres real: quitándole el ``<> ''`` al predicado,
    los otros 15 tests de este archivo pasan igual. Este es el único que lo cubre.
    """
    await _seed_tenant(sm, tenant_id)
    inserta = text(
        "INSERT INTO products (id, tenant_id, name, sale_price_ars, stock_units, "
        "is_active, sku, sku_normalized, created_at, updated_at) "
        "VALUES (:id, :t, 'Legacy', 100, 0, true, '', '', now(), now())"
    )
    async with sm() as s:
        for _ in range(2):
            await s.execute(inserta, {"id": uuid.uuid4(), "t": tenant_id})
        await s.commit()

    async with sm() as s:
        total = (
            await s.execute(
                text(
                    "SELECT count(*) FROM products "
                    "WHERE tenant_id = :t AND sku_normalized = ''"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert total == 2, "el parcial no debe alcanzar a los normalizados vacíos"


async def test_dos_activos_con_el_mismo_barcode_son_rechazados(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """EAN-13 real y no un ``"779"`` cualquiera: ``normalize_barcode`` exige 8–14
    dígitos y un listener del ORM recalcula la columna al insertar, así que un
    barcode corto cae a NULL y queda FUERA del índice parcial — el test pasaría
    en verde sin probar el índice."""
    ean = "7791234567890"
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        s.add(_producto(tenant_id, barcode=ean))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with sm() as s:
            s.add(_producto(tenant_id, barcode=ean))
            await s.commit()


async def test_balance_duplicado_es_rechazado(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """``uq_inventory_balances_tenant_product`` NO es parcial: nunca dos balances."""
    await _seed_tenant(sm, tenant_id)
    async with sm() as s:
        prod = _producto(tenant_id, sku="S1", sku_normalized="s1")
        s.add(prod)
        await s.flush()
        pid = prod.id
        s.add(InventoryBalance(tenant_id=tenant_id, product_id=pid, current_qty=1))
        await s.commit()
    with pytest.raises(IntegrityError):
        async with sm() as s:
            s.add(InventoryBalance(tenant_id=tenant_id, product_id=pid, current_qty=2))
            await s.commit()


# ── Lógica de la migración ───────────────────────────────────────────────────
#
# Estos tests ejercitan ``_ensure_index`` creando índices DE VERDAD. Van contra una
# tabla scratch con nombre único por test, NUNCA contra ``products``: una versión
# anterior creaba el índice homónimo sobre la tabla real y resultó flaky bajo xdist
# —el ``CREATE UNIQUE INDEX`` total fallaba cuando otro worker tenía, legalmente,
# tres INACTIVOS con el mismo SKU—. Un test que depende de lo que otro worker tenga
# en la tabla no prueba lo que dice probar.


@pytest_asyncio.fixture
async def scratch(pg_engine: AsyncEngine) -> AsyncGenerator[str, None]:
    """Tabla con la forma de ``products`` y nombre único: aislamiento total."""
    nombre = f"f5b_probe_{uuid.uuid4().hex[:12]}"
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text(
                f"CREATE TABLE {nombre} "
                "(tenant_id uuid, sku_normalized text, is_active boolean)"
            )
        )
    try:
        yield nombre
    finally:
        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"DROP TABLE IF EXISTS {nombre} CASCADE"))


def test_canonical_iguala_lo_que_renderiza_postgres() -> None:
    """PG reconstruye el predicado con paréntesis y casts propios."""
    nuestro = "is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''"
    de_pg = "(is_active AND (sku_normalized IS NOT NULL) AND ((sku_normalized)::text <> ''::text))"
    assert mig._canonical(nuestro) == mig._canonical(de_pg)


def test_canonical_distingue_predicados_realmente_distintos() -> None:
    """La normalización no puede ser tan laxa que tape un predicado incorrecto."""
    correcto = "is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''"
    sin_is_active = "sku_normalized IS NOT NULL AND sku_normalized <> ''"
    otra_columna = "is_active AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''"
    assert mig._canonical(correcto) != mig._canonical(sin_is_active)
    assert mig._canonical(correcto) != mig._canonical(otra_columna)


async def test_preflight_aborta_con_duplicados_y_dice_como_arreglarlo(
    pg_engine: AsyncEngine,
) -> None:
    """Con datos sucios aborta nombrando el problema y el comando de F3.

    No se puede ensuciar ``products`` de verdad: el índice de producción ya está
    puesto y rechaza el duplicado —que es justo lo que queremos que pase—. Se usan
    TABLAS TEMPORALES homónimas: el ``pg_temp`` de la sesión precede a ``public``
    en el ``search_path``, así que las queries del preflight (que nombran
    ``products``/``inventory_balances`` sin calificar) leen las temporales. Valida
    la QUERY, que es lo único de esta pieza que puede estar mal, y es
    session-local → no se pisa con otro worker de xdist.
    """
    async with pg_engine.connect() as conn:
        await conn.execute(
            text(
                "CREATE TEMP TABLE products "
                "(tenant_id uuid, sku_normalized text, barcode_normalized text, "
                " is_active boolean)"
            )
        )
        await conn.execute(
            text("CREATE TEMP TABLE inventory_balances (tenant_id uuid, product_id uuid)")
        )
        tid = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO products VALUES "
                "(:t, 's1', NULL, true), (:t, 's1', NULL, true), "   # dup de SKU
                "(:t, 's2', NULL, false), (:t, 's2', NULL, false), "  # inactivos: NO cuenta
                "(:t, '', NULL, true), (:t, '', NULL, true), "        # vacío: NO cuenta
                "(:t, NULL, NULL, true), (:t, NULL, NULL, true)"      # NULL: NO cuenta
            ),
            {"t": tid},
        )

        with pytest.raises(RuntimeError, match="ABORTADA") as exc:
            await conn.run_sync(lambda c: mig._run_preflight(c))

        mensaje = str(exc.value)
        assert "SKU activo duplicado: 1 grupo(s)" in mensaje, (
            "solo el grupo de ACTIVOS con clave no vacía puede bloquear: "
            "inactivos, '' y NULL están fuera del predicado del índice"
        )
        assert "código de barras" not in mensaje, "no hay duplicados de barcode"
        assert "dedupe_products_by_name.py" in mensaje, "tiene que decir cómo arreglarlo"


async def test_preflight_no_bloquea_con_datos_limpios(pg_engine: AsyncEngine) -> None:
    """Los normalizados vacíos/NULL y los duplicados entre INACTIVOS no bloquean.

    Un precheck más amplio que su índice frenaría un deploy legítimo por datos que
    el índice acepta sin problema. Es el falso positivo que hay que evitar.
    """
    async with pg_engine.connect() as conn:
        await conn.execute(
            text(
                "CREATE TEMP TABLE products "
                "(tenant_id uuid, sku_normalized text, barcode_normalized text, "
                " is_active boolean)"
            )
        )
        await conn.execute(
            text("CREATE TEMP TABLE inventory_balances (tenant_id uuid, product_id uuid)")
        )
        tid = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO products VALUES "
                "(:t, 's1', NULL, false), (:t, 's1', NULL, false), "
                "(:t, '', '', true), (:t, '', '', true), "
                "(:t, NULL, NULL, true), (:t, NULL, NULL, true)"
            ),
            {"t": tid},
        )
        await conn.run_sync(lambda c: mig._run_preflight(c))  # no levanta


def _spec_para(tabla: str, nombre: str) -> Any:
    return mig._IndexSpec(
        name=nombre,
        table=tabla,
        columns=("tenant_id", "sku_normalized"),
        predicate="is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''",
    )


async def test_ensure_index_es_idempotente_sobre_uno_valido(
    pg_engine: AsyncEngine, scratch: str
) -> None:
    """Segunda corrida sobre un índice válido y compatible: no explota ni recrea.

    Es el caso del deploy repetido (o el reintento tras un fallo posterior).
    """
    spec = _spec_para(scratch, f"uq_{scratch}_idem")
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.run_sync(lambda c: mig._ensure_index(c, spec))
        await conn.run_sync(lambda c: mig._ensure_index(c, spec))  # 2ª vez: no-op
        fila = (
            (await conn.execute(mig._EXISTING_INDEX_SQL, {"name": spec.name})).mappings().first()
        )
        assert fila is not None
        assert fila["is_unique"] and fila["is_valid"]


async def test_ensure_index_aborta_ante_homonimo_incompatible(
    pg_engine: AsyncEngine, scratch: str
) -> None:
    """Un índice con el mismo nombre pero otro predicado NO se pisa: se aborta."""
    nombre = f"uq_{scratch}_incompat"
    spec = _spec_para(scratch, nombre)
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        # Homónimo SIN el predicado parcial → incompatible.
        await conn.execute(
            text(f"CREATE UNIQUE INDEX {nombre} ON {scratch} (tenant_id, sku_normalized)")
        )
        with pytest.raises(RuntimeError, match="ABORTADA"):
            await conn.run_sync(lambda c: mig._ensure_index(c, spec))
        # Y NO lo borró: dropear a ciegas podría matar un índice que alguien creó
        # a mano por otra razón.
        existe = (
            await conn.execute(
                text("SELECT count(*) FROM pg_class WHERE relname = :n"), {"n": nombre}
            )
        ).scalar_one()
        assert existe == 1


async def test_ensure_index_limpia_un_invalido_previo(
    pg_engine: AsyncEngine, scratch: str
) -> None:
    """Un INVALID de una corrida anterior no bloquea el reintento: se dropea y rehace."""
    spec = _spec_para(scratch, f"uq_{scratch}_invalido")
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        # Se fabrica un INVALID real: CONCURRENTLY sobre datos duplicados falla y
        # deja el índice inválido (es EXACTAMENTE la basura del deploy anterior).
        await conn.execute(
            text(f"INSERT INTO {scratch} VALUES (:t,'s1',true), (:t,'s1',true)"),
            {"t": uuid.uuid4()},
        )
        with pytest.raises(IntegrityError):
            await conn.execute(text(spec.create_sql()))
        fila = (
            (await conn.execute(mig._EXISTING_INDEX_SQL, {"name": spec.name})).mappings().first()
        )
        assert fila is not None and not fila["is_valid"], "debería haber quedado INVALID"

        # Ahora sí, datos limpios: _ensure_index tiene que barrer el inválido y crear.
        await conn.execute(text(f"DELETE FROM {scratch}"))
        await conn.run_sync(lambda c: mig._ensure_index(c, spec))
        fila = (
            (await conn.execute(mig._EXISTING_INDEX_SQL, {"name": spec.name})).mappings().first()
        )
        assert fila is not None
        assert fila["is_unique"] and fila["is_valid"]


async def test_create_fallido_no_deja_indice_invalido(
    pg_engine: AsyncEngine, scratch: str
) -> None:
    """La carrera que el preflight no puede cerrar: duplicados al momento del CREATE.

    ``CONCURRENTLY`` que falla NO revierte —deja el índice INVALID—, y ese INVALID
    con el nombre esperado haría fallar el reintento del próximo deploy por nombre
    duplicado. Se prueba que el ``except`` lo limpia y re-lanza el error original
    sin enmascararlo.
    """
    spec = _spec_para(scratch, f"uq_{scratch}_falla")
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text(f"INSERT INTO {scratch} VALUES (:t,'s1',true), (:t,'s1',true)"),
            {"t": uuid.uuid4()},
        )
        with pytest.raises(IntegrityError):
            await conn.run_sync(lambda c: mig._ensure_index(c, spec))
        restante = (
            await conn.execute(
                text("SELECT count(*) FROM pg_class WHERE relname = :n"), {"n": spec.name}
            )
        ).scalar_one()
        assert restante == 0, "el índice INVALID tiene que haberse limpiado"
