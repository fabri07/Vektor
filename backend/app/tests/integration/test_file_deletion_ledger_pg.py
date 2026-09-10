"""E6b — el ledger de reversa: ¿todo efecto aplicado tiene su reversa trazable?

La pregunta no es "¿borrar limpia?" sino la más exigente que pide el plan: que el
resultado del borrado **distinga tres situaciones** y no mienta en ninguna.

1. **reversión total** — el archivo se borra y el tenant queda como antes de
   importarlo;
2. **parcial por edición posterior** — algo no vuelve porque alguien lo tocó
   después, y eso se INFORMA con su motivo;
3. **sin evidencia histórica** — el archivo se importó antes de que existiera el
   ledger, así que no se sabe qué creó, y eso también se informa.

La afirmación central de este módulo es la que ningún contador de "cuántos
borré" puede dar: **nada sobrevive en silencio**. Se toma un baseline del tenant
ANTES de que el archivo produzca un solo efecto y se compara contra el estado
posterior al borrado; lo que no volvió tiene que estar en ``conservados`` con su
motivo. Un borrado que revierte los gastos pero se olvida el stock pasaría
cualquier aserción sobre una sola tabla, y acá no.

Detalle que hace válido al escenario: ``ingestion_version`` se sella como lo hace
``finalize_import_lease``. El borrado decide por ese campo si el archivo trae
ledger, así que un test que no lo sellara mediría el camino legacy creyendo medir
el moderno — y los dos tienen que probarse, pero por separado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services.file_deletion_service import revert_file_data
from app.domain.file_deletion_reasons import PreservationReason
from app.persistence.models.product import Product
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F6F  # "VEKTOR" + E6b (ledger)

_FILAS = [
    esc.fila(5, esc.PRODUCTO, "5", "6000"),
    esc.fila(6, "Difusor bambu", "3", "3600"),
]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
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
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await esc.limpiar(factory, tenant_id)


def _negocio(snapshot: dict[str, Any]) -> dict[str, Any]:
    """El estado de NEGOCIO, sin la fila del archivo.

    El archivo en sí no vuelve —se borra— y su estado no dice nada sobre si los
    efectos se revirtieron. Compararlo haría fallar el test por la razón
    equivocada.
    """
    return {
        "gastos_vivos": esc.gastos_vivos(snapshot),
        # Sólo los ACTIVOS: el borrado desactiva, no elimina, y una fila
        # desactivada ya no participa de ninguna vista de negocio. Compararla
        # como residuo haría fallar el test por la razón equivocada — pasó, y por
        # eso el snapshot ahora trae el flag.
        "productos_activos": [p for p in snapshot["productos"] if p[4]],
        "proveedores_activos": [p for p in snapshot["proveedores"] if p[1]],
        "movimientos_vivos": [m for m in snapshot["movimientos"] if not m[3]],
    }


async def _borrar(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, file_id: uuid.UUID
) -> dict[str, Any]:
    async with factory() as s:
        resultado = await revert_file_data(s, file_id, tenant_id)
        await s.commit()
    return resultado


async def test_borrar_un_archivo_intacto_devuelve_el_tenant_a_como_estaba(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Reversión TOTAL: el caso que tiene que quedar sin residuos ni avisos."""
    file_id = await esc.preparar_tenant_y_archivo(sm, tenant_id, _FILAS)
    antes = _negocio(await esc.estado(sm, tenant_id))
    assert antes["gastos_vivos"] == [] and antes["productos_activos"] == []

    await esc.importar_archivo(sm, tenant_id, file_id, _FILAS)
    importado = _negocio(await esc.estado(sm, tenant_id))
    # Control: sin efectos que revertir, el test no probaría nada.
    assert importado != antes
    assert len(importado["gastos_vivos"]) == 2
    assert len(importado["productos_activos"]) == 2
    assert len(importado["movimientos_vivos"]) == 2

    resultado = await _borrar(sm, tenant_id, file_id)

    despues = _negocio(await esc.estado(sm, tenant_id))
    assert despues == antes, (
        f"quedaron efectos del archivo después de borrarlo.\n"
        f"antes:   {antes}\ndespués: {despues}\nconservados: {resultado['conservados']}"
    )
    assert not resultado["conservados"], (
        f"no quedó nada, pero el borrado informa conservados: {resultado['conservados']}"
    )


