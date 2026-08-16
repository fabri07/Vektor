"""F-D (sub-commit 4, 7b/7c) — captura de campos cross-sección al buffer.

7a validó el mapeo (`app/tests/api/v1/test_ingestion_cross_target_validation_fd.py`)
y `cross_field_buffer.py` probó el desempate puro. Este módulo cablea ambos al
import service real: una fila de venta/gasto cuya referencia de cliente/
proveedor resuelve ``matched`` vuelca sus columnas ``customer:*``/``supplier:*``
al buffer compartido; una que NO resuelve (anonymous/unresolved) no vuelca
nada — nunca se escribe sobre el sentinela ni sobre una entidad ambigua.

La escritura real de los campos (7f) y su ledger (7e) esperan la migración,
así que lo único observable hoy es el conteo que llega a ``counts``
(``cross_fields_pendientes``/``cross_fields_entidades_pendientes``) — mismo
patrón que ya usaba ``targets_cruzados_descartados`` antes de este commit.
``product:*`` (expense→product) sigue deliberadamente sin capturar: está
acoplado al motor de costos de F-H6 y se declara fase propia
(``_CROSS_CAPTURE_KINDS`` en ``ingestion_import_service.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.config.settings import get_settings
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant


def _flat_sale_summary(*, second_row: bool = False) -> dict[str, Any]:
    rows = [
        {
            "fecha": "2024-01-15",
            "monto": "3000",
            "doc_cliente": "30111222",
            "apellido_cliente": "Pérez",
        }
    ]
    if second_row:
        rows.append(
            {
                "fecha": "2024-01-16",
                "monto": "1500",
                "doc_cliente": "30111222",
                "apellido_cliente": "Gómez",  # distinto: no debe ganar
            }
        )
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": rows,
    }


class TestCapturaFlatPathVentaCliente:
    async def test_cliente_matched_captura_el_campo_cruzado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        await repo.save(
            Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
        )

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _flat_sale_summary(),
            {"ventas": True},
            column_mappings={
                "doc_cliente": "customer_dni",
                "apellido_cliente": "customer:last_name",
            },
        )

        assert counts["ventas_cliente_identificado"] == 1
        assert counts["cross_fields_entidades_pendientes"] == 1
        assert counts["cross_fields_pendientes"] == 1
        assert counts["cross_fields_por_campo"] == {"last_name": 1}
        # No es "descartado": está capturado, sólo falta persistirse (7f).
        assert not counts.get("targets_cruzados_descartados")

    async def test_cliente_no_resuelto_no_captura_nada(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Sin cliente que matchee, la fila cae a "Local" — la ruta cross
        NUNCA escribe sobre el sentinela."""
        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _flat_sale_summary(),
            {"ventas": True},
            column_mappings={
                "doc_cliente": "customer_dni",
                "apellido_cliente": "customer:last_name",
            },
        )

        assert counts["ventas_cliente_no_resuelto"] == 1
        assert counts.get("cross_fields_pendientes", 0) == 0
        assert counts.get("cross_fields_entidades_pendientes", 0) == 0

    async def test_primera_fila_gana_entre_dos_ventas_del_mismo_cliente(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        await repo.save(
            Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
        )

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _flat_sale_summary(second_row=True),
            {"ventas": True},
            column_mappings={
                "doc_cliente": "customer_dni",
                "apellido_cliente": "customer:last_name",
            },
        )

        assert counts["ventas_cliente_identificado"] == 2
        # Dos filas, MISMO cliente, mismo campo → una sola entidad pendiente,
        # no dos escrituras (el invariante "una por entidad, no una por fila").
        assert counts["cross_fields_entidades_pendientes"] == 1
        assert counts["cross_fields_pendientes"] == 1


def _merch_purchase_summary(supplier_name: str, cuil: str) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "gasto": "5000",
                "producto": "Yerba",
                "cantidad": "10",
                "proveedor": supplier_name,
                "cuil_prov": cuil,
                "forma_pago_prov": "transferencia",
            }
        ],
    }


class TestCapturaFlatPathGastoProveedor:
    async def test_proveedor_matched_captura_el_campo_cruzado(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
        valid_cuil = "20-12345678-6"
        db_session.add(
            Supplier(
                tenant_id=sample_tenant.tenant_id, name="Distribuidora Real SA", cuil=valid_cuil
            )
        )
        await db_session.flush()

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _merch_purchase_summary("Distribuidora Real", valid_cuil),
            {"gastos": True},
            column_mappings={
                "cuil_prov": "supplier_cuil",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert counts["compras_proveedor_identificado"] == 1
        assert counts["cross_fields_entidades_pendientes"] == 1
        assert counts["cross_fields_pendientes"] == 1

    async def test_producto_cruzado_sigue_descartado_no_capturado(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`product:*` (expense→product) NO se captura todavía — acoplado al
        motor de costos de F-H6, fase propia (ver `_CROSS_CAPTURE_KINDS`)."""
        monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
        valid_cuil = "20-12345678-6"
        db_session.add(
            Supplier(
                tenant_id=sample_tenant.tenant_id, name="Distribuidora Real SA", cuil=valid_cuil
            )
        )
        await db_session.flush()

        summary = _merch_purchase_summary("Distribuidora Real", valid_cuil)
        summary["gastos_detectados"][0]["categoria_prod"] = "Almacén"
        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            summary,
            {"gastos": True},
            column_mappings={
                "cuil_prov": "supplier_cuil",
                "categoria_prod": "product:category",
            },
        )

        assert counts.get("targets_cruzados_descartados", 0) == 1
        assert counts.get("cross_fields_pendientes", 0) == 0


def _multisheet_sale_with_customer_summary(*, dni: str) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "label": "Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["Fecha", "Monto", "Documento", "Apellido"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "ventas_detectadas": [
            {
                "Fecha": "2024-01-15",
                "Monto": "3000",
                "Documento": dni,
                "Apellido": "Pérez",
                "__context__": "sheet:Ventas",
            },
        ],
        "gastos_detectados": [],
        "clientes_detectados": [],
        "proveedores_detectados": [],
        "stock_detectado": [],
    }


class TestCapturaMultiHojaVentaCliente:
    async def test_cliente_matched_captura_el_campo_cruzado_multi_hoja(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.persistence.repositories.customer_repository import CustomerRepository

        repo = CustomerRepository(db_session)
        await repo.save(
            Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", dni="30111222")
        )

        counts = await importer.insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _multisheet_sale_with_customer_summary(dni="30111222"),
            {"ventas": True},
            context_mappings={
                "sheet:Ventas": {
                    "Fecha": "transaction_date",
                    "Monto": "amount",
                    "Documento": "customer_dni",
                    "Apellido": "customer:last_name",
                },
            },
            context_confirmed={"sheet:Ventas": True},
        )

        assert counts["ventas_cliente_identificado"] == 1
        assert counts["cross_fields_entidades_pendientes"] == 1
        assert counts["cross_fields_pendientes"] == 1
