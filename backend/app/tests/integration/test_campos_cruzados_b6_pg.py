"""B·6 — completar la ficha desde otra sección y revertirla, contra PostgreSQL real.

SQLite ignora el CHECK ``ck_repair_items_action``: la suite común no puede
probar que la migración ``20261021_0003`` acepta las acciones nuevas, ni que el
camino entero (import con buffer → ledger por campo → reversa que conserva lo
editado después) funciona con los tipos y el JSONB de Postgres. Todo corre dentro
de una transacción que se revierte. Correr con ``-n 0`` contra una base con
``alembic upgrade head``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.services.file_deletion_service import (
    preview_file_deletion,
    record_import_ledger,
    revert_file_data,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DNI = "30111222"


@contextlib.asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                yield AsyncSession(bind=c, join_transaction_mode="create_savepoint")
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def _tenant_cliente_y_archivo(s: AsyncSession) -> tuple[uuid.UUID, Customer, UploadedFile]:
    tid = uuid.uuid4()
    s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
    await s.flush()
    s.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tid,
            vertical_code=Vertical.KIOSCO_ALMACEN.value,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
        )
    )
    cliente = Customer(tenant_id=tid, name="Cliente Uno", dni=_DNI)
    archivo = UploadedFile(
        tenant_id=tid,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    s.add_all([cliente, archivo])
    await s.flush()
    return tid, cliente, archivo


async def test_import_ledger_y_reversa_que_conserva_lo_editado_despues() -> None:
    async with _sesion() as s:
        tid, cliente, archivo = await _tenant_cliente_y_archivo(s)

        counts = await insert_confirmed_data(
            s,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "ventas_detectadas": [
                    {
                        "fecha": "2024-01-15",
                        "monto": "3000",
                        "doc": _DNI,
                        "apellido": "Pérez",
                        "domicilio": "Mitre 10",
                        "iva": "Monotributista",
                    }
                ],
            },
            {"ventas": True},
            column_mappings={
                "doc": "customer_dni",
                "apellido": "customer:last_name",
                "domicilio": "customer:address",
                "iva": "customer:iva_condition",
            },
            uploaded_file_id=archivo.id,
            apply_cross_fields=True,
        )
        assert counts["cross_fields_aplicados"] == 3
        await record_import_ledger(
            s,
            tenant_id=tid,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=counts.pop("cross_field_details"),
        )
        await s.flush()

        acciones = (
            await s.execute(
                select(DataRepairItem.action).where(DataRepairItem.source_file_id == archivo.id)
            )
        ).scalars().all()
        assert sorted(acciones) == ["UPDATE_CUSTOMER_CROSS_FIELD"] * 3

        await s.refresh(cliente)
        assert cliente.iva_condition == "monotributo"
        # Edición posterior de UN campo.
        cliente.address = "Mitre 1050"
        await s.flush()

        preview = await preview_file_deletion(s, archivo.id, tid)
        assert preview["campos_cruzados_a_restaurar"] == 2

        contadores = await revert_file_data(s, archivo.id, tid)
        await s.flush()
        await s.refresh(cliente)

        assert cliente.last_name is None
        assert cliente.iva_condition is None
        assert cliente.address == "Mitre 1050"
        assert contadores["campos_cruzados_restaurados"] == 2
        [conservado] = contadores["conservados"]
        assert conservado["fields"] == ["address"]
        assert conservado["reasons"] == ["campo_modificado_posteriormente"]


async def test_el_check_acepta_las_acciones_nuevas_y_sigue_rechazando_otras() -> None:
    async with _sesion() as s:
        tid, _cliente, archivo = await _tenant_cliente_y_archivo(s)
        run = DataRepairRun(
            tenant_id=tid, repair_type="INGESTION_IMPORT", status="APPLIED", dry_run=False,
            details_json={},
        )
        s.add(run)
        await s.flush()
        for accion in ("UPDATE_CUSTOMER_CROSS_FIELD", "UPDATE_SUPPLIER_CROSS_FIELD"):
            s.add(DataRepairItem(
                run_id=run.id, tenant_id=tid, source_file_id=archivo.id, action=accion,
                before_json={}, after_json={}, confidence="HIGH",
            ))
        await s.flush()

        with pytest.raises(IntegrityError):
            async with s.begin_nested():
                s.add(DataRepairItem(
                    run_id=run.id, tenant_id=tid, source_file_id=archivo.id,
                    action="UPDATE_PRODUCT_CROSS_FIELD", confidence="HIGH",
                ))
                await s.flush()
