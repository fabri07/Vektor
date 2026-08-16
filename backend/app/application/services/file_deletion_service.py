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
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._ledger_restore import (
    entity_changed_since_ledger,
    fields_changed_since_ledger,
    restore_from_before,
    snapshot_master,
)
from app.application.services.stock_service import void_movement
from app.domain.file_deletion_reasons import PreservationReason
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
    UNCLASSIFIED_ROW_REF_PREFIX,
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

# Maestros creados/modificados por un import. Nombres paralelos a los de producto
# a propósito: `REREAD_MASTER_CREATE`/`UPDATE` son de la RELECTURA y siguen
# existiendo (tienen datos vivos), pero mezclar las dos convenciones en el mismo
# CHECK haría que revisar el ledger sea adivinar cuál mira cada consulta.
ACTION_CREATE_CUSTOMER = "CREATE_CUSTOMER"
ACTION_UPDATE_CUSTOMER = "UPDATE_CUSTOMER"
ACTION_CREATE_SUPPLIER = "CREATE_SUPPLIER"
ACTION_UPDATE_SUPPLIER = "UPDATE_SUPPLIER"

_MASTER_ACTIONS: dict[tuple[str, bool], str] = {
    ("customer", True): ACTION_CREATE_CUSTOMER,
    ("customer", False): ACTION_UPDATE_CUSTOMER,
    ("supplier", True): ACTION_CREATE_SUPPLIER,
    ("supplier", False): ACTION_UPDATE_SUPPLIER,
}
_CREATE_MASTER_ACTIONS = (ACTION_CREATE_CUSTOMER, ACTION_CREATE_SUPPLIER)
_UPDATE_MASTER_ACTIONS = (ACTION_UPDATE_CUSTOMER, ACTION_UPDATE_SUPPLIER)

# F-D (7g, mig 20260816_0001): un campo cross-sección puntual (ej. `sale→
# customer:last_name`) — NUNCA mezclado con ACTION_UPDATE_CUSTOMER/SUPPLIER,
# que asumen snapshot de la entidad ENTERA. Ver el docstring de la migración.
ACTION_UPDATE_CUSTOMER_CROSS_FIELD = "UPDATE_CUSTOMER_CROSS_FIELD"
ACTION_UPDATE_SUPPLIER_CROSS_FIELD = "UPDATE_SUPPLIER_CROSS_FIELD"
_CROSS_FIELD_ACTIONS = (ACTION_UPDATE_CUSTOMER_CROSS_FIELD, ACTION_UPDATE_SUPPLIER_CROSS_FIELD)


async def snapshot_masters_before_import(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[uuid.UUID, dict[str, Any]], dict[uuid.UUID, dict[str, Any]]]:
    """Estado de los maestros ANTES de importar. Solo llamar si el archivo trae.

    No se sabe a cuáles va a tocar el motor de identidad hasta que corre, así que
    hay que fotografiar todo. Es el mismo enfoque que ya usa la relectura.

    El caller decide cuándo pagarlo: un import que no trae clientes ni proveedores
    NO debe llamar a esto — serían dos SELECT completos por confirm, para nada.
    """
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    clientes = (
        await session.execute(select(Customer).where(Customer.tenant_id == tenant_id))
    ).scalars().all()
    proveedores = (
        await session.execute(select(Supplier).where(Supplier.tenant_id == tenant_id))
    ).scalars().all()
    return (
        {c.id: snapshot_master(c, "customer") for c in clientes if not c.is_sentinel},
        {s.id: snapshot_master(s, "supplier") for s in proveedores if not s.is_sentinel},
    )


