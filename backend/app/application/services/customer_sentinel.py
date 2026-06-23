"""Cliente sentinela "Local" — agrupa ventas sin cliente registrado.

Espejo del sentinela de proveedores ("No identificado",
``ingestion_import_service._resolve_or_create_sentinel_supplier``). Un kiosco que
no sabe quién compró registra la venta contra un único cliente **"Local"** por
tenant, marcado con ``custom_fields["_sentinel"] == "true"`` y garantizado por un
índice único parcial en la DB. Este cliente:

  - NO aparece en la lista normal ni en métricas (``list_by_tenant`` /
    ``count_active`` / ``get_inactive_customers`` lo excluyen);
  - NO es borrable ni editable a nivel backend;
  - NUNCA se usa para fiado (``payment_method == "account"``): esas ventas exigen
    un cliente real (rechazo 400 en los endpoints de ventas).

Se distingue por el flag, no por el nombre (el nombre es fijo, pero el flag es el
identificador canónico — espejo del sentinela de proveedores).
"""

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models._sentinel import SENTINEL_FLAG_KEY
from app.persistence.models.customer import Customer
from app.persistence.models.transaction import SaleEntry

LOCAL_CUSTOMER_NAME = "Local"


async def resolve_or_create_local_sentinel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> uuid.UUID:
    """Find-or-create del cliente sentinela "Local" del tenant.

    Busca por ``custom_fields["_sentinel"] == "true"`` (no por nombre). Maneja la
    concurrencia: si dos requests lo crean a la vez, el índice único parcial dispara
    ``IntegrityError`` → re-query. Devuelve el ``id`` del sentinela.
    """

    async def _find() -> uuid.UUID | None:
        # ``.as_string()`` es cross-dialect (PG + SQLite de tests); ``.astext`` es
        # solo PostgreSQL y rompe en SQLite.
        result = await session.execute(
            select(Customer.id).where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
                Customer.custom_fields[SENTINEL_FLAG_KEY].as_string() == "true",
            )
        )
        return result.scalars().first()

    found = await _find()
    if found is not None:
        return found

    new_id = uuid.uuid4()
    sentinel = Customer(
        id=new_id,
        tenant_id=tenant_id,
        name=LOCAL_CUSTOMER_NAME,
        custom_fields={SENTINEL_FLAG_KEY: "true"},
    )
    session.add(sentinel)
    try:
        # Flush dentro de un savepoint para disparar el índice único parcial ahora
        # (no en el commit del caller): si otra corrida ya lo creó, lo detectamos y
        # re-queryamos en vez de abortar la transacción entera.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        existing = await _find()
        if existing is None:  # pragma: no cover — el índice garantiza que exista
            raise
        return existing
    return new_id


async def assign_orphan_sales_to_local(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> int:
    """Reasigna las ventas del tenant sin cliente (``customer_id IS NULL``) al
    sentinela "Local". UNA query, idempotente. La usan los imports (al cerrar) y el
    script de backfill ``scripts/backfill_local_customer.py``.

    Nota: en Fase A los imports no mapean cliente, así que TODA venta importada cae
    a "Local" (incluido un eventual "account"/fiado de archivo). La regla estricta
    "fiado exige cliente real" aplica al alta interactiva (API de ventas), donde el
    usuario elige el método de pago explícitamente. Devuelve la cantidad reasignada.
    """
    # Solo resolver/crear el sentinela si hay ventas huérfanas que reasignar.
    has_orphans = await session.execute(
        select(SaleEntry.id).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.customer_id.is_(None),
            SaleEntry.voided_at.is_(None),
        ).limit(1)
    )
    if has_orphans.first() is None:
        return 0
    local_id = await resolve_or_create_local_sentinel(session, tenant_id)
    result = await session.execute(
        update(SaleEntry)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.customer_id.is_(None),
            SaleEntry.voided_at.is_(None),
        )
        .values(customer_id=local_id)
    )
    return int(cast("CursorResult[Any]", result).rowcount or 0)
