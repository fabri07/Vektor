"""La carrera que el lote de altas puede perder: el alta encolada que se sustituye.

`ProductCreateBatch` encola las altas y corre su post-trabajo recién cuando la
identidad final está resuelta. Si al flushear resulta que otra transacción ocupó
el SKU en el medio, el alta encolada se DESCARTA y el post-trabajo recibe el
producto que quedó.

Pero el import además guarda ese alta encolada en sus índices EN MEMORIA (es lo
que evita que dos filas del mismo catálogo encolen dos altas del mismo producto).
La fila siguiente con la misma identidad lo encuentra ahí y fuerza el flush antes
de tocarlo —el merge escribe un movimiento que lo referencia por FK—; lo que
faltaba era **volver a leer el índice después de ese flush**: si el alta se
sustituyó, la variable local seguía apuntando al transient descartado, y mergear
contra él pierde la fila y deja un ``inventory_movements.product_id`` apuntando a
un producto que nunca se insertó.

Va contra Postgres real por dos razones: la FK de ``inventory_movements`` es lo
que convierte el bug en un error visible, y el índice único parcial sobre
``sku_normalized`` —el que dispara la sustitución— tampoco existe en SQLite.

La carrera se simula como la documenta ``add_product_or_reuse``: el ocupante ya
está en la base, pero el índice de identidad que el import precargó NO lo tiene
(es exactamente el estado de un producto insertado por otra transacción entre la
precarga y el flush).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import ingestion_import_service as iis
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_CTX = "sheet:Catalogo"
_SKU = "SKU-CARRERA"


def _summary() -> dict[str, Any]:
    """Catálogo con DOS filas de la MISMA identidad (mismo nombre y SKU).

    La primera encola el alta; la segunda la encuentra en el índice en memoria y
    es la que dispara el flush anticipado.
    """
    fila = {"producto": "Difusor 125 ml", "sku": _SKU, "__context__": _CTX}
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "sku", "precio", "stock"],
                "row_count": 2,
            }
        ],
        "stock_detectado": [
            {**fila, "precio": "5000", "stock": "10"},
            {**fila, "precio": "5000", "stock": "7"},
        ],
    }


_MAPPINGS = {
    _CTX: {
        "producto": "name",
        "sku": "sku",
        "precio": "sale_price_ars",
        "stock": "stock_units",
    }
}


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
    # `autoflush=False` como producción.
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            for modelo in (
                InventoryMovement,
                InventoryBalance,
                UnclassifiedRecord,
                Product,
                OperationFingerprint,
                BusinessProfile,
                UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def test_el_alta_sustituida_no_deja_la_fila_siguiente_apuntando_al_descartado(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        upload_id = uuid.uuid4()
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="catalogo.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        # El ocupante del SKU: ya está en la base cuando el lote intenta insertar.
        ocupante = Product(
            tenant_id=tenant_id,
            name="Difusor 125 ml",
            sku=_SKU,
            sale_price_ars=1,
            stock_units=0,
            is_active=True,
        )
        session.add(ocupante)
        await session.commit()
        ocupante_id = ocupante.id

        # ...pero el índice de identidad que el import precarga NO lo ve: es el
        # estado exacto de un producto insertado por otra transacción DESPUÉS de la
        # precarga. Sin esto, la primera fila lo resolvería y nunca encolaría nada.
        async def _sin_indices(*_a: Any, **_k: Any) -> iis.ProductIdentityIndexes:
            return iis.ProductIdentityIndexes({}, {}, {}, {}, {})

        monkeypatch.setattr(iis, "_load_product_identity_indexes", _sin_indices)

        counts = await insert_confirmed_data(
            session,
            tenant_id,
            _summary(),
            {},
            context_mappings=_MAPPINGS,
            context_entity={_CTX: "product"},
            context_confirmed={_CTX: True},
            stock_treatment={_CTX: "opening_balance"},
            source="ingestion",
            uploaded_file_id=upload_id,
        )
        # Con el bug esto revienta acá: el movimiento de la segunda fila referencia
        # por FK al producto descartado, que no existe.
        await session.commit()

    assert counts["productos"] == 2, "las dos filas se procesaron"

    async with sm() as s:
        productos = (
            (
                await s.execute(select(Product).where(Product.tenant_id == tenant_id))
            )
            .scalars()
            .all()
        )
        movimientos = (
            (
                await s.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )

    # 1. No se duplicó el producto: la carrera se resolvió reusando al ocupante.
    assert [p.id for p in productos] == [ocupante_id]
    # 2. Y las DOS filas se aplicaron sobre él: la segunda es la que se perdía.
    #    El precio del ocupante era 1; el catálogo declara 5000.
    assert int(productos[0].sale_price_ars) == 5000
    # 3. Ningún movimiento quedó colgado de un producto inexistente (en Postgres
    #    esto ya lo garantiza la FK; se afirma igual para que el test diga QUÉ se
    #    está protegiendo y no dependa sólo de que el commit no explote).
    assert movimientos, "el catálogo declara stock: tiene que haber movimientos"
    assert {m.product_id for m in movimientos} == {ocupante_id}


async def test_dos_filas_de_la_misma_identidad_crean_un_solo_producto(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El mismo mecanismo SIN carrera: nadie ocupó la clave.

    La segunda fila encuentra el alta todavía encolada, fuerza su INSERT y mergea
    contra ella. Es el camino normal de un catálogo que repite un producto; lo que
    prueba es que repetirlo no crea dos productos ni deja un movimiento colgado.
    """
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
        upload_id = uuid.uuid4()
        session.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="catalogo.xlsx",
                s3_key=f"tests/{upload_id}.xlsx",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await session.commit()

        counts = await insert_confirmed_data(
            session,
            tenant_id,
            _summary(),
            {},
            context_mappings=_MAPPINGS,
            context_entity={_CTX: "product"},
            context_confirmed={_CTX: True},
            stock_treatment={_CTX: "opening_balance"},
            source="ingestion",
            uploaded_file_id=upload_id,
        )
        await session.commit()

    assert counts["productos"] == 2

    async with sm() as s:
        productos = (
            (await s.execute(select(Product).where(Product.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        movimientos = (
            (
                await s.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(productos) == 1, "el SKU repetido es UN producto, no dos"
    assert {m.product_id for m in movimientos} == {productos[0].id}
