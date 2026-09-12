"""Escenario compartido por los tests de relectura contra PostgreSQL real (E6b).

Un archivo de compras es el más chico que ejerce todo lo que una relectura puede
tocar: en un solo import crea el gasto, el producto, su costo, el movimiento de
inventario y el maestro del proveedor. Los tests de E6b necesitan ese mismo
escenario y el mismo snapshot, así que vive acá y no duplicado: dos copias del
snapshot divergirían y cada test mediría un universo distinto.

No define fixtures — las arma cada módulo, porque el ciclo de vida (por test, por
módulo) es decisión de quien mide.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import Table, delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.file_deletion_service import (
    build_master_details,
    record_import_ledger,
    snapshot_masters_before_import,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.verticals import Vertical
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

#: Tablas que estos tests consultan. Igual criterio que el resto de los ``*_pg``:
#: contra el schema ya migrado el ``checkfirst`` queda no-op, y pedir
#: ``create_all`` de TODO el metadata falla —lo hace— porque hay modelos cuyo DDL
#: del ORM no coincide con lo que construyó la cadena de migraciones. El import
#: real escribe además en tablas que no se listan (balances, huellas, auditoría);
#: todas existen en una base migrada, que es el requisito de estos tests.
_MODELOS = (
    Tenant,
    BusinessProfile,
    UploadedFile,
    DataRepairRun,
    Supplier,
    Product,
    ExpenseEntry,
    InventoryMovement,
)

PRODUCTO = "Vela aromatica 200g"
PROVEEDOR = "Distribuidora Sur"
#: `precio_compra` está a propósito: sin una columna de costo, el import no
#: escribe `unit_cost_ars` y los tests del ledger no tendrían ningún costo que
#: restaurar — el caso "reversión parcial por edición posterior" no se podría
#: montar sobre un campo que el archivo nunca tocó.
HEADERS = ["fecha", "articulo", "cantidad", "total", "precio_compra", "proveedor"]


def fila(
    dia: int, articulo: str, cantidad: str, total: str, precio_compra: str = "1200"
) -> dict[str, str]:
    return {
        "fecha": f"2024-03-{dia:02d}",
        "articulo": articulo,
        "cantidad": cantidad,
        "total": total,
        "precio_compra": precio_compra,
        "proveedor": PROVEEDOR,
    }


def summary(filas: list[dict[str, str]]) -> dict[str, Any]:
    """El summary de una hoja de compras, con las filas en el orden que se pase."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": False,
        "has_gasto": True,
        "row_count": len(filas),
        "headers": HEADERS,
        "gastos_detectados": filas,
        "preview_rows": filas,
        "confirmed_fields": {"gastos": True},
    }


async def crear_engine(dsn: str, clave_ddl: int) -> AsyncEngine:
    """Engine asyncpg propio (NullPool → cada sesión, su conexión física)."""
    engine = create_async_engine(dsn, poolclass=NullPool)
    tablas: list[Table] = [cast("Table", m.__table__) for m in _MODELOS]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": clave_ddl})
        await conn.run_sync(Base.metadata.create_all, tables=tablas, checkfirst=True)
    return engine


async def limpiar(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    """Borra en orden inverso al de creación: las hijas antes que ``tenants``."""
    async with factory() as s:
        for modelo in (
            InventoryMovement,
            ExpenseEntry,
            Product,
            Supplier,
            DataRepairRun,
            UploadedFile,
            BusinessProfile,
        ):
            await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
        await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        await s.commit()


async def preparar_tenant_y_archivo(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    filas: list[dict[str, str]],
    *,
    con_ledger: bool = True,
) -> uuid.UUID:
    """Tenant + perfil + archivo confirmado, SIN importar todavía.

    Separado del import para poder tomar un baseline del tenant antes de que el
    archivo produzca un solo efecto: es lo único contra lo que se puede afirmar
    que un borrado "devolvió todo".

    ``con_ledger`` sella ``ingestion_version`` como lo hace
    ``finalize_import_lease`` al confirmar. **No es cosmético**: el borrado
    decide por ese campo si el archivo trae ledger, así que un escenario que no
    lo selle mide el camino LEGACY creyendo medir el moderno.
    """
    file_id = uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        # Sin perfil el import levanta `UnknownVerticalError`: desde que se
        # eliminaron los fallbacks de rubro, un tenant sin perfil es un estado
        # roto y los servicios prefieren fallar antes que asumir kiosco.
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code=Vertical.KIOSCO_ALMACEN.value,
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=False,
            )
        )
        await s.flush()
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="compras.xlsx",
                s3_key="k",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="gastos",
                processing_status=PROCESSING_STATUS_DONE,
                parsed_summary_json=summary(filas),
                ingestion_version=INGESTION_VERSION if con_ledger else 1,
            )
        )
        await s.commit()
    return file_id


