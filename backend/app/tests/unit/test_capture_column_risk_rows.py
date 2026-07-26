"""F8b (Task 3) — primitiva idempotente de captura de riesgo de columnas.

``_capture_column_risk_rows`` es la primitiva que Task 4 va a invocar cuando
una decisión ``route_affected_rows_to_others`` rutea las filas afectadas de un
contexto a la bandeja "Otros". Estos tests cubren el contrato del brief:

- UNA sola ``UnclassifiedRecord`` por fila, combinando TODOS los campos
  problemáticos de esa fila (no una por campo).
- Huella PROPIA ``risk:{context_id}:{row_index}`` (namespace ``risk``, nunca
  el ancla ``IMPORT_ROW`` de venta/gasto) registrada DESPUÉS de persistir la
  captura (invariante 6) — un reintento con la misma fila no duplica.
- Dos contextos distintos con el mismo ``row_index`` no colisionan.
- ``context_label`` sin PII (invariante 7): solo nombres de columna, nunca
  los valores crudos de la fila.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    _capture_column_risk_rows,
    _risk_row_anchor,
)
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord


@pytest.mark.asyncio
async def test_one_record_per_row_combines_all_bad_fields(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una fila con 3 campos malos → UN solo UnclassifiedRecord con los 3."""
    tid = sample_tenant.tenant_id
    affected_rows = {
        4: {
            "email": "no-es-un-mail",
            "telefono": "abc123",
            "vencimiento": "31/13/2026",
        }
    }

    created = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Clientes",
        entity_type="customer",
        affected_rows=affected_rows,
    )
    await db_session.flush()

    assert created == 1
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
    )
    records = result.scalars().all()
    assert len(records) == 1
    record = records[0]
    assert record.suggested_entity == "customer"
    # Las columnas problemáticas de negocio (excluyendo la key reservada de
    # correlación __risk_ref__ que agrega Task 5).
    assert {k for k in record.row_data if not k.startswith("__")} == {
        "email",
        "telefono",
        "vencimiento",
    }
    assert record.row_data["email"] == "no-es-un-mail"
    assert record.row_data["telefono"] == "abc123"
    assert record.row_data["vencimiento"] == "31/13/2026"


@pytest.mark.asyncio
async def test_fingerprint_registered_after_persist_retry_does_not_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La huella se registra DESPUÉS de persistir; un reintento con la misma
    fila (mismo contexto + índice) hace skip sin duplicar (invariante 6)."""
    tid = sample_tenant.tenant_id
    affected_rows = {7: {"cuit": "no-valido"}}

    first = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Proveedores",
        entity_type="supplier",
        affected_rows=affected_rows,
    )
    await db_session.flush()

    anchor = _risk_row_anchor(tid, None, "sheet:Proveedores", 7)
    fp_count_after_first = await db_session.scalar(
        select(func.count()).select_from(OperationFingerprint).where(
            OperationFingerprint.tenant_id == tid,
            OperationFingerprint.action_type == "IMPORT_ROW",
        )
    )
    assert fp_count_after_first == 1
    # Sanity: la huella persistida corresponde al ancla `risk:` esperado
    # (namespace propio, no el de venta/gasto).
    import hashlib

    expected_fp = hashlib.sha256(anchor.encode()).hexdigest()
    stored = await db_session.scalar(
        select(OperationFingerprint.fingerprint).where(
            OperationFingerprint.tenant_id == tid,
            OperationFingerprint.action_type == "IMPORT_ROW",
        )
    )
    assert stored == expected_fp

    # Reintento (misma fila, mismo contexto): NO debe duplicar.
    second = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Proveedores",
        entity_type="supplier",
        affected_rows=affected_rows,
    )
    await db_session.flush()

    assert first == 1
    assert second == 0
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_captura_guarda_clave_de_correlacion_pii_free(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Task 5: la captura guarda ``__risk_ref__`` = ``{context_id, row_index}`` en
    ``row_data`` para que la relectura pueda mapear la fila a su registro previo.
    La clave es PII-free (solo id de contexto + índice) y va bajo prefijo ``__``.
    """
    import json

    from app.application.services.ingestion_import_service import RISK_REF_KEY

    tid = sample_tenant.tenant_id
    await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=uuid.uuid4(),
        context_id="sheet:Clientes",
        entity_type="customer",
        affected_rows={9: {"email": "malo"}},
    )
    await db_session.flush()

    record = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalars().one()
    assert RISK_REF_KEY in record.row_data
    ref = json.loads(record.row_data[RISK_REF_KEY])
    assert ref == {"context_id": "sheet:Clientes", "row_index": 9}


