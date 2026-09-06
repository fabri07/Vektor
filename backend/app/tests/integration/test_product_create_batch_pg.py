"""El fallback del lote de altas de producto: atomicidad e identidad.

`ProductCreateBatch` cambió el alta de producto de "un savepoint por producto" a
"un savepoint por lote". Es la parte más riesgosa del cambio, porque el camino de
excepción —el lote choca contra un índice único— revierte el LOTE ENTERO: el
savepoint expunga todo lo pendiente, no sólo el producto que colisionó.

Lo que estos tests fijan es que ese camino:

  * no pierde el resto del lote,
  * no fusiona productos de identidad distinta,
  * distingue QUÉ restricción falló (una FK rota o un NOT NULL no puede leerse
    como "ya existía"),
  * conserva la semántica de `sku` / `barcode` / `internal_sku`,
  * no deja objetos inválidos en la sesión después del rollback al savepoint,
  * corre el post-trabajo con la identidad FINAL, nunca con la descartada.

Va contra Postgres real porque los índices que disparan el conflicto son
**parciales** (`WHERE is_active AND sku_normalized <> ''`) y el clasificador de
`guarded_savepoint` reconoce el constraint por su nombre — dos cosas que un
SQLite en memoria no reproduce.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from app.application.services.product_identity import (
    ProductCreateBatch,
    ProductIdentityConflictError,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


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
    # `autoflush=False` como producción: con autoflush encendido, un SELECT
    # cualquiera drenaría lo pendiente y el conflicto saltaría en otro momento.
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            await s.execute(delete(Product).where(Product.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


def _producto(
    tenant_id: uuid.UUID,
    nombre: str,
    *,
    sku: str | None = None,
    barcode: str | None = None,
) -> Product:
    return Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=nombre,
        sku=sku,
        barcode=barcode,
        sale_price_ars=Decimal("100.00"),
        stock_units=0,
    )


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    session.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
    await session.flush()


async def test_lote_sin_conflicto_crea_todo_y_corre_el_post_trabajo(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with sm() as session:
        await _tenant(session, tenant_id)
        batch = ProductCreateBatch(session, chunk_size=10)
        vistos: list[tuple[uuid.UUID, bool]] = []

        async def post(p: Product, creado: bool) -> None:
            vistos.append((p.id, creado))

        productos = [_producto(tenant_id, f"Prod {i}", sku=f"SKU-{i}") for i in range(5)]
        for p in productos:
            batch.encolar(p, post)
        sustituciones = await batch.flush()
        await session.commit()

    assert sustituciones == {}, "sin conflicto no hay sustituciones"
    assert [c for _, c in vistos] == [True] * 5
    assert {pid for pid, _ in vistos} == {p.id for p in productos}
    async with sm() as s:
        guardados = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    assert len(guardados) == 5
    # `internal_sku` lo deriva el listener del id: sigue existiendo para todos.
    assert all(p.internal_sku for p in guardados)


async def test_un_sku_ocupado_no_se_lleva_puesto_el_resto_del_lote(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El caso que el fallback existe para cubrir: otra transacción ocupó el SKU
    entre que se cargó el índice de identidad y el flush del lote."""
    async with sm() as session:
        await _tenant(session, tenant_id)
        # El ocupante: ya está en la base cuando el lote intenta insertarse.
        ocupante = _producto(tenant_id, "Ocupante", sku="REPETIDO")
        session.add(ocupante)
        await session.flush()
        ocupante_id = ocupante.id

        batch = ProductCreateBatch(session, chunk_size=10)
        resultados: dict[str, tuple[uuid.UUID, bool]] = {}

        async def post_de(nombre: str) -> Any:
            async def post(p: Product, creado: bool) -> None:
                resultados[nombre] = (p.id, creado)

            return post

        colisiona = _producto(tenant_id, "Colisiona", sku="REPETIDO")
        otros = [_producto(tenant_id, f"Otro {i}", sku=f"LIBRE-{i}") for i in range(3)]
        encolado_id = colisiona.id

        batch.encolar(colisiona, await post_de("colisiona"))
        for i, p in enumerate(otros):
            batch.encolar(p, await post_de(f"otro{i}"))
        sustituciones = await batch.flush()
        await session.commit()

    # 1. El resto del lote NO se pierde.
    async with sm() as s:
        guardados = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    nombres = {p.name for p in guardados}
    assert {"Otro 0", "Otro 1", "Otro 2", "Ocupante"} <= nombres
    # 2. NO se fusionan identidades distintas: el que colisionó no se creó, y el
    #    ocupante sigue siendo uno solo (no se duplicó ni se pisó).
    assert "Colisiona" not in nombres
    assert len([p for p in guardados if p.sku == "REPETIDO"]) == 1
    assert len(guardados) == 4
    # 3. El post-trabajo del que colisionó recibió el producto EXISTENTE, no el
    #    descartado: si recibiera el encolado, escribiría movimientos y vínculos
    #    contra un id que no existe.
    assert resultados["colisiona"] == (ocupante_id, False)
    # Y el id encolado se reporta como sustituido, que es lo que le permite al
    # import corregir sus índices en memoria antes de que una venta posterior
    # resuelva contra un producto que no se insertó.
    assert list(sustituciones) == [encolado_id]
    reemplazo = sustituciones[encolado_id]
    assert reemplazo is not None
    assert reemplazo.id == ocupante_id
    # 4. Los demás sí se crearon.
    assert all(resultados[f"otro{i}"][1] is True for i in range(3))


