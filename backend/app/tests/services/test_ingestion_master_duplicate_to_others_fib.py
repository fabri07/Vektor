"""F-I(B): en el camino general multi-hoja, una fila de clientes/proveedores
que repite la clave (documento/business_code) de otra fila del MISMO archivo
va a "Otros" — no se fusiona sola con la entidad de la fila anterior. Espejo,
por el camino multi-hoja, de los tests unitarios de
``test_customer_extraction.py``/``test_supplier_import.py``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord


def _hoja_de_clientes_con_duplicado() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "label": "Clientes",
                "entity_type": "customer",
                "source_kind": "sheet",
                "headers": ["Nombre", "DNI"],
                "fields": None,
                "preview_rows": [],
                "row_count": 2,
            },
        ],
        "clientes_detectados": [
            {"Nombre": "Juan Perez", "DNI": "30111222", "__context__": "sheet:Clientes"},
            # Mismo DNI que la fila anterior — duplicado dentro del archivo.
            {"Nombre": "Juan Perez V2", "DNI": "30111222", "__context__": "sheet:Clientes"},
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "proveedores_detectados": [],
        "stock_detectado": [],
    }


async def test_duplicado_de_clientes_en_multi_hoja_va_a_otros_con_uploaded_file_id(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    file = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="clientes.xlsx",
        s3_key=f"uploads/test/clientes-{sample_tenant.tenant_id}.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
    )
    db_session.add(file)
    await db_session.flush()

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _hoja_de_clientes_con_duplicado(),
        {"clientes": True},
        context_mappings={
            "sheet:Clientes": {"Nombre": "name", "DNI": "dni"},
        },
        context_confirmed={"sheet:Clientes": True},
        context_entity={"sheet:Clientes": "customer"},
        uploaded_file_id=file.id,
    )

    assert counts["clientes"] == 1
    assert counts["clientes_a_otros"] == 1

    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1
    assert customers[0].name == "Juan Perez"  # nunca lo tocó la fila 2

    pending = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id,
                UnclassifiedRecord.suggested_entity == "customer",
            )
        )
    ).scalars().all()
    assert len(pending) == 1
    assert pending[0].uploaded_file_id == file.id
