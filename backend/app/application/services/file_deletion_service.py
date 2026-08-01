"""Borrar un archivo saca de la interfaz los datos que ese archivo trajo.

Hasta acá ``DELETE /ingestion/files/{id}`` hacía UNA cosa: ``deleted_at = now()``.
El archivo desaparecía de la lista y sus ventas, gastos, productos y movimientos
seguían vivos en el dashboard. Dos consecuencias, las dos reportadas desde el
negocio:

1. El usuario borra un archivo mal importado y los números no cambian.
2. Vuelve a subir el mismo archivo corregido y **duplica**: las huellas
   anti-duplicado (``_import_row_anchor``) incluyen el ``uploaded_file_id``, así
   que un archivo nuevo no reconoce nada de lo que cargó el anterior.

Cómo se sabe qué trajo un archivo
---------------------------------
- ``SaleEntry`` / ``ExpenseEntry`` / ``InventoryMovement``: por ``source_upload_id``.
- ``UnclassifiedRecord`` ("Otros"): por ``uploaded_file_id``.
- ``Product``: NO tiene columna de origen. El vínculo es el **ledger** que el
  confirm escribe (``record_import_ledger``): un ``DataRepairRun`` de tipo
  ``INGESTION_IMPORT`` con un ``DataRepairItem`` por producto creado/actualizado.
  Sin ledger (archivos importados antes de esto) no se puede distinguir un
  producto que el archivo CREÓ de uno que ya existía, y se informa como ambiguo
  en vez de adivinar — borrar un producto preexistente sería peor que no borrarlo.

Por qué desactivar y no borrar productos
----------------------------------------
``is_active = False``, no ``DELETE``. Además de no romper FKs, es lo que
DESBLOQUEA la reimportación: los índices únicos de identidad de F5-B
(``uq_products_tenant_sku_norm`` / ``uq_products_tenant_barcode_norm``) son
PARCIALES sobre ``is_active``, así que un producto desactivado no colisiona con
el que el archivo nuevo va a crear.

El stock se revierte con ``void_movement`` (incremental, idempotente): NUNCA se
recomputa ``stock_units`` desde el ledger, porque no todo el stock viene de
movimientos (alta manual, chat, seed, catálogo con stock absoluto).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._ledger_restore import (
    entity_changed_since_ledger,
    restore_from_before,
)
from app.application.services.stock_service import void_movement
from app.domain.ingestion_version import INGESTION_VERSION_WITH_LEDGER
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

logger = get_logger(__name__)

# Tipo de run del ledger de import. Comparte tabla con las reparaciones y con la
# relectura (`REREAD`), que ya reusan `DataRepairRun`/`DataRepairItem`.
REPAIR_TYPE_IMPORT = "INGESTION_IMPORT"

# Runs que registran creación de productos ATADA A UN ARCHIVO. La relectura entra
# porque re-crea productos del mismo archivo con su propio repair_type; ignorarla
# dejaba vivos los productos de todo archivo releído.
_LEDGER_REPAIR_TYPES = (REPAIR_TYPE_IMPORT, "REREAD_FILE")

# Valor del set cerrado de `void_reason` (ver ck_sales_entries_void_reason). El
# usuario canceló el archivo entero; no es un duplicado ni una reparación.
VOID_REASON_FILE_DELETED = "USER_CANCELLED"

# Acciones del ledger — las mismas que ya usa la relectura, así que el CHECK de
# `ck_repair_items_action` no necesita migración.
ACTION_CREATE_PRODUCT = "CREATE_PRODUCT"
ACTION_UPDATE_PRODUCT = "UPDATE_PRODUCT"


async def record_import_ledger(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    product_details: list[dict[str, Any]],
) -> uuid.UUID | None:
    """Registra qué productos creó/actualizó un import, para poder revertirlo.

    Se llama DENTRO del savepoint del confirm: si el import se revierte, el
    ledger se va con él (no puede quedar un ledger de un import que no ocurrió).

    Devuelve el ``run_id``, o ``None`` si el import no tocó ningún producto (no
    tiene sentido un run vacío). ``product_details`` es lo que ya devuelve
    ``insert_confirmed_data(..., return_details=True)``: mismo formato que
    consume la relectura.
    """
    if not product_details:
        return None

    run = DataRepairRun(
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE_IMPORT,
        status="APPLIED",
        dry_run=False,
        details_json={"file_id": str(file_id)},
        products_created=sum(1 for d in product_details if d.get("action") == "CREATED"),
        products_updated=sum(1 for d in product_details if d.get("action") == "UPDATED"),
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()

    for detalle in product_details:
        raw_id = detalle.get("product_id")
        if not raw_id:
            continue
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                product_id=uuid.UUID(str(raw_id)),
                action=(
                    ACTION_CREATE_PRODUCT
                    if detalle.get("action") == "CREATED"
                    else ACTION_UPDATE_PRODUCT
                ),
                before_json=detalle.get("before"),
                after_json=detalle.get("after"),
                confidence="HIGH",
            )
        )
    await session.flush()
    return run.id


async def _product_ids_created_by_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> set[uuid.UUID]:
    """Productos que el ledger dice que ESTE archivo creó.

    Solo ``CREATE_PRODUCT``: un ``UPDATE_PRODUCT`` significa que el producto ya
    existía y el archivo apenas lo tocó — desactivarlo sería destruir un dato que
    el archivo no trajo.

    Incluye los runs de RELECTURA además de los de import: una relectura aplicada
    re-crea productos para el mismo archivo y los audita con su propio
    ``repair_type``. Filtrar solo por el de import dejaba vivos los productos de
    todo archivo releído — el mismo huérfano que este servicio existe para evitar.
    """
    res = await session.execute(
        select(DataRepairItem.product_id)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .where(
            DataRepairRun.repair_type.in_(_LEDGER_REPAIR_TYPES),
            DataRepairItem.tenant_id == tenant_id,
            DataRepairItem.source_file_id == file_id,
            DataRepairItem.action == ACTION_CREATE_PRODUCT,
            DataRepairItem.product_id.is_not(None),
        )
    )
    return {pid for pid in res.scalars().all() if pid is not None}


async def _product_items_updated_by_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[DataRepairItem]:
    """Items ``UPDATE_PRODUCT`` de este archivo, en orden cronológico.

    Son los productos que YA existían y el archivo modificó. El ledger guardó su
    ``before_json``, y hasta acá nadie lo leía: el borrado desactivaba lo creado y
    dejaba lo modificado pisado para siempre. Si un archivo cambió el precio de un
    producto del usuario, borrarlo tiene que devolver ese precio.

    Orden por ``created_at`` porque un mismo producto puede tener VARIOS items del
    mismo archivo (dos filas de la planilla que lo tocan). Para restaurar hay que
    usar el ``before`` del PRIMERO —el estado anterior a que este archivo lo
    tocara por primera vez—, no el del último, que ya refleja un cambio del propio
    archivo.
    """
    res = await session.execute(
        select(DataRepairItem)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .where(
            DataRepairRun.repair_type.in_(_LEDGER_REPAIR_TYPES),
            DataRepairItem.tenant_id == tenant_id,
            DataRepairItem.source_file_id == file_id,
            DataRepairItem.action == ACTION_UPDATE_PRODUCT,
            DataRepairItem.product_id.is_not(None),
        )
        .order_by(DataRepairItem.created_at)
    )
    return list(res.scalars().all())


async def _products_with_live_external_sales(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: set[uuid.UUID],
    *,
    excluir_archivo: uuid.UUID | None = None,
) -> set[uuid.UUID]:
    """Productos con ventas vivas de OTRA fuente: quedan protegidos del borrado.

    Una venta viva sobre un producto lo vuelve un dato del usuario, no del
    archivo.

    ``excluir_archivo`` existe por el orden de los dos llamadores: la reversa lo
    invoca DESPUÉS de anular las ventas del archivo (lo que quede vivo ya es
    externo, no hace falta excluir nada), pero el preview corre ANTES — sin
    excluirlas, las propias ventas del archivo protegerían a sus productos y el
    preview informaría 0 cuando la reversa va a desactivar varios.
    """
    if not product_ids:
        return set()
    stmt = select(SaleEntry.product_id).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.product_id.in_(product_ids),
        SaleEntry.voided_at.is_(None),
    )
    if excluir_archivo is not None:
        stmt = stmt.where(
            (SaleEntry.source_upload_id.is_(None))
            | (SaleEntry.source_upload_id != excluir_archivo)
        )
    res = await session.execute(stmt)
    return {pid for pid in res.scalars().all() if pid is not None}


async def _has_import_ledger(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    """¿Este archivo se importó con ledger? (los viejos no lo tienen).

    Se responde con ``UploadedFile.ingestion_version``, que es justo el mecanismo
    del framework de versionado: ``finalize_import_lease`` lo sella al confirmar.
    NO se pregunta por la existencia de items del ledger — un import legítimo que
    no tocó ningún producto no escribe items, y eso es indistinguible de "no
    sabemos qué creó" si se mira por ahí.

    Tampoco se filtra por ``details_json``: ``->>`` es específico de Postgres y
    reventaría en SQLite, donde corren los tests (verde en SQLite no prueba
    Postgres, pero acá el riesgo es al revés y se ve enseguida).
    """
    res = await session.execute(
        select(UploadedFile.ingestion_version).where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant_id,
        )
    )
    version = res.scalar_one_or_none()
    return version is not None and version >= INGESTION_VERSION_WITH_LEDGER


async def preview_file_deletion(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> dict[str, Any]:
    """Qué se va a borrar si se elimina este archivo. Read-only.

    Alimenta la advertencia que el usuario tiene que aceptar. Incluye
    ``has_user_edits`` porque el borrado revierte TAMBIÉN lo editado a mano
    (decisión del producto): el usuario tiene que poder ver eso antes de aceptar.
    """
    from app.application.services.reread_service import file_has_user_edits

    async def _contar(modelo: Any, columna: Any) -> int:
        # `func.count()`, no `len(scalars().all())`: esto es un preview read-only
        # y traerse miles de UUIDs a Python para contarlos es gratis de evitar.
        res = await session.execute(
            select(func.count())
            .select_from(modelo)
            .where(
                modelo.tenant_id == tenant_id,
                columna == file_id,
                modelo.voided_at.is_(None),
            )
        )
        return int(res.scalar_one())

    ventas = await _contar(SaleEntry, SaleEntry.source_upload_id)
    gastos = await _contar(ExpenseEntry, ExpenseEntry.source_upload_id)
    movimientos = await _contar(InventoryMovement, InventoryMovement.source_upload_id)

    otros = (
        await session.execute(
            select(func.count())
            .select_from(UnclassifiedRecord)
            .where(
                UnclassifiedRecord.tenant_id == tenant_id,
                UnclassifiedRecord.uploaded_file_id == file_id,
                UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
            )
        )
    ).scalar_one()
    # Filas de "Otros" que el usuario YA resolvió: no se borran (ver el paso 4 de
    # `revert_file_data`). Se informan para que la advertencia no las prometa.
    otros_ya_clasificados = (
        await session.execute(
            select(func.count())
            .select_from(UnclassifiedRecord)
            .where(
                UnclassifiedRecord.tenant_id == tenant_id,
                UnclassifiedRecord.uploaded_file_id == file_id,
                UnclassifiedRecord.status != UNCLASSIFIED_STATUS_PENDING,
            )
        )
    ).scalar_one()

    # Mismos protegidos que aplica la reversa: sin esto el preview prometía
    # desactivar productos que después sobreviven.
    creados = await _product_ids_created_by_file(session, file_id, tenant_id)
    productos_activos = 0
    if creados:
        protegidos = await _products_with_live_external_sales(
            session, tenant_id, creados, excluir_archivo=file_id
        )
        res = await session.execute(
            select(func.count())
            .select_from(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.id.in_(creados - protegidos),
                Product.is_active.is_(True),
            )
        )
        productos_activos = res.scalar_one()

    con_ledger = await _has_import_ledger(session, file_id, tenant_id)

    return {
        "ventas": ventas,
        "gastos": gastos,
        "productos": productos_activos,
        "movimientos_stock": movimientos,
        "otros": otros,
        "otros_ya_clasificados": otros_ya_clasificados,
        "has_user_edits": await file_has_user_edits(session, file_id, tenant_id),
        # Sin ledger no se puede saber qué productos creó este archivo: se avisa
        # en vez de adivinar (los productos quedan y hay que revisarlos a mano).
        "productos_no_rastreables": not con_ledger,
    }


async def revert_file_data(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Revierte todo lo que este archivo importó. NO commitea (lo hace el caller).

    Revierte también lo editado a mano: es la decisión de producto, y el usuario
    la acepta explícitamente en la advertencia previa.
    """
    ahora = datetime.now(UTC)
    contadores = {
        "ventas": 0,
        "gastos": 0,
        "productos": 0,
        "movimientos_stock": 0,
        "otros": 0,
        # Productos que el archivo MODIFICÓ: se les devolvió su valor anterior, o
        # se conservaron porque alguien los editó después del import.
        "productos_restaurados": 0,
        "productos_conservados": 0,
    }

    # 1. Ventas y gastos → soft delete auditado (el dashboard ya filtra por
    #    `voided_at IS NULL`, así que desaparecen de la interfaz).
    ventas_res = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.source_upload_id == file_id,
            SaleEntry.voided_at.is_(None),
        )
    )
    for venta in ventas_res.scalars().all():
        venta.voided_at = ahora
        venta.void_reason = VOID_REASON_FILE_DELETED
        contadores["ventas"] += 1

    gastos_res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    for gasto in gastos_res.scalars().all():
        gasto.voided_at = ahora
        gasto.void_reason = VOID_REASON_FILE_DELETED
        contadores["gastos"] += 1

    # 2. Movimientos de inventario → `void_movement` revierte el efecto de cada
    #    uno sobre stock_units/inventory_balances. Incremental e idempotente:
    #    recomputar desde el ledger destruiría el stock de origen no-ledger.
    movs_res = await session.execute(
        select(InventoryMovement).where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.source_upload_id == file_id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    for mov in movs_res.scalars().all():
        await void_movement(mov, session)
        contadores["movimientos_stock"] += 1

    # 3. Productos CREADOS por el archivo → desactivar. Solo los del ledger, y
    #    solo si no quedaron ventas vivas de otra fuente apuntándolos (una venta
    #    manual posterior sobre ese producto lo vuelve un dato del usuario, no
    #    del archivo).
    creados = await _product_ids_created_by_file(session, file_id, tenant_id)
    if creados:
        protegidos = await _products_with_live_external_sales(session, tenant_id, creados)
        productos_res = await session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.id.in_(creados - protegidos),
                Product.is_active.is_(True),
            )
        )
        for producto in productos_res.scalars().all():
            producto.is_active = False
            contadores["productos"] += 1

    # 3-bis. Productos que el archivo MODIFICÓ (no creó) → restaurar su `before`.
    #    El ledger lo venía guardando y nadie lo leía: un archivo que pisaba el
    #    precio de un producto del usuario lo dejaba pisado para siempre.
    #
    #    Se restaura el `before` del PRIMER item de cada producto (el estado
    #    anterior a que este archivo lo tocara), no el del último — el último ya
    #    refleja un cambio del propio archivo.
    #
    #    Guard: si alguien editó el producto DESPUÉS del import, no se pisa esa
    #    edición. Se informa y se sigue (la Fase 6 lo afinará a nivel de campo).
    _items_update = await _product_items_updated_by_file(session, file_id, tenant_id)
    _primer_item: dict[uuid.UUID, DataRepairItem] = {}
    _ultimo_item: dict[uuid.UUID, DataRepairItem] = {}
    for _it in _items_update:
        if _it.product_id is None:
            continue
        _primer_item.setdefault(_it.product_id, _it)
        _ultimo_item[_it.product_id] = _it
    for _pid, _item in _primer_item.items():
        _prod = await session.get(Product, _pid)
        if _prod is None or _prod.tenant_id != tenant_id:
            continue
        # `updated_at` tiene `onupdate` server-side y puede estar expirado tras
        # los flushes de los pasos anteriores de esta misma transacción.
        await session.refresh(_prod)
        # Se compara contra el `after` del ÚLTIMO item: es el estado en que este
        # archivo dejó el producto.
        if entity_changed_since_ledger(_prod, _ultimo_item[_pid].after_json):
            contadores["productos_conservados"] += 1
            continue
        if restore_from_before(_prod, "product", _item.before_json or {}):
            contadores["productos_restaurados"] += 1

    # 4. Filas que quedaron en "Otros" esperando clasificación manual: se borran
    #    (nunca fueron dato de negocio, son staging del archivo).
    #
    #    SOLO las PENDING. Una fila que el usuario ya resolvió (IMPORTED) generó
    #    una venta/gasto/producto real, y ese registro NO lleva
    #    `source_upload_id` (lo crea `others.py`, no el importador), así que la
    #    reversa de arriba no lo alcanza. Borrar la fila de staging destruiría el
    #    único rastro que queda hacia el archivo, dejando el dato derivado vivo y
    #    huérfano. Se conservan y se informan aparte.
    otros_res = await session.execute(
        select(UnclassifiedRecord.id).where(
            UnclassifiedRecord.tenant_id == tenant_id,
            UnclassifiedRecord.uploaded_file_id == file_id,
            UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
        )
    )
    otros_ids = list(otros_res.scalars().all())
    if otros_ids:
        await session.execute(
            delete(UnclassifiedRecord).where(UnclassifiedRecord.id.in_(otros_ids))
        )
        contadores["otros"] = len(otros_ids)

    # 5. Huellas de fila de este archivo. Sin esto, volver a subir el MISMO
    #    archivo (que genera otro file_id, y por lo tanto otras anclas) funciona,
    #    pero quedan huellas colgadas de datos que ya no existen.
    await _delete_import_fingerprints(session, tenant_id, file_id)

    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="INGESTION_FILE_DELETED_WITH_DATA",
            decision_data={"file_id": str(file_id), **contadores},
            triggered_by="ingestion:delete_file",
            actor_user_id=actor_user_id,
            context={"source": "file_deletion_service.revert_file_data"},
            created_at=ahora,
        )
    )
    logger.info(
        "ingestion.delete.reverted",
        file_id=str(file_id),
        **contadores,
    )
    return contadores


async def _delete_import_fingerprints(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID
) -> None:
    """Borra las huellas de fila que dejó este archivo.

    Las anclas se hashean (``sha256``) antes de guardarse, así que no se pueden
    filtrar por ``LIKE`` sobre el ``file_id``. Se re-derivan desde el
    ``source_row_ref`` de los registros del archivo, que ES el mismo hash (ver
    el docstring de ``reread_service``: ``source_row_ref == fingerprint``).
    """
    refs: set[str] = set()
    ventas_refs = await session.execute(
        select(SaleEntry.source_row_ref).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.source_upload_id == file_id,
            SaleEntry.source_row_ref.is_not(None),
        )
    )
    refs.update(r for r in ventas_refs.scalars().all() if r)
    gastos_refs = await session.execute(
        select(ExpenseEntry.source_row_ref).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.source_upload_id == file_id,
            ExpenseEntry.source_row_ref.is_not(None),
        )
    )
    refs.update(r for r in gastos_refs.scalars().all() if r)

    if not refs:
        return
    await session.execute(
        delete(OperationFingerprint).where(
            OperationFingerprint.tenant_id == tenant_id,
            OperationFingerprint.fingerprint.in_(refs),
        )
    )