async def test_un_producto_preexistente_editado_despues_se_conserva_y_se_informa(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Reversión PARCIAL por edición posterior.

    El caso es un producto que YA EXISTÍA y cuyo costo pisó la compra importada.
    Al borrar el archivo hay que devolverle su costo anterior — salvo que alguien
    lo haya tocado después, y ahí no se pisa: se conserva y se INFORMA. Callarlo
    convertiría un borrado incompleto en uno que dice haber limpiado todo.

    Un producto CREADO por el archivo es otra cosa y tiene otro test: existe sólo
    por ese archivo, así que se desactiva aunque esté editado — decisión de
    producto declarada en ``revert_file_data``, que el usuario acepta en la
    advertencia previa del borrado.
    """
    file_id = await esc.preparar_tenant_y_archivo(sm, tenant_id, _FILAS)
    async with sm() as s:
        s.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=esc.PRODUCTO,
                sale_price_ars=Decimal("2100"),
                # Costo en 0 a propósito: una compra facturada NO pisa un costo de
                # referencia ya cargado (`debe_pisar_costo_de_referencia`, F-H6.d),
                # justamente para no perder un costo que incluía flete. Con 0 sí lo
                # recibe — si no, el stock quedaría valuado en cero. Es la única
                # forma de que este escenario tenga un costo que RESTAURAR.
                unit_cost_ars=Decimal("0"),
                stock_units=10,
            )
        )
        await s.commit()

    await esc.importar_archivo(sm, tenant_id, file_id, _FILAS)

    async with sm() as s:
        producto = (
            (
                await s.execute(
                    select(Product).where(
                        Product.tenant_id == tenant_id, Product.name == esc.PRODUCTO
                    )
                )
            )
            .scalars()
            .one()
        )
        # Control: la compra pisó el costo. Sin esto no habría nada que restaurar
        # y el test pasaría sin ejercer la reversa.
        assert producto.unit_cost_ars, "la compra no cargó el costo: el caso no aplica"
        # Se edita el MISMO campo que tocó el archivo. Editar otro (el precio de
        # venta, por ejemplo) no genera conflicto y no debe generar aviso: la
        # reversa sólo devuelve lo que el archivo cambió, así que una edición
        # sobre un campo ajeno sobrevive sola. Medido: ese caso restaura el costo
        # sin conservar nada, y está bien.
        producto.unit_cost_ars = Decimal("1500")
        producto.has_user_edits = True
        await s.commit()

    resultado = await _borrar(sm, tenant_id, file_id)

    conservados = resultado["conservados"]
    assert conservados, "el producto editado se conservó sin avisar"
    assert esc.PRODUCTO in {c["name"] for c in conservados}, f"conservó otra cosa: {conservados}"
    motivos = {m for c in conservados for m in c["reasons"]}
    assert motivos & {
        PreservationReason.EDICION_MANUAL_POSTERIOR.value,
        PreservationReason.CAMPO_MODIFICADO_POSTERIORMENTE.value,
    }, f"el motivo no explica que hubo una edición posterior: {motivos}"

    # El resto SÍ se revirtió: conservar uno no puede dejar los demás efectos.
    final = await esc.estado(sm, tenant_id)
    assert esc.gastos_vivos(final) == [], f"los gastos no se revirtieron: {final['gastos']}"


async def test_un_producto_creado_por_el_archivo_se_desactiva_aunque_este_editado(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La decisión declarada de producto, con su test para que no cambie por accidente.

    ``revert_file_data`` dice por escrito que revierte también lo editado a mano,
    y el usuario lo acepta en la advertencia previa del borrado. Para un producto
    que existe SÓLO por este archivo eso es coherente: no hay a qué estado
    anterior volver, y dejarlo activo sería dejar un producto huérfano en el
    catálogo. Se afirma acá para que si algún día se decide lo contrario, sea una
    decisión y no una regresión silenciosa.
    """
    file_id = await esc.preparar_tenant_y_archivo(sm, tenant_id, _FILAS)
    await esc.importar_archivo(sm, tenant_id, file_id, _FILAS)

    async with sm() as s:
        producto = (
            (
                await s.execute(
                    select(Product).where(
                        Product.tenant_id == tenant_id, Product.name == esc.PRODUCTO
                    )
                )
            )
            .scalars()
            .one()
        )
        producto.sale_price_ars = Decimal("9999")
        producto.has_user_edits = True
        await s.commit()

    await _borrar(sm, tenant_id, file_id)

    final = await esc.estado(sm, tenant_id)
    activos = [p for p in final["productos"] if p[4]]
    assert activos == [], f"quedó activo un producto que sólo existía por el archivo: {activos}"


async def test_un_archivo_sin_ledger_lo_dice_en_vez_de_prometer_limpieza(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """SIN evidencia histórica: importado antes de que existiera el ledger.

    Ahí no se sabe qué creó el archivo, y la respuesta honesta no es "borré todo"
    sino "borré lo que puedo rastrear y esto no lo puedo". Se simula con
    ``ingestion_version`` viejo, que es exactamente lo que distingue a un archivo
    histórico — el borrado decide por ese campo, no por la existencia de items.
    """
    file_id = await esc.preparar_tenant_y_archivo(sm, tenant_id, _FILAS, con_ledger=False)
    await esc.importar_archivo(sm, tenant_id, file_id, _FILAS)

    resultado = await _borrar(sm, tenant_id, file_id)

    motivos = {m for c in resultado["conservados"] for m in c["reasons"]}
    assert PreservationReason.SIN_LEDGER.value in motivos, (
        f"un archivo sin ledger no puede prometer reversión completa: "
        f"{resultado['conservados']}"
    )
    tipos = {c["entity_type"] for c in resultado["conservados"]}
    assert "file" in tipos, (
        f"el aviso de «sin ledger» es del ARCHIVO, no de una entidad concreta: {tipos}"
    )


async def test_los_gastos_se_revierten_igual_sin_ledger(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La contracara del caso anterior: «sin ledger» no es «no borro nada».

    Las ventas y los gastos llevan ``source_upload_id``, así que se rastrean sin
    ledger. Si la falta de ledger frenara también eso, borrar un archivo viejo
    dejaría su plata adentro y el usuario no tendría forma de sacarla.
    """
    file_id = await esc.preparar_tenant_y_archivo(sm, tenant_id, _FILAS, con_ledger=False)
    await esc.importar_archivo(sm, tenant_id, file_id, _FILAS)
    assert len(esc.gastos_vivos(await esc.estado(sm, tenant_id))) == 2

    await _borrar(sm, tenant_id, file_id)

    final = await esc.estado(sm, tenant_id)
    assert esc.gastos_vivos(final) == [], (
        f"un archivo sin ledger dejó sus gastos vivos: {final['gastos']}"
    )