@pytest.mark.asyncio
async def test_different_contexts_same_row_index_do_not_collide(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos contextos distintos con el mismo row_index no comparten huella."""
    tid = sample_tenant.tenant_id

    created_a = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Clientes",
        entity_type="customer",
        affected_rows={0: {"email": "malo-a"}},
    )
    created_b = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Proveedores",
        entity_type="supplier",
        affected_rows={0: {"email": "malo-b"}},
    )
    await db_session.flush()

    assert created_a == 1
    assert created_b == 1
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
    )
    records = result.scalars().all()
    assert len(records) == 2
    entities = {r.suggested_entity for r in records}
    assert entities == {"customer", "supplier"}


@pytest.mark.asyncio
async def test_context_label_has_no_pii(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """context_label lista SOLO nombres de columna — nunca los valores crudos
    (documentos/emails/teléfonos) de la fila (invariante 7)."""
    tid = sample_tenant.tenant_id
    pii_email = "juan.perez@example.com"
    pii_phone = "+54 9 11 5555-1234"
    affected_rows = {2: {"email": pii_email, "telefono": pii_phone}}

    await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Clientes",
        entity_type="customer",
        affected_rows=affected_rows,
    )
    await db_session.flush()

    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
    )
    record = result.scalars().one()
    assert record.context_label is not None
    assert pii_email not in record.context_label
    assert pii_phone not in record.context_label
    # Los NOMBRES de columna sí son esperables en el label (no son PII).
    assert "email" in record.context_label
    assert "telefono" in record.context_label
    # Pero los valores crudos SÍ deben estar en row_data (para que "Otros" los
    # pueda mostrar/editar) — la ausencia de PII es solo del label.
    assert record.row_data["email"] == pii_email
    assert record.row_data["telefono"] == pii_phone


@pytest.mark.asyncio
async def test_empty_capture_does_not_register_phantom_fingerprint(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Si la fila combinada queda vacía (nada que capturar), la huella NO se
    registra — de lo contrario una fila corregida en un reintento posterior
    quedaría bloqueada para siempre por una huella "fantasma" que nunca
    capturó nada. Este es el test que MUTA (ver mutation test en el reporte):
    si se registrara la huella ANTES de intentar la captura, este test falla
    porque el reintento (ahora con datos) queda huérfano — 0 creados."""
    tid = sample_tenant.tenant_id

    # Primera pasada: la fila "afectada" llega sin campos (caso borde) → no
    # hay nada que persistir.
    first = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Proveedores",
        entity_type="supplier",
        affected_rows={5: {}},
    )
    await db_session.flush()
    assert first == 0

    # Reintento (misma fila, ahora con el campo problemático real): DEBE
    # poder capturarse — la huella no debe haber quedado registrada antes.
    second = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=None,
        context_id="sheet:Proveedores",
        entity_type="supplier",
        affected_rows={5: {"cuit": "invalido"}},
    )
    await db_session.flush()

    assert second == 1
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_uploaded_file_id_distinguishes_anchor_from_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos archivos distintos del mismo tenant reusando el mismo context_id
    sintético ("table") no colisionan entre sí (docstring de _risk_row_anchor)."""
    tid = sample_tenant.tenant_id
    file_a = uuid.uuid4()
    file_b = uuid.uuid4()

    created_a = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=file_a,
        context_id="table",
        entity_type="sale",
        affected_rows={1: {"monto": "no-numerico"}},
    )
    created_b = await _capture_column_risk_rows(
        db_session,
        tenant_id=tid,
        uploaded_file_id=file_b,
        context_id="table",
        entity_type="sale",
        affected_rows={1: {"monto": "tampoco"}},
    )
    await db_session.flush()

    assert created_a == 1
    assert created_b == 1
