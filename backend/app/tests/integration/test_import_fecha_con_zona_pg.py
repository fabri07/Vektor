"""I6-6 — una fecha ISO con zona en un ARCHIVO se importa en hora argentina, en Postgres.

`transaction_date` es `timestamp without time zone`. La carga manual ya sacaba la
zona en el schema (`test_fecha_con_zona_pg.py`); el camino de archivos no: el
parser de fechas devolvía el datetime de `fromisoformat` CON zona, y asyncpg lo
rechaza en una columna sin zona (`can't subtract offset-naive and offset-aware
datetimes`), así que el confirm fallaba entero. En SQLite no se ve: se guarda la
hora UTC como local.

Acá se recorre el camino real: CSV → `parse_uploaded_content` →
`insert_confirmed_data` → INSERT en Postgres → releído con `func.date`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests._respaldo import productos_con_compra

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_CSV = (
    b"fecha,producto,total,cliente,forma de pago\n"
    b"2026-06-06T02:30:00Z,Vela,1500,Consumidor final,efectivo\n"
    b"2026-06-07T10:15:00-03:00,Vela,2500,Consumidor final,efectivo\n"
)


@pytest_asyncio.fixture
async def sesiones() -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID], None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_id = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
        await s.commit()
    try:
        yield sm, tenant_id
    finally:
        async with sm() as s:
            # Todo lo que el import y el respaldo hayan escrito para este tenant,
            # de las hojas a la raíz.
            existentes = set(
                (
                    await s.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
                    )
                ).scalars()
            )
            for tabla in reversed(Base.metadata.sorted_tables):
                if tabla.name in existentes and "tenant_id" in tabla.c and tabla.name != "tenants":
                    await s.execute(delete(tabla).where(tabla.c.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()
        await engine.dispose()


async def test_el_import_guarda_la_fecha_con_zona_en_hora_argentina(
    sesiones: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    sm, tenant_id = sesiones
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    # Lo que el confirm le agrega al summary antes de importar.
    summary["confirmed_fields"] = {"ventas": True}

    async with sm() as s:
        await productos_con_compra(s, tenant_id, ["Vela"])
        upload_id = uuid.uuid4()
        s.add(
            UploadedFile(
                id=upload_id,
                tenant_id=tenant_id,
                original_filename="ventas.csv",
                s3_key=f"tests/{upload_id}.csv",
                content_type="text/csv",
                size_bytes=len(_CSV),
                purpose="ingestion",
                status="uploaded",
                processing_status="DONE",
            )
        )
        await s.commit()

        # Como lo llama el confirm para una tabla sola: mapeo plano + sección.
        counts = await insert_confirmed_data(
            s,
            tenant_id,
            summary,
            {"ventas": True},
            column_mappings={
                "fecha": "transaction_date",
                "producto": "product_name",
                "total": "amount",
                "cliente": "customer_name",
                "forma de pago": "payment_method",
            },
            source="ingestion",
            uploaded_file_id=upload_id,
        )
        await s.commit()
    assert counts["ventas"] == 2, {k: v for k, v in counts.items() if v}

    async with sm() as s:
        filas = (
            await s.execute(
                select(SaleEntry.transaction_date, func.date(SaleEntry.transaction_date))
                .where(SaleEntry.tenant_id == tenant_id)
                .order_by(SaleEntry.transaction_date)
            )
        ).all()
    assert [f[0] for f in filas] == [
        datetime(2026, 6, 5, 23, 30),
        datetime(2026, 6, 7, 10, 15),
    ]
    assert all(f[0].tzinfo is None for f in filas)
    assert [f[1] for f in filas] == [date(2026, 6, 5), date(2026, 6, 7)], (
        "el día de negocio es el argentino, no el UTC"
    )
