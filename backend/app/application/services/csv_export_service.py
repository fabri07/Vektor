"""Cambio 3 del plan de conservación y acceso a datos de negocio — exportación
completa del lado servidor (docs/plans/conservacion-y-acceso-datos-negocio.md).

Distinto del CSV que ya arma `SmartTable` en el frontend (columnas VISIBLES,
filas YA CARGADAS — acotadas por los techos de acumulación de cada service,
ej. `PRODUCTS_MAX_ACCUMULATED`). Esto es la otra mitad del contrato de
cierre: "todos los campos de negocio autorizados y todos los registros del
alcance seleccionado, aunque las columnas estén ocultas o las filas no estén
cargadas en el navegador."

No es un sistema de jobs asíncronos (decisión explícita del plan) — es un
`StreamingResponse` síncrono que pagina puertas adentro con los repositorios
existentes, así que nunca junta el catálogo completo en memoria.

Lectura estable ante escrituras concurrentes
---------------------------------------------
Los `list_by_tenant` de cada repo ordenan por una columna NO única
(`created_at`/`transaction_date`) y paginan con OFFSET/LIMIT. Un INSERT con el
mismo timestamp a mitad de la corrida puede correr una fila a la página
siguiente (se pierde) o a la anterior (se repite). La mitigación es
`REPEATABLE READ` en Postgres ANTES de la primera lectura de ESTA
exportación: todas sus páginas ven el mismo snapshot MVCC, sin importar qué se
commitee después.

Por eso `stream_entity_csv` abre su PROPIA sesión en vez de reusar la del
request: para cuando el endpoint la llama, la sesión inyectada por FastAPI ya
corrió al menos dos queries (`get_current_user`/`get_current_tenant`
resolviendo el JWT), y Postgres exige fijar el nivel de aislamiento ANTES de
la primera sentencia de la transacción — sobre esa sesión ya sería tarde. Una
sesión nueva, dedicada, resuelve esto sin tocar la cadena de auth. SQLite
(tests) no tiene este nivel — no-op ahí.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.business_field_catalog_service import get_available_fields
from app.domain.business_field_catalog import read_by_value_path
from app.domain.csv_safety import format_export_value, formula_safe_cell
from app.domain.verticals import Vertical
from app.persistence.db.session import async_session_factory

#: Filas por página al leer un repo — no es el límite de la exportación (esa
#: no tiene techo), es el tamaño del lote que se trae a memoria por vez.
_PAGE_SIZE = 500


async def _paginar_productos(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool
) -> AsyncIterator[Any]:
    from app.persistence.repositories.product_repository import ProductRepository

    repo = ProductRepository(session)
    offset = 0
    while True:
        pagina = await repo.list_by_tenant(
            tenant_id,
            is_active=None if include_inactive else True,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        for fila in pagina:
            yield fila
        if len(pagina) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


async def _paginar_ventas(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool
) -> AsyncIterator[Any]:
    # Sin concepto de activo/inactivo — las ventas anuladas ya están
    # excluidas incondicionalmente por `list_by_tenant` (`voided_at IS NULL`).
    # `include_inactive` no aplica acá; se ignora a propósito.
    from app.persistence.repositories.transaction_repository import SaleRepository

    repo = SaleRepository(session)
    offset = 0
    while True:
        pagina = await repo.list_by_tenant(tenant_id, limit=_PAGE_SIZE, offset=offset)
        for fila in pagina:
            yield fila
        if len(pagina) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


async def _paginar_gastos(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool
) -> AsyncIterator[Any]:
    # Mismo motivo que ventas: `voided_at IS NULL` ya es incondicional.
    # `include_inactive` no aplica acá; se ignora a propósito.
    from app.persistence.repositories.transaction_repository import ExpenseRepository

    repo = ExpenseRepository(session)
    offset = 0
    while True:
        pagina = await repo.list_by_tenant(tenant_id, limit=_PAGE_SIZE, offset=offset)
        for fila in pagina:
            yield fila
        if len(pagina) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


async def _paginar_clientes(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool
) -> AsyncIterator[Any]:
    from app.persistence.repositories.customer_repository import CustomerRepository

    repo = CustomerRepository(session)
    offset = 0
    while True:
        pagina = await repo.list_by_tenant(
            tenant_id,
            limit=_PAGE_SIZE,
            offset=offset,
            include_inactive=include_inactive,
        )
        for fila in pagina:
            yield fila
        if len(pagina) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


async def _paginar_proveedores(
    session: AsyncSession, tenant_id: uuid.UUID, *, include_inactive: bool
) -> AsyncIterator[Any]:
    from app.persistence.repositories.supplier_repository import SupplierRepository

    repo = SupplierRepository(session)
    offset = 0
    while True:
        pagina = await repo.list_by_tenant(
            tenant_id,
            limit=_PAGE_SIZE,
            offset=offset,
            include_inactive=include_inactive,
        )
        for fila in pagina:
            yield fila
        if len(pagina) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


#: Allowlist de entidad → lector paginado. Nunca se interpola un nombre de
#: tabla/entidad que venga del cliente: el `entity_type` de la request solo
#: sirve para indexar este dict fijo (mismo criterio que
#: `AVAILABLE_ENTITY_TYPES` en business_field_catalog.py).
_ROW_READERS: dict[str, Callable[..., AsyncIterator[Any]]] = {
    "product": _paginar_productos,
    "sale": _paginar_ventas,
    "expense": _paginar_gastos,
    "customer": _paginar_clientes,
    "supplier": _paginar_proveedores,
}

EXPORTABLE_ENTITY_TYPES = frozenset(_ROW_READERS)


async def _fijar_snapshot_estable(session: AsyncSession) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Primera operación de esta sesión NUEVA y dedicada — ver el docstring del
    # módulo sobre por qué no puede ser la sesión del request.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


async def stream_entity_csv(
    tenant_id: uuid.UUID,
    vertical_code: Vertical,
    entity_type: str,
    *,
    include_inactive: bool = False,
) -> AsyncIterator[str]:
    """CSV completo de una entidad — todos los campos exportables del catálogo
    de lectura (Cambio 1), todas las filas del alcance, paginado puertas
    adentro. `include_inactive=False` por default (mismo alcance que ve la
    pantalla hoy: solo activos); `True` suma inactivos/dados de baja. Sale y
    expense no tienen este concepto (ver sus lectores) — el parámetro se
    ignora ahí sin error, no todas las entidades lo necesitan.

    Abre y cierra su PROPIA sesión (ver docstring del módulo) — el caller no
    pasa la del request.
    """
    reader = _ROW_READERS.get(entity_type)
    if reader is None:
        raise ValueError(f"exportación no soportada para entity_type={entity_type!r}")

    async with async_session_factory() as session:
        await _fijar_snapshot_estable(session)

        campos = await get_available_fields(session, tenant_id, vertical_code, entity_type)
        exportables = [f for f in campos if f["exportable"]]

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")

        def _volcar() -> str:
            valor = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return valor

        writer.writerow([formula_safe_cell(f["label"]) for f in exportables])
        # BOM UTF-8: mismo criterio que `lib/csv.ts` del frontend — sin esto,
        # Excel en es-AR desarma los acentos de labels/valores.
        yield "﻿" + _volcar()

        async for fila in reader(session, tenant_id, include_inactive=include_inactive):
            writer.writerow(
                [
                    formula_safe_cell(
                        format_export_value(
                            read_by_value_path(fila, f["value_path"]), f["data_type"]
                        )
                    )
                    for f in exportables
                ]
            )
            yield _volcar()
