"""I5-1 (revisión) — un maestro con CUIT, DNI, teléfono y CP numéricos, en Postgres.

Desde I5-1 el parser xlsx conserva las celdas numéricas como número. Para un
maestro de clientes eso es un CUIT, un DNI, un teléfono o un código postal: los
lectores de ``customer_import_service`` hacían ``.strip()`` sobre el ``int`` (500)
y, donde no, el número crudo llegaba a columnas ``String`` y asyncpg lo rechaza
(``DataError``). En SQLite no se ve: guarda lo que le den. El arreglo los pasa a
texto en el borde del import de maestros, igual que antes de I5-1.

Camino real: xlsx de openpyxl → ``parse_uploaded_content`` →
``insert_confirmed_data`` → INSERT en Postgres.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from openpyxl import Workbook
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _clientes() -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Clientes"
    hoja.append(["cliente", "cuit", "dni", "telefono", "cp", "email"])
    hoja.append(["Juan Perez", 20123456786, 30123456, 1155551234, 1406, "j@x.com"])
    hoja.append(["Ana Gomez", None, 31123456, 5491155556666, 0, "a@x.com"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


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


async def test_el_maestro_con_documentos_numericos_se_guarda_como_texto(
    sesiones: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    sm, tenant_id = sesiones
    summary = parse_uploaded_content(_clientes(), _XLSX, "clientes.xlsx")
    contexto = summary["mapping_contexts"][0]["context_id"]
    assert summary["mapping_contexts"][0]["entity_type"] == "customer"

    async with sm() as s:
        counts = await insert_confirmed_data(
            s,
            tenant_id,
            summary,
            {"clientes": True},
            context_mappings={
                contexto: {
                    "cliente": "name",
                    "cuit": "cuit",
                    "dni": "dni",
                    "telefono": "phone",
                    "cp": "postal_code",
                    "email": "email",
                }
            },
            context_entity={contexto: "customer"},
            context_confirmed={contexto: True},
            source="ingestion",
        )
        await s.commit()
    assert counts.get("clientes") == 2, {k: v for k, v in counts.items() if v}

    async with sm() as s:
        clientes = {
            c.dni: c
            for c in (
                await s.execute(select(Customer).where(Customer.tenant_id == tenant_id))
            ).scalars()
            if c.dni
        }
    juan, ana = clientes["30123456"], clientes["31123456"]
    assert juan.phone is not None and "1155551234" in juan.phone
    assert juan.postal_code == "1406"
    assert juan.cuit is not None and juan.cuit.replace("-", "") == "20123456786"
    assert ana.postal_code == "0", "un 0 nativo en un campo de texto es «0», no vacío"