async def importar_archivo(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    filas: list[dict[str, str]],
    *,
    con_ledger: bool = True,
) -> None:
    """Aplica los efectos del archivo, COMMITEADOS — como el confirm real.

    **El ledger se escribe acá y no es opcional para que el escenario sea
    fiel.** El endpoint de confirm hace tres cosas en la misma transacción:
    snapshot de maestros, ``insert_confirmed_data(return_details=True)`` y
    ``record_import_ledger``. Un escenario que llamara sólo a la del medio
    produciría un archivo sellado con ``ingestion_version`` moderno pero SIN
    ledger — un estado que la confirmación real no puede dejar—, y los tests de
    borrado medirían un mundo inexistente: el reverso de productos y maestros
    sale del ledger, así que sin él "no revirtió" es un artefacto del test.
    """
    async with factory() as s:
        antes_clientes, antes_proveedores = await snapshot_masters_before_import(s, tenant_id)
        counts = await insert_confirmed_data(
            s,
            tenant_id,
            summary(filas),
            {"gastos": True},
            return_details=True,
            uploaded_file_id=file_id,
        )
        # Mismo consumo que el confirm: los detalles viajan DENTRO de `counts`.
        detalles = counts.pop("product_details", []) or []
        if con_ledger:
            maestros = await build_master_details(s, counts, antes_clientes, antes_proveedores)
            await record_import_ledger(
                s,
                tenant_id=tenant_id,
                file_id=file_id,
                product_details=detalles,
                master_details=maestros,
            )
        await s.commit()


async def importar_la_primera_vez(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    filas: list[dict[str, str]],
    *,
    con_ledger: bool = True,
) -> uuid.UUID:
    """El estado de partida de los tests de relectura: preparar + importar.

    Tiene que estar commiteado de verdad — si el baseline viviera en la misma
    transacción que la relectura, un rollback lo borraría también y el test
    estaría comparando dos vacíos.
    """
    file_id = await preparar_tenant_y_archivo(factory, tenant_id, filas, con_ledger=con_ledger)
    await importar_archivo(factory, tenant_id, file_id, filas, con_ledger=con_ledger)
    return file_id


async def estado(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> dict[str, Any]:
    """Todo lo que la relectura puede tocar, leído en una sesión NUEVA.

    Con los campos que un cambio silencioso movería —el ``voided_at`` de cada
    gasto, el stock y el costo de cada producto, el signo y la cantidad de cada
    movimiento—. Contar filas no alcanza: una relectura que anula un gasto y crea
    otro deja el mismo total.
    """
    async with factory() as s:
        gastos = (
            (
                await s.execute(
                    select(ExpenseEntry)
                    .where(ExpenseEntry.tenant_id == tenant_id)
                    .order_by(ExpenseEntry.id)
                )
            )
            .scalars()
            .all()
        )
        productos = (
            (
                await s.execute(
                    select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
                )
            )
            .scalars()
            .all()
        )
        proveedores = (
            (
                await s.execute(
                    select(Supplier)
                    .where(Supplier.tenant_id == tenant_id)
                    .order_by(Supplier.name)
                )
            )
            .scalars()
            .all()
        )
        movimientos = (
            (
                await s.execute(
                    select(InventoryMovement)
                    .where(InventoryMovement.tenant_id == tenant_id)
                    .order_by(InventoryMovement.id)
                )
            )
            .scalars()
            .all()
        )
        # Lista y no `scalar_one()`: un escenario que sube un SEGUNDO archivo
        # —el caso de la misma planilla cargada de nuevo— rompía el helper con
        # `MultipleResultsFound`, y el error aparecía lejos de su causa.
        archivos = (
            (
                await s.execute(
                    select(UploadedFile)
                    .where(UploadedFile.tenant_id == tenant_id)
                    .order_by(UploadedFile.created_at)
                )
            )
            .scalars()
            .all()
        )
        runs = (
            (
                await s.execute(
                    select(DataRepairRun)
                    .where(DataRepairRun.tenant_id == tenant_id)
                    .order_by(DataRepairRun.created_at)
                )
            )
            .scalars()
            .all()
        )
        return {
            "gastos": [
                (
                    str(g.id),
                    str(g.amount),
                    g.voided_at is not None,
                    str(g.product_id),
                    g.category,
                    g.expense_type,
                    g.has_user_edits,
                )
                for g in gastos
            ],
            # `is_active` incluido: el borrado DESACTIVA productos, no los
            # elimina. Un snapshot que no lo mire ve residuo donde hubo reversa —
            # y no vería una reversa que falta.
            "productos": [
                (
                    p.name,
                    int(p.stock_units or 0),
                    str(p.unit_cost_ars),
                    p.requires_completion,
                    p.is_active,
                )
                for p in productos
            ],
            "proveedores": [(p.name, p.deactivated_at is None) for p in proveedores],
            # `voided_at` incluido: el borrado ANULA los movimientos, no los
            # elimina, así que un snapshot que no lo mire ve residuo donde hubo
            # reversa (y no vería una reversa que falta).
            "movimientos": [
                (str(m.movement_type), int(m.qty), str(m.unit_cost), m.voided_at is not None)
                for m in movimientos
            ],
            "archivos": [
                (a.reread_status, a.ingestion_version, a.reread_at is not None, a.reread_summary)
                for a in archivos
            ],
            "runs": [(r.status, r.repair_type) for r in runs],
        }


def gastos_vivos(snapshot: dict[str, Any]) -> list[tuple[str, str, bool, str]]:
    return [g for g in snapshot["gastos"] if not g[2]]
