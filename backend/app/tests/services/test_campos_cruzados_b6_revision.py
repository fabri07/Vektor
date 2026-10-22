"""B·6 — correcciones de la revisión del PR #158.

1. Borrar un archivo de CLIENTES sigue revirtiendo su ficha aunque después B·6
   le haya completado un campo desde otro archivo (en los dos órdenes).
3. Un email o teléfono que comparten dos fichas no alcanza para ESCRIBIR una:
   hace falta documento o código.
4. El historial de mapeos no re-sugiere un cruzado que el confirm rechazaría.
6/7. El apellido no se completa en una empresa, si ya está al final del nombre o
   si la fila nombra a otra persona.

La concurrencia (2) está en ``app/tests/integration/test_campos_cruzados_b6_pg.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services._ledger_restore import snapshot_master
from app.application.services.column_mapping_service import ColumnMappingService
from app.application.services.file_deletion_service import (
    record_import_ledger,
    revert_file_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
from app.persistence.models.column_mapping import TenantColumnMapping
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

_DNI = "30111222"


async def _archivo(session: AsyncSession, tenant: Tenant, nombre: str) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type="text/csv",
        size_bytes=10,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    session.add(record)
    await session.flush()
    return record


async def _clientes_y_despues_ventas(
    session: AsyncSession, tenant: Tenant
) -> tuple[Customer, UploadedFile, UploadedFile]:
    """C (hoja de clientes) le cambia el teléfono a una ficha existente; después
    A (hoja de ventas) le completa el apellido desde otra sección."""
    cliente = Customer(tenant_id=tenant.tenant_id, name="Cliente Uno", dni=_DNI, phone="111")
    session.add(cliente)
    await session.flush()
    await session.refresh(cliente)
    c, a = await _archivo(session, tenant, "clientes.csv"), await _archivo(
        session, tenant, "ventas.csv"
    )

    antes = snapshot_master(cliente, "customer")
    cliente.phone = "222"
    await session.flush()
    await session.refresh(cliente)
    await record_import_ledger(
        session,
        tenant_id=tenant.tenant_id,
        file_id=c.id,
        product_details=[],
        master_details=[
            {
                "action": "UPDATE_CUSTOMER",
                "before": antes,
                "after": snapshot_master(cliente, "customer"),
            }
        ],
    )

    cliente.last_name = "Pérez"
    # La escritura de B·6 mueve `updated_at`. En SQLite `now()` tiene grano de un
    # segundo y el test entero cabe en uno, así que se mueve a mano: es la señal
    # que hacía que borrar C viera una «edición posterior» que no lo era.
    cliente.updated_at = datetime.now(UTC) + timedelta(minutes=5)
    await session.flush()
    await record_import_ledger(
        session,
        tenant_id=tenant.tenant_id,
        file_id=a.id,
        product_details=[],
        cross_field_details=[
            {
                "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                "before": {"last_name": None},
                "after": {"last_name": "Pérez", "id": str(cliente.id), "kind": "customer"},
            }
        ],
    )
    await session.flush()
    return cliente, c, a


async def _borrar(session: AsyncSession, tenant: Tenant, archivo: UploadedFile) -> dict[str, Any]:
    contadores = await revert_file_data(session, archivo.id, tenant.tenant_id)
    archivo.deleted_at = datetime.now(UTC)
    await session.flush()
    return contadores


class TestBorrarClientesDespuesDeCompletarDesdeVentas:
    async def test_primero_clientes_vuelve_la_ficha_y_el_apellido_queda_de_ventas(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente, c, a = await _clientes_y_despues_ventas(db_session, sample_tenant)

        contadores = await _borrar(db_session, sample_tenant, c)
        await db_session.refresh(cliente)
        assert contadores["conservados"] == []
        assert cliente.phone == "111"
        # El apellido es del archivo de ventas, que sigue vivo.
        assert cliente.last_name == "Pérez"

        contadores = await _borrar(db_session, sample_tenant, a)
        await db_session.refresh(cliente)
        assert contadores["conservados"] == []
        assert cliente.last_name is None

    async def test_primero_ventas_y_despues_clientes_tambien_vuelve(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente, c, a = await _clientes_y_despues_ventas(db_session, sample_tenant)

        await _borrar(db_session, sample_tenant, a)
        # La reversa de B·6 también mueve el reloj (ver la nota del helper).
        cliente.updated_at = datetime.now(UTC) + timedelta(minutes=10)
        await db_session.flush()
        contadores = await _borrar(db_session, sample_tenant, c)

        await db_session.refresh(cliente)
        assert contadores["conservados"] == []
        assert (cliente.phone, cliente.last_name) == ("111", None)

    async def test_una_edicion_real_posterior_se_sigue_conservando(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente, c, _a = await _clientes_y_despues_ventas(db_session, sample_tenant)
        cliente.phone = "333"  # el usuario corrige el teléfono a mano
        await db_session.flush()

        contadores = await _borrar(db_session, sample_tenant, c)

        await db_session.refresh(cliente)
        assert cliente.phone == "333"
        assert [x["reasons"] for x in contadores["conservados"]] == [
            ["edicion_manual_posterior"]
        ]


def _ventas(*filas: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": list(filas),
    }


async def _importar(
    session: AsyncSession, tenant: Tenant, mapeo: dict[str, str], *filas: dict[str, Any]
) -> dict[str, Any]:
    return await importer.insert_confirmed_data(
        session,
        tenant.tenant_id,
        _ventas(*filas),
        {"ventas": True},
        column_mappings=mapeo,
        apply_cross_fields=True,
    )


class TestSoloDocumentoOCodigoEscribenLaFicha:
    async def test_telefono_compartido_por_dos_fichas_no_completa_ninguna(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        uno = Customer(tenant_id=sample_tenant.tenant_id, name="Ana", phone="1155550000")
        dos = Customer(tenant_id=sample_tenant.tenant_id, name="Beto", phone="1155550000")
        db_session.add_all([uno, dos])
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            {"tel": "customer_phone", "dom": "customer:address"},
            {"fecha": "2024-01-15", "monto": "100", "tel": "11 5555-0000", "dom": "Mitre 10"},
        )

        assert counts["ventas_cliente_identificado"] == 1  # vincula la venta
        assert counts["cross_fields_ficha_por_contacto"] == 1
        assert not counts.get("cross_fields_aplicados")
        await db_session.refresh(uno)
        await db_session.refresh(dos)
        assert uno.address is None and dos.address is None

    async def test_con_documento_si_completa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = Customer(
            tenant_id=sample_tenant.tenant_id, name="Ana", dni=_DNI, phone="1155550000"
        )
        db_session.add(cliente)
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            {"doc": "customer_dni", "tel": "customer_phone", "dom": "customer:address"},
            {"fecha": "2024-01-15", "monto": "100", "doc": _DNI, "tel": "1155550000",
             "dom": "Mitre 10"},
        )

        assert counts["cross_fields_aplicados"] == 1
        await db_session.refresh(cliente)
        assert cliente.address == "Mitre 10"


class TestApellido:
    @pytest.mark.parametrize(
        ("ficha", "fila_nombre"),
        [
            ({"name": "Pérez Hnos SA", "customer_type": "company"}, None),
            ({"name": "Juan Pérez"}, None),
            ({"name": "Juan"}, "Pedro"),
        ],
        ids=["empresa", "ya_en_el_nombre", "la_fila_nombra_a_otro"],
    )
    async def test_no_se_completa(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        ficha: dict[str, Any],
        fila_nombre: str | None,
    ) -> None:
        cliente = Customer(tenant_id=sample_tenant.tenant_id, dni=_DNI, **ficha)
        db_session.add(cliente)
        await db_session.flush()
        mapeo = {"doc": "customer_dni", "ape": "customer:last_name"}
        fila: dict[str, Any] = {"fecha": "2024-01-15", "monto": "100", "doc": _DNI, "ape": "Pérez"}
        if fila_nombre:
            mapeo["nom"] = "customer_name"
            fila["nom"] = fila_nombre

        counts = await _importar(db_session, sample_tenant, mapeo, fila)

        assert counts["cross_fields_apellido_omitido"] == 1
        await db_session.refresh(cliente)
        assert cliente.last_name is None

    async def test_mismo_nombre_en_la_fila_si_completa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = Customer(tenant_id=sample_tenant.tenant_id, name="Juan", dni=_DNI)
        db_session.add(cliente)
        await db_session.flush()

        await _importar(
            db_session,
            sample_tenant,
            {"doc": "customer_dni", "ape": "customer:last_name", "nom": "customer_name"},
            {"fecha": "2024-01-15", "monto": "100", "doc": _DNI, "ape": "Pérez", "nom": "JUAN"},
        )

        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez"


async def test_el_historial_no_resugiere_un_cruzado_que_el_confirm_rechaza() -> None:
    ahora = datetime.now(UTC)
    aprendidos = [
        TenantColumnMapping(
            id=uuid.uuid4(), tenant_id=uuid.uuid4(), entity_type="sale",
            source_column="localidad", target_field="supplier:payment_method",
            confirmed_count=5, last_seen_at=ahora, created_at=ahora,
        ),
        TenantColumnMapping(
            id=uuid.uuid4(), tenant_id=uuid.uuid4(), entity_type="sale",
            source_column="domicilio", target_field="customer:address",
            confirmed_count=5, last_seen_at=ahora, created_at=ahora,
        ),
    ]
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = aprendidos
    db.execute = AsyncMock(return_value=result)

    sugerencias = await ColumnMappingService(db).suggest_mappings(
        uuid.uuid4(), "sale", ["localidad", "domicilio"], [{"localidad": "X", "domicilio": "Y"}]
    )

    por_col = {s["source_column"]: s for s in sugerencias}
    assert por_col["localidad"]["target_field"] != "supplier:payment_method"
    assert por_col["domicilio"]["target_field"] == "customer:address"
    assert por_col["domicilio"]["source"] == "tenant_history"