async def test_barcode_y_sku_de_duenos_distintos_no_se_reusan(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Ambigüedad real: no hay "el existente" que reusar. El lote no la decide
    solo — se la pasa al caller, que en el import manda la fila a "Otros"."""
    async with sm() as session:
        await _tenant(session, tenant_id)
        dueno_sku = _producto(tenant_id, "Dueño del SKU", sku="S-1")
        dueno_barcode = _producto(tenant_id, "Dueño del barcode", barcode="7791234567890")
        session.add_all([dueno_sku, dueno_barcode])
        await session.flush()

        batch = ProductCreateBatch(session, chunk_size=10)
        ambiguos: list[ProductIdentityConflictError] = []
        corrio_post = False

        async def post(p: Product, creado: bool) -> None:
            nonlocal corrio_post
            corrio_post = True

        async def al_ser_ambiguo(c: ProductIdentityConflictError) -> None:
            ambiguos.append(c)

        candidato = _producto(
            tenant_id, "Ambiguo", sku="S-1", barcode="7791234567890"
        )
        encolado_id = candidato.id
        batch.encolar(candidato, post, al_ser_ambiguo=al_ser_ambiguo)
        sustituciones = await batch.flush()
        await session.commit()

    assert len(ambiguos) == 1
    assert ambiguos[0].ambiguous is True
    # El post-trabajo NO corrió: no hay identidad final que darle.
    assert corrio_post is False
    # Y el id encolado queda marcado como muerto (sin reemplazo) para que el
    # caller saque sus índices en memoria en vez de apuntar a un producto
    # inexistente.
    assert sustituciones == {encolado_id: None}
    async with sm() as s:
        guardados = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    assert {p.name for p in guardados} == {"Dueño del SKU", "Dueño del barcode"}


async def test_una_violacion_que_no_es_de_identidad_se_propaga(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Una FK rota NO puede leerse como "ya existía": eso persistiría una
    conclusión falsa en silencio y saltearía la fila."""
    async with sm() as session:
        await _tenant(session, tenant_id)
        batch = ProductCreateBatch(session, chunk_size=10)

        async def post(p: Product, creado: bool) -> None:  # pragma: no cover
            raise AssertionError("el post-trabajo no debería correr")

        # tenant_id inexistente → viola la FK de products.tenant_id.
        huerfano = _producto(uuid.uuid4(), "Huérfano", sku="X-1")
        batch.encolar(huerfano, post)
        with pytest.raises(IntegrityError):
            await batch.flush()
        await session.rollback()


async def test_el_rollback_del_savepoint_no_deja_objetos_invalidos_en_la_sesion(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """`_restore_snapshot` expunga TODO lo pendiente al revertir el savepoint. El
    fallback tiene que dejar la sesión coherente: lo creado, persistente; lo
    descartado, fuera — nunca un transient huérfano que un flush posterior
    intente insertar de nuevo."""
    async with sm() as session:
        await _tenant(session, tenant_id)
        ocupante = _producto(tenant_id, "Ocupante", sku="DUP")
        session.add(ocupante)
        await session.flush()

        batch = ProductCreateBatch(session, chunk_size=10)

        async def post(p: Product, creado: bool) -> None:
            return None

        colisiona = _producto(tenant_id, "Colisiona", sku="DUP")
        superviviente = _producto(tenant_id, "Superviviente", sku="OK")
        batch.encolar(colisiona, post)
        batch.encolar(superviviente, post)
        await batch.flush()

        assert sa_inspect(superviviente).persistent, "el superviviente debe quedar en la DB"
        assert not sa_inspect(colisiona).persistent, "el descartado no puede estar persistente"
        assert colisiona not in session.new, "un transient huérfano se re-insertaría al flushear"

        # Un flush posterior no vuelve a intentar el INSERT descartado.
        await session.flush()
        await session.commit()

    async with sm() as s:
        guardados = (
            await s.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
    assert {p.name for p in guardados} == {"Ocupante", "Superviviente"}
