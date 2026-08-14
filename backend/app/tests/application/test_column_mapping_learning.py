"""Aprender el mapeo de un archivo con VARIAS hojas de la misma entidad.

Un libro con `Compras_Mercaderia`, `Compras_Insumos` y `Gastos_Fijos` manda las
tres al aprendizaje como `expense`, y las tres traen una columna `fecha`. El
confirm terminaba en 500:

    UniqueViolationError: duplicate key value violates unique constraint
    "uq_tcm_tenant_entity_col"
    DETAIL: Key (tenant_id, entity_type, source_column)=(..., expense, fecha)
    already exists

La causa es leer lo que uno mismo acaba de escribir: `save_mappings` hace
`SELECT` y, si no encuentra, `add`. En producción la sesión corre con
`autoflush=False`, así que la fila agregada en la vuelta anterior sigue PENDIENTE
y el `SELECT` no la ve → inserta una segunda con la misma clave.

Los tests que ya existían de `save_mappings` usan `AsyncMock`, así que prueban el
mundo que ellos mismos arman y no pueden ver esto. Por eso éste va contra la base
de verdad y dentro de `no_autoflush`, que es como corre producción.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import ColumnMappingService
from app.persistence.models.column_mapping import TenantColumnMapping
from app.persistence.models.tenant import Tenant


async def _contar(db: AsyncSession, tenant_id: uuid.UUID, col: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(TenantColumnMapping)
            .where(
                TenantColumnMapping.tenant_id == tenant_id,
                TenantColumnMapping.source_column == col,
            )
        )
    ).scalar_one()


async def test_tres_hojas_con_la_misma_columna_aprenden_una_sola_vez(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    svc = ColumnMappingService(db_session)
    with db_session.no_autoflush:
        await svc.save_mappings(
            sample_tenant.tenant_id,
            "expense",
            [
                {"source_column": "Fecha", "target_field": "expense_date"},
                {"source_column": "Fecha", "target_field": "expense_date"},
                {"source_column": "Fecha", "target_field": "expense_date"},
            ],
        )
        await db_session.flush()

    assert await _contar(db_session, sample_tenant.tenant_id, "fecha") == 1


async def test_la_confianza_no_se_infla_por_repetir_hojas_del_mismo_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Tres hojas del MISMO archivo son una confirmación, no tres: contarlas por
    separado le daría a un alias la confianza de tres archivos distintos."""
    svc = ColumnMappingService(db_session)
    with db_session.no_autoflush:
        await svc.save_mappings(
            sample_tenant.tenant_id,
            "expense",
            [
                {"source_column": "Monto", "target_field": "amount"},
                {"source_column": "Monto", "target_field": "amount"},
                {"source_column": "Monto", "target_field": "amount"},
            ],
        )
        await db_session.flush()

    fila = (
        await db_session.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == sample_tenant.tenant_id,
                TenantColumnMapping.source_column == "monto",
            )
        )
    ).scalar_one()
    assert fila.confirmed_count == 1


async def test_dos_hojas_que_mapean_distinto_no_aprenden_esa_columna(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Evidencia contradictoria dentro del mismo archivo no es una preferencia
    del usuario: es ambigüedad. Aprender cualquiera de las dos sería elegir por
    orden de hoja, que es el last-wins silencioso que este pipeline evita."""
    svc = ColumnMappingService(db_session)
    with db_session.no_autoflush:
        await svc.save_mappings(
            sample_tenant.tenant_id,
            "expense",
            [
                {"source_column": "Fecha", "target_field": "expense_date"},
                {"source_column": "Fecha", "target_field": "transaction_date"},
            ],
        )
        await db_session.flush()

    assert await _contar(db_session, sample_tenant.tenant_id, "fecha") == 0


async def test_ignorar_la_columna_en_otra_hoja_no_borra_el_mapeo_bueno(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Ignorar una columna en una hoja no dice NADA sobre qué significa esa
    columna. Tratarlo como contradicción descartaba el mapeo legítimo de la otra
    hoja — y si el alias ya existía, lo dejaba sin refrescar."""
    svc = ColumnMappingService(db_session)
    with db_session.no_autoflush:
        await svc.save_mappings(
            sample_tenant.tenant_id,
            "expense",
            [
                {"source_column": "Fecha", "target_field": "expense_date"},
                {"source_column": "Fecha", "target_field": "ignore"},
            ],
        )
        await db_session.flush()

    fila = (
        await db_session.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == sample_tenant.tenant_id,
                TenantColumnMapping.source_column == "fecha",
            )
        )
    ).scalar_one()
    assert fila.target_field == "expense_date"
