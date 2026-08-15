"""F-ID: `assign_next_sequence` — contador atómico por (tenant, entity_type, prefix).

La garantía de concurrencia real (que dos transacciones concurrentes nunca
reciben el mismo valor) sólo se puede probar contra PostgreSQL — ver
`app/tests/integration/test_entity_code_sequences_contention_pg.py`. Acá se
prueba el comportamiento secuencial: primera llamada da 1, la siguiente da 2,
prefijos/entidades/tenants distintos son contadores independientes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.entity_code_service import assign_next_sequence
from app.persistence.models.tenant import Tenant


async def test_primera_llamada_da_1(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    value = await assign_next_sequence(db_session, sample_tenant.tenant_id, "product", "TEX")
    assert value == 1


async def test_llamadas_sucesivas_incrementan(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    first = await assign_next_sequence(db_session, sample_tenant.tenant_id, "customer", "CLI")
    second = await assign_next_sequence(db_session, sample_tenant.tenant_id, "customer", "CLI")
    third = await assign_next_sequence(db_session, sample_tenant.tenant_id, "customer", "CLI")
    assert (first, second, third) == (1, 2, 3)


async def test_prefijos_distintos_son_contadores_independientes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tex = await assign_next_sequence(db_session, sample_tenant.tenant_id, "product", "TEX")
    gen = await assign_next_sequence(db_session, sample_tenant.tenant_id, "product", "GEN")
    assert tex == 1
    assert gen == 1


async def test_entity_type_distinto_es_contador_independiente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    cli = await assign_next_sequence(db_session, sample_tenant.tenant_id, "customer", "CLI")
    prv = await assign_next_sequence(db_session, sample_tenant.tenant_id, "supplier", "PRV")
    assert cli == 1
    assert prv == 1


async def test_tenant_distinto_es_contador_independiente(db_session: AsyncSession) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    db_session.add(Tenant(tenant_id=tenant_a, legal_name="A", display_name="A"))
    db_session.add(Tenant(tenant_id=tenant_b, legal_name="B", display_name="B"))
    await db_session.flush()

    a1 = await assign_next_sequence(db_session, tenant_a, "product", "GEN")
    b1 = await assign_next_sequence(db_session, tenant_b, "product", "GEN")
    a2 = await assign_next_sequence(db_session, tenant_a, "product", "GEN")

    assert (a1, b1, a2) == (1, 1, 2)