async def build_master_details(
    session: AsyncSession,
    counts: dict[str, Any],
    before_customers: dict[uuid.UUID, dict[str, Any]],
    before_suppliers: dict[uuid.UUID, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Arma los items de maestros para el ledger, desde los ids del import.

    ``insert_confirmed_data`` ya devuelve ``clientes_creados_ids`` y compañía; acá
    solo se les adjunta el ``before`` (None si la entidad no existía) y el
    ``after`` de cómo quedó.

    Dedup igual que en la relectura: si dos filas del MISMO archivo matchean la
    misma identidad, el id cae en creados Y en actualizados. El "antes" real de
    esa entidad relativo a TODO este import es "no existía", así que se audita una
    sola vez como CREATE — un UPDATE con ``before=None`` estaría mal etiquetado.

    **Los centinelas ("Local" / "No identificado") nunca entran**: son
    infraestructura del tenant, no algo que el archivo haya traído, y desactivar
    el centinela dejaría ventas sin cliente al que apuntar.
    """
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    detalles: list[dict[str, Any]] = []

    async def _agregar(
        ids: list[str],
        kind: str,
        model: type[Any],
        before_map: dict[uuid.UUID, Any],
        *,
        creado: bool,
    ) -> None:
        if not ids:
            return
        entity_ids = [uuid.UUID(str(i)) for i in ids]
        # UNA query, no un `session.get` + `refresh` por entidad: esto corre en el
        # camino caliente del confirm, y un archivo con 500 clientes serían ~1000
        # round-trips colgados de la request. El SELECT fresco además ya trae el
        # `updated_at` que el flush del import acaba de escribir (es server-side,
        # y leerlo de un objeto expirado dispararía un lazy-load síncrono que
        # revienta bajo AsyncSession con MissingGreenlet).
        res = await session.execute(select(model).where(model.id.in_(entity_ids)))
        for entity in res.scalars().all():
            if getattr(entity, "is_sentinel", False):
                continue
            detalles.append(
                {
                    "action": _MASTER_ACTIONS[(kind, creado)],
                    "kind": kind,
                    "id": str(entity.id),
                    "name": getattr(entity, "name", "") or "",
                    "before": None if creado else before_map.get(entity.id),
                    "after": snapshot_master(entity, kind),
                }
            )

    _creados_c = {str(i) for i in counts.get("clientes_creados_ids", [])}
    _creados_s = {str(i) for i in counts.get("proveedores_creados_ids", [])}
    _actualizados_c = [
        i for i in map(str, counts.get("clientes_actualizados_ids", [])) if i not in _creados_c
    ]
    _actualizados_s = [
        i for i in map(str, counts.get("proveedores_actualizados_ids", [])) if i not in _creados_s
    ]

    await _agregar(sorted(_creados_c), "customer", Customer, before_customers, creado=True)
    await _agregar(_actualizados_c, "customer", Customer, before_customers, creado=False)
    await _agregar(sorted(_creados_s), "supplier", Supplier, before_suppliers, creado=True)
    await _agregar(_actualizados_s, "supplier", Supplier, before_suppliers, creado=False)
    return detalles


async def record_import_ledger(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    product_details: list[dict[str, Any]],
    master_details: list[dict[str, Any]] | None = None,
    cross_field_details: list[dict[str, Any]] | None = None,
) -> uuid.UUID | None:
    """Registra qué creó/actualizó un import, para poder revertirlo.

    Se llama DENTRO del savepoint del confirm: si el import se revierte, el
    ledger se va con él (no puede quedar un ledger de un import que no ocurrió).

    Devuelve el ``run_id``, o ``None`` si el import no tocó nada (no tiene sentido
    un run vacío). ``product_details`` es lo que ya devuelve
    ``insert_confirmed_data(..., return_details=True)``; ``master_details`` lo
    arma ``build_master_details`` desde los ids de clientes/proveedores que ese
    mismo import devuelve. ``cross_field_details`` (F-D) sale directo de
    ``counts["cross_field_details"]`` — ya viene con ``action``/``before``/
    ``after`` armados por ``_apply_cross_field_buffer``, no necesita un
    ``build_*`` propio porque no hay snapshot previo que anticipar (el "antes"
    es el valor del campo al momento de escribirlo, ya calculado ahí).
    """
    master_details = master_details or []
    cross_field_details = cross_field_details or []
    if not product_details and not master_details and not cross_field_details:
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

    # Maestros: mismo run, misma transacción. `product_id` queda NULL — la
    # identidad de la entidad viaja en el `after_json` (`id` + `kind`), que es de
    # donde la lee la reversa.
    for detalle in master_details:
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                action=detalle["action"],
                before_json=detalle.get("before"),
                after_json=detalle.get("after"),
                confidence="HIGH",
            )
        )

    # F-D: campos cross-sección — mismo run, misma transacción, mismo convenio
    # `after_json["id"]`/`["kind"]` que los maestros (ver `_revert_cross_field_items`).
    for detalle in cross_field_details:
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                source_file_id=file_id,
                action=detalle["action"],
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


async def _master_items_by_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[DataRepairItem]:
    """Items de clientes/proveedores que este archivo creó o modificó."""
    res = await session.execute(
        select(DataRepairItem)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .where(
            DataRepairRun.repair_type.in_(_LEDGER_REPAIR_TYPES),
            DataRepairItem.tenant_id == tenant_id,
            DataRepairItem.source_file_id == file_id,
            DataRepairItem.action.in_(_CREATE_MASTER_ACTIONS + _UPDATE_MASTER_ACTIONS),
        )
        .order_by(DataRepairItem.created_at)
    )
    return list(res.scalars().all())


async def _master_tiene_dependencias(
    session: AsyncSession, entity_id: uuid.UUID, kind: str, tenant_id: uuid.UUID
) -> PreservationReason | None:
    """¿Este maestro tiene operaciones vivas que lo referencian?

    Desactivar un cliente con ventas dejaría esas ventas apuntando a una ficha
    inactiva. La entidad se conserva y se informa el motivo.
    """
    if kind == "customer":
        res = await session.execute(
            select(func.count())
            .select_from(SaleEntry)
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.customer_id == entity_id,
                SaleEntry.voided_at.is_(None),
            )
        )
        if res.scalar_one():
            return PreservationReason.VENTA_MANUAL_POSTERIOR
        return None

    res = await session.execute(
        select(func.count())
        .select_from(ExpenseEntry)
        .where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.supplier_id == entity_id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    if res.scalar_one():
        return PreservationReason.COMPRA_POSTERIOR
    res = await session.execute(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.supplier_id == entity_id,
            InventoryMovement.voided_at.is_(None),
        )
    )
    if res.scalar_one():
        return PreservationReason.MOVIMIENTO_POSTERIOR
    return None


async def _revert_master_items(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    dry_run: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Revierte los maestros del archivo. (desactivados, restaurados, conservados).

    Creados por el archivo → se desactivan, salvo que tengan operaciones vivas.
    Modificados por el archivo → vuelven a su ``before``, salvo edición posterior.

    ``dry_run=True`` recorre exactamente el mismo camino sin escribir: es lo que
    usa el preview. Una función separada para "anticipar" habría divergido de la
    que decide — el preview terminaría prometiendo algo distinto de lo que pasa.

    Los centinelas nunca llegan acá (``build_master_details`` no los registra),
    pero se filtran igual: una fila vieja del ledger no puede desactivar el
    cliente "Local" y dejar sus ventas sin a quién apuntar.
    """
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    _modelo: dict[str, type[Any]] = {"customer": Customer, "supplier": Supplier}
    items = await _master_items_by_file(session, file_id, tenant_id)

    # Un mismo maestro puede tener varios items del archivo: para restaurar vale
    # el `before` del PRIMERO; para el guard, el `after` del ÚLTIMO.
    primero: dict[tuple[str, uuid.UUID], DataRepairItem] = {}
    ultimo: dict[tuple[str, uuid.UUID], DataRepairItem] = {}
    fue_creado: set[tuple[str, uuid.UUID]] = set()
    for it in items:
        after = it.after_json or {}
        kind = str(after.get("kind") or "")
        raw_id = after.get("id")
        if kind not in _modelo or not raw_id:
            continue
        clave = (kind, uuid.UUID(str(raw_id)))
        primero.setdefault(clave, it)
        ultimo[clave] = it
        if it.action in _CREATE_MASTER_ACTIONS:
            fue_creado.add(clave)

    desactivados = restaurados = 0
    conservados: list[dict[str, Any]] = []
    for (kind, entity_id), item in primero.items():
        entity = await session.get(_modelo[kind], entity_id)
        if entity is None or entity.tenant_id != tenant_id:
            continue
        if getattr(entity, "is_sentinel", False):
            continue
        await session.refresh(entity)
        nombre = getattr(entity, "name", "") or ""

        motivo = await _master_tiene_dependencias(session, entity_id, kind, tenant_id)
        if motivo is not None:
            conservados.append(
                {
                    "entity_type": kind,
                    "id": str(entity_id),
                    "name": nombre,
                    "reasons": [motivo.value],
                    "fields": [],
                }
            )
            continue

        if (kind, entity_id) in fue_creado:
            # Lo creó el archivo y nadie lo usa: se desactiva (nunca hard delete —
            # rompería el ON DELETE SET NULL de lo que llegue a referenciarlo).
            if not dry_run:
                entity.deactivated_at = datetime.now(UTC)
            desactivados += 1
            continue

        if entity_changed_since_ledger(entity, ultimo[(kind, entity_id)].after_json):
            conservados.append(
                {
                    "entity_type": kind,
                    "id": str(entity_id),
                    "name": nombre,
                    "reasons": [PreservationReason.EDICION_MANUAL_POSTERIOR.value],
                    "fields": [],
                }
            )
            continue
        if dry_run:
            # Se cuenta lo que se restauraría, sin tocar la entidad.
            if item.before_json:
                restaurados += 1
        elif restore_from_before(entity, kind, item.before_json or {}):
            restaurados += 1

    return desactivados, restaurados, conservados


async def _cross_field_items_by_file(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[DataRepairItem]:
    """Items de campos cross-sección (F-D) que este archivo escribió."""
    res = await session.execute(
        select(DataRepairItem)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .where(
            DataRepairRun.repair_type.in_(_LEDGER_REPAIR_TYPES),
            DataRepairItem.tenant_id == tenant_id,
            DataRepairItem.source_file_id == file_id,
            DataRepairItem.action.in_(_CROSS_FIELD_ACTIONS),
        )
        .order_by(DataRepairItem.created_at)
    )
    return list(res.scalars().all())


async def _revert_cross_field_items(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    dry_run: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    """Revierte los campos cross-sección que este archivo escribió (F-D, 7g).

    Función HERMANA de ``_revert_master_items``, no la misma función: ahí un
    item representa la entidad ENTERA (creada → se desactiva; modificada → se
    restaura completa). Acá cada item es UN campo puntual de una entidad que
    YA existía antes de este archivo (F-D nunca crea cliente/proveedor), así
    que la reversa es POR CAMPO — mezclar las dos habría hecho que un solo
    campo cross activara el guard/la desactivación de la entidad entera.

    Grano fino: si alguien tocó ESE campo después del import, ese campo se
    conserva (``CAMPO_MODIFICADO_POSTERIORMENTE``, con ``fields`` poblado —
    el único motivo pensado para viajar por campo, ver
    ``domain/file_deletion_reasons.py``) pero el resto de los campos que este
    archivo escribió sí vuelve a su valor anterior.
    """
    from app.persistence.models.customer import Customer  # noqa: PLC0415
    from app.persistence.models.supplier import Supplier  # noqa: PLC0415

    _modelo: dict[str, type[Any]] = {"customer": Customer, "supplier": Supplier}
    items = await _cross_field_items_by_file(session, file_id, tenant_id)

    # Un mismo (kind, entidad) puede tener más de un item si el archivo se
    # reprocesó (relectura). Vale el `before` del PRIMERO; el `after` del
    # ÚLTIMO es contra el que se compara "¿cambió después?" — mismo criterio
    # que `_revert_master_items`.
    primero: dict[tuple[str, uuid.UUID], DataRepairItem] = {}
    ultimo: dict[tuple[str, uuid.UUID], DataRepairItem] = {}
    for it in items:
        after = it.after_json or {}
        kind = str(after.get("kind") or "")
        raw_id = after.get("id")
        if kind not in _modelo or not raw_id:
            continue
        clave = (kind, uuid.UUID(str(raw_id)))
        primero.setdefault(clave, it)
        ultimo[clave] = it

    restaurados = 0
    conservados: list[dict[str, Any]] = []
    for (kind, entity_id), item in primero.items():
        entity = await session.get(_modelo[kind], entity_id)
        if entity is None or entity.tenant_id != tenant_id:
            continue
        if getattr(entity, "is_sentinel", False):
            # F-D nunca captura sobre el sentinela (sólo entidades "matched"),
            # pero el guard se mantiene por consistencia con el resto del
            # archivo — un ledger viejo/corrupto no debería poder tocarlo.
            continue
        await session.refresh(entity)

        _cambiados = fields_changed_since_ledger(entity, ultimo[(kind, entity_id)].after_json)
        _restaurable = dict(item.before_json or {})
        for campo in _cambiados:
            _restaurable.pop(campo, None)

        if _restaurable and (
            (dry_run) or restore_from_before(entity, kind, _restaurable)
        ):
            restaurados += 1

        if _cambiados:
            conservados.append(
                {
                    "entity_type": kind,
                    "id": str(entity_id),
                    "name": getattr(entity, "name", "") or "",
                    "reasons": [PreservationReason.CAMPO_MODIFICADO_POSTERIORMENTE.value],
                    "fields": _cambiados,
                }
            )

    return restaurados, conservados


async def _productos_con_otra_fuente(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: set[uuid.UUID],
    *,
    file_id: uuid.UUID,
    excluir_filas_del_archivo: bool = False,
) -> dict[uuid.UUID, list[PreservationReason]]:
    """Productos con evidencia DEMOSTRABLE de otra fuente viva, con su motivo.

    "Demostrable" es literal: ledger, ``source_upload_id`` o una referencia real
    en la base. NUNCA coincidencia de nombre — dos productos que se llaman igual
    no son el mismo producto, y usar el nombre como evidencia haría que borrar un
    archivo conserve cosas que sí debía revertir (o al revés).

    Antes esto solo miraba ventas vivas; un producto con compras posteriores, con
    movimientos de stock de otro origen, o traído también por otro archivo activo,
    se desactivaba igual.

    ``excluir_filas_del_archivo`` es para el PREVIEW, que corre antes de anular las
    filas del archivo: sin excluirlas, las propias ventas del archivo protegerían
    a sus productos y el preview informaría 0 conservados donde la reversa
    desactiva varios. La reversa no lo necesita (a esa altura lo suyo ya está
    anulado y los filtros ``voided_at IS NULL`` lo dejan afuera solo).

    ``file_id`` es SIEMPRE obligatorio y siempre se excluye del criterio "otro
    archivo activo": el archivo que se está borrando no puede ser evidencia de sí
    mismo. Sin eso se protegía a sus propios productos y no desactivaba ninguno —
    el endpoint lo tapaba porque marca ``deleted_at`` antes de llamar, pero
    depender de ese orden es exactamente el tipo de acoplamiento que rompe cuando
    alguien invoca la reversa desde otro lado.
    """
    if not product_ids:
        return {}
    motivos: dict[uuid.UUID, list[PreservationReason]] = defaultdict(list)

    async def _marcar(stmt: Any, motivo: PreservationReason) -> None:
        res = await session.execute(stmt)
        for pid in {p for p in res.scalars().all() if p is not None}:
            if motivo not in motivos[pid]:
                motivos[pid].append(motivo)

    # 1. Ventas vivas de otro origen.
    _ventas = select(SaleEntry.product_id).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.product_id.in_(product_ids),
        SaleEntry.voided_at.is_(None),
    )
    if excluir_filas_del_archivo:
        _ventas = _ventas.where(
            (SaleEntry.source_upload_id.is_(None))
            | (SaleEntry.source_upload_id != file_id)
        )
    await _marcar(_ventas, PreservationReason.VENTA_MANUAL_POSTERIOR)

    # 2. Compras/gastos vivos que lo referencian.
    _gastos = select(ExpenseEntry.product_id).where(
        ExpenseEntry.tenant_id == tenant_id,
        ExpenseEntry.product_id.in_(product_ids),
        ExpenseEntry.voided_at.is_(None),
    )
    if excluir_filas_del_archivo:
        _gastos = _gastos.where(
            (ExpenseEntry.source_upload_id.is_(None))
            | (ExpenseEntry.source_upload_id != file_id)
        )
    await _marcar(_gastos, PreservationReason.COMPRA_POSTERIOR)

    # 3. Movimientos de stock de OTRO archivo o sin archivo (alta manual/chat).
    _movs = select(InventoryMovement.product_id).where(
        InventoryMovement.tenant_id == tenant_id,
        InventoryMovement.product_id.in_(product_ids),
        InventoryMovement.voided_at.is_(None),
    )
    if excluir_filas_del_archivo:
        _movs = _movs.where(
            (InventoryMovement.source_upload_id.is_(None))
            | (InventoryMovement.source_upload_id != file_id)
        )
    await _marcar(_movs, PreservationReason.MOVIMIENTO_POSTERIOR)

    # 4. Otro archivo VIVO que el ledger dice que también lo creó/tocó.
    _otro_archivo = (
        select(DataRepairItem.product_id)
        .join(DataRepairRun, DataRepairRun.id == DataRepairItem.run_id)
        .join(UploadedFile, UploadedFile.id == DataRepairItem.source_file_id)
        .where(
            DataRepairRun.repair_type.in_(_LEDGER_REPAIR_TYPES),
            DataRepairItem.tenant_id == tenant_id,
            DataRepairItem.product_id.in_(product_ids),
            UploadedFile.deleted_at.is_(None),
            # SIEMPRE: "otro archivo" excluye por definición al que se borra.
            DataRepairItem.source_file_id != file_id,
        )
    )
    await _marcar(_otro_archivo, PreservationReason.OTRO_ARCHIVO_ACTIVO)

    return dict(motivos)


async def _otros_clasificados_revertidos(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> set[uuid.UUID]:
    """Filas de "Otros" ya clasificadas cuyo registro derivado SÍ se pudo revertir.

    Desde F11, clasificar una fila a mano estampa
    ``source_row_ref='unclassified:{record_id}'`` en la venta/gasto que genera.
    Ese ref es la prueba de que el derivado está atado a esta fila y que la
    reversa lo alcanzó.

    Las clasificadas ANTES no lo tienen: su derivado nació sin
    ``source_upload_id``, sobrevive al borrado, y su fila de staging es el único
    rastro que queda hacia el archivo — por eso se conserva.

    Se mira sin filtrar ``voided_at``: en la reversa este helper corre DESPUÉS de
    anular las ventas y gastos del archivo, así que exigir "vivo" no devolvería
    ninguna. Lo que importa es la existencia del vínculo, no su estado.
    """
    prefijo = UNCLASSIFIED_ROW_REF_PREFIX
    refs: set[str] = set()
    for modelo in (SaleEntry, ExpenseEntry):
        res = await session.execute(
            select(modelo.source_row_ref).where(
                modelo.tenant_id == tenant_id,
                modelo.source_upload_id == file_id,
                modelo.source_row_ref.like(f"{prefijo}%"),
            )
        )
        refs.update(r for r in res.scalars().all() if r)

    ids: set[uuid.UUID] = set()
    for ref in refs:
        try:
            ids.add(uuid.UUID(ref.removeprefix(prefijo)))
        except ValueError:
            # Un ref con ese prefijo pero sin un uuid válido no se interpreta:
            # ante la duda, la fila se conserva.
            continue
    return ids


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
    conservados: list[dict[str, Any]] = []
    if creados:
        # MISMO criterio que la reversa (`_productos_con_otra_fuente`), pero
        # excluyendo las filas de este archivo: el preview corre ANTES de
        # anularlas, y sin excluirlas las propias ventas del archivo protegerían
        # a sus productos e informarían 0 donde la reversa desactiva varios.
        motivos_por_producto = await _productos_con_otra_fuente(
            session, tenant_id, creados, file_id=file_id, excluir_filas_del_archivo=True
        )
        protegidos = set(motivos_por_producto)
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
        # Los protegidos se calculaban SOLO para restarlos del conteo y se
        # descartaban: el usuario veía "5 productos" sin saber que otros 3 iban a
        # sobrevivir. Ahora salen con nombre y motivo. READ-ONLY: el preview no
        # marca `requires_completion` (eso lo hace la reversa, si se ejecuta).
        conservados.extend(
            await _describir_conservados_con_motivos(
                session, tenant_id, motivos_por_producto
            )
        )

    # Productos que el archivo MODIFICÓ: se les devuelve su valor anterior, salvo
    # que alguien los haya editado después (ahí se conservan y se informan).
    a_restaurar, conservados_por_edicion = await _clasificar_productos_modificados(
        session, file_id, tenant_id
    )
    conservados.extend(conservados_por_edicion)

    # Maestros: MISMA función que la reversa, en modo lectura. Anticiparlos con
    # una implementación aparte los habría hecho divergir.
    _m_desact, _m_rest, _m_conservados = await _revert_master_items(
        session, file_id, tenant_id, dry_run=True
    )
    a_restaurar += _m_rest
    conservados.extend(_m_conservados)

    # Campos cross-sección (F-D): misma función que la reversa, en modo
    # lectura — igual criterio que arriba, para no divergir del DELETE real.
    _, _conservados_cross_preview = await _revert_cross_field_items(
        session, file_id, tenant_id, dry_run=True
    )
    conservados.extend(_conservados_cross_preview)

    con_ledger = await _has_import_ledger(session, file_id, tenant_id)
    if not con_ledger:
        conservados.append(
            {
                "entity_type": "file",
                "id": str(file_id),
                "name": "Productos de este archivo",
                "reasons": [PreservationReason.SIN_LEDGER.value],
                "fields": [],
            }
        )
    # De las ya clasificadas, sólo se conservan las que NO dejaron procedencia:
    # las clasificadas desde F11 llevan su ref y su derivado se revierte con el
    # resto. Reportarlas todas como "sin procedencia" sería avisar de un problema
    # que ya no existe.
    _con_procedencia = await _otros_clasificados_revertidos(session, file_id, tenant_id)
    _clasificadas_huerfanas = max(0, otros_ya_clasificados - len(_con_procedencia))
    if _clasificadas_huerfanas:
        conservados.append(
            {
                "entity_type": "unclassified",
                "id": str(file_id),
                "name": (
                    f"{_clasificadas_huerfanas} filas ya clasificadas desde «Otros»"
                ),
                "reasons": [
                    PreservationReason.OTRO_CLASIFICADO_HISTORICO_SIN_PROCEDENCIA.value
                ],
                "fields": [],
            }
        )

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
        "productos_a_restaurar": a_restaurar,
        "conservados": conservados,
    }


async def _describir_conservados_con_motivos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    motivos_por_producto: dict[uuid.UUID, list[PreservationReason]],
    *,
    marcar_para_completar: bool = False,
) -> list[dict[str, Any]]:
    """Filas legibles para el usuario, con el motivo REAL de cada producto.

    ``marcar_para_completar`` pone ``requires_completion=True``: el producto
    sobrevive por sus dependencias, pero el archivo que respaldaba sus valores ya
    no está. Dejarlo como si nada implicaría que esos precios siguen teniendo
    fuente. NO se ponen precios en NULL — ``sale_price_ars`` es NOT NULL y la UI
    no maneja el vacío; el usuario los completa.
    """
    if not motivos_por_producto:
        return []
    res = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant_id, Product.id.in_(set(motivos_por_producto))
        )
    )
    filas: list[dict[str, Any]] = []
    for producto in res.scalars().all():
        if marcar_para_completar:
            producto.requires_completion = True
        filas.append(
            {
                "entity_type": "product",
                "id": str(producto.id),
                "name": producto.name,
                "reasons": [r.value for r in motivos_por_producto[producto.id]],
                "fields": [],
            }
        )
    return filas


async def _clasificar_productos_modificados(
    session: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[int, list[dict[str, Any]]]:
    """(cuántos se pueden restaurar, cuáles se conservan) — READ-ONLY.

    Comparte criterio con la reversa: se restaura el ``before`` del PRIMER item y
    se compara el ``after`` del ÚLTIMO contra el estado actual. Acá NO se muta —
    el preview es una estimación; la decisión autoritativa la toma el DELETE
    dentro de su transacción.
    """
    items = await _product_items_updated_by_file(session, file_id, tenant_id)
    primero: dict[uuid.UUID, DataRepairItem] = {}
    ultimo: dict[uuid.UUID, DataRepairItem] = {}
    for it in items:
        if it.product_id is None:
            continue
        primero.setdefault(it.product_id, it)
        ultimo[it.product_id] = it

    restaurables = 0
    conservados: list[dict[str, Any]] = []
    for pid, item in primero.items():
        prod = await session.get(Product, pid)
        if prod is None or prod.tenant_id != tenant_id:
            continue
        if entity_changed_since_ledger(prod, ultimo[pid].after_json):
            conservados.append(
                {
                    "entity_type": "product",
                    "id": str(pid),
                    "name": prod.name,
                    "reasons": [PreservationReason.EDICION_MANUAL_POSTERIOR.value],
                    "fields": [],
                }
            )
        elif item.before_json:
            restaurables += 1
    return restaurables, conservados


async def revert_file_data(
    session: AsyncSession,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
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
    # Lo que sobrevive al borrado, con nombre y motivo. Se devuelve al caller para
    # que el DELETE lo informe: un borrado que no puede revertir todo tiene que
    # decirlo, no responder 204 como si hubiera limpiado.
    conservados: list[dict[str, Any]] = []

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
        motivos_por_producto = await _productos_con_otra_fuente(
            session, tenant_id, creados, file_id=file_id
        )
        protegidos = set(motivos_por_producto)
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
        # Los protegidos sobreviven: el usuario tiene que saber CUÁLES y por qué,
        # o el borrado le miente diciendo que limpió todo. Además, se los marca
        # para completar: el archivo que los trajo ya no respalda sus valores.
        conservados.extend(
            await _describir_conservados_con_motivos(
                session, tenant_id, motivos_por_producto, marcar_para_completar=True
            )
        )

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
            conservados.append(
                {
                    "entity_type": "product",
                    "id": str(_pid),
                    "name": _prod.name,
                    "reasons": [PreservationReason.EDICION_MANUAL_POSTERIOR.value],
                    "fields": [],
                }
            )
            continue
        if restore_from_before(_prod, "product", _item.before_json or {}):
            contadores["productos_restaurados"] += 1

    # 3-ter. Clientes y proveedores que el archivo trajo. Hasta acá NO se
    #    revertían: un archivo que creaba 50 clientes los dejaba vivos y sin
    #    manera de saber de dónde salieron.
    _mc, _mr, _conservados_maestros = await _revert_master_items(
        session, file_id, tenant_id
    )
    contadores["maestros_desactivados"] = _mc
    contadores["maestros_restaurados"] = _mr
    conservados.extend(_conservados_maestros)

    # 3-quater. Campos cross-sección (F-D) que el archivo escribió sobre
    #    clientes/proveedores YA existentes — nunca desactiva nada, sólo
    #    devuelve el campo puntual a su valor anterior (o lo conserva si
    #    alguien lo tocó después, ver `_revert_cross_field_items`).
    _cfr, _conservados_cross = await _revert_cross_field_items(session, file_id, tenant_id)
    contadores["campos_cross_restaurados"] = _cfr
    conservados.extend(_conservados_cross)

    # 4. Filas de "Otros" del archivo.
    #
    #    Las PENDING se borran siempre: nunca fueron dato de negocio, son staging.
    #
    #    Las IMPORTED (el usuario ya las clasificó) dependen de si su registro
    #    derivado se pudo revertir:
    #      - Clasificadas DESDE F11: el derivado lleva
    #        `source_row_ref='unclassified:{id}'`, así que los pasos de arriba ya
    #        lo anularon. La fila de staging se borra también — dejarla viva
    #        apuntando a un archivo que ya no existe no le sirve a nadie.
    #      - Clasificadas ANTES: el derivado nació sin `source_upload_id` y la
    #        reversa no lo alcanza. Borrar la fila destruiría el ÚNICO rastro que
    #        queda hacia el archivo, dejando el dato huérfano y sin explicación.
    #        Se conservan y se informan aparte.
    _ids_con_procedencia = await _otros_clasificados_revertidos(
        session, file_id, tenant_id
    )
    otros_res = await session.execute(
        select(UnclassifiedRecord.id).where(
            UnclassifiedRecord.tenant_id == tenant_id,
            UnclassifiedRecord.uploaded_file_id == file_id,
            (UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING)
            | (UnclassifiedRecord.id.in_(_ids_con_procedencia)),
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

    # Sin ledger no se puede afirmar qué productos creó el archivo: se informa
    # como conservado en vez de adivinar.
    if not await _has_import_ledger(session, file_id, tenant_id):
        conservados.append(
            {
                "entity_type": "file",
                "id": str(file_id),
                "name": "Productos de este archivo",
                "reasons": [PreservationReason.SIN_LEDGER.value],
                "fields": [],
            }
        )

    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="INGESTION_FILE_DELETED_WITH_DATA",
            decision_data={
                "file_id": str(file_id),
                **contadores,
                # La auditoría guarda TAMBIÉN lo que no se pudo revertir: sin eso,
                # un borrado parcial es indistinguible de uno completo al leer el
                # log meses después.
                "conservados": conservados,
            },
            triggered_by="ingestion:delete_file",
            actor_user_id=actor_user_id,
            context={"source": "file_deletion_service.revert_file_data"},
            created_at=ahora,
        )
    )
    logger.info(
        "ingestion.delete.reverted",
        file_id=str(file_id),
        conservados=len(conservados),
        **contadores,
    )
    contadores_out: dict[str, Any] = dict(contadores)
    contadores_out["conservados"] = conservados
    return contadores_out


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
