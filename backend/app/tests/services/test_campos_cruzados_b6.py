"""B·6 (F-D) — venta → cliente y compra → proveedor, contra el importador real.

Una columna de una hoja de ventas mapeada a ``customer:*`` (o de compras a
``supplier:*``) completa la ficha que la fila identificó por clave fuerte:

- sólo una ficha ``matched`` — nunca el centinela, nunca una ficha que el mismo
  archivo creó o actualizó desde su hoja de clientes/proveedores;
- la primera fila del archivo con valor gana, y la ficha se escribe una vez;
- sólo un campo vacío se completa; uno con dato no se pisa, con
  ``has_user_edits`` o sin él;
- cada campo escrito deja su ítem de ledger, y borrar el archivo lo devuelve a
  como estaba salvo que alguien lo haya cambiado después.

El rechazo del confirm (422) y el camino HTTP completo están en
``app/tests/api/v1/test_campos_cruzados_b6_api.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.file_deletion_service import (
    preview_file_deletion,
    record_import_ledger,
    revert_file_data,
)
from app.config.settings import get_settings
from app.domain.ingestion_version import INGESTION_VERSION
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

pytestmark = pytest.mark.asyncio

_DNI = "30111222"
_CUIL = "20-12345678-6"


async def _cliente(session: AsyncSession, tenant: Tenant, **campos: Any) -> Customer:
    c = Customer(tenant_id=tenant.tenant_id, name="Cliente Uno", dni=_DNI, **campos)
    session.add(c)
    await session.flush()
    return c


def _ventas_planas(*filas: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": list(filas),
    }


def _fila_venta(**extra: Any) -> dict[str, Any]:
    return {"fecha": "2024-01-15", "monto": "3000", "doc": _DNI, **extra}


_MAPEO_VENTA = {"doc": "customer_dni", "apellido": "customer:last_name"}


async def _importar(
    session: AsyncSession, tenant: Tenant, summary: dict[str, Any], **kw: Any
) -> dict[str, Any]:
    kw.setdefault("apply_cross_fields", True)
    confirmed = kw.pop("confirmed", {"ventas": True})
    return await importer.insert_confirmed_data(
        session, tenant.tenant_id, summary, confirmed, **kw
    )


# ── Captura y escritura ───────────────────────────────────────────────────────


class TestVentaCompletaLaFichaDelCliente:
    async def test_campo_vacio_se_completa_y_queda_el_detalle_para_el_ledger(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant)

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(_fila_venta(apellido="Pérez")),
            column_mappings=_MAPEO_VENTA,
        )

        assert counts["cross_fields_aplicados"] == 1
        assert counts["cross_fields_fichas"] == 1
        assert not counts.get("targets_cruzados_descartados")
        [detalle] = counts["cross_field_details"]
        assert detalle["action"] == "UPDATE_CUSTOMER_CROSS_FIELD"
        assert detalle["before"] == {"last_name": None}
        assert detalle["after"]["last_name"] == "Pérez"
        assert detalle["after"]["id"] == str(cliente.id)
        assert detalle["after"]["kind"] == "customer"
        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez"

    async def test_la_primera_fila_del_archivo_gana_y_se_escribe_una_vez(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant)

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(
                _fila_venta(apellido="Pérez"),
                _fila_venta(apellido="Gómez", fecha="2024-01-16"),
            ),
            column_mappings=_MAPEO_VENTA,
        )

        assert counts["ventas_cliente_identificado"] == 2
        assert counts["cross_fields_aplicados"] == 1
        assert len(counts["cross_field_details"]) == 1
        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez"

    async def test_un_campo_con_dato_no_se_pisa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, last_name="Gómez")

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(_fila_venta(apellido="Pérez")),
            column_mappings=_MAPEO_VENTA,
        )

        assert not counts.get("cross_fields_aplicados")
        assert counts["cross_fields_ya_tenian_dato"] == 1
        assert "cross_field_details" not in counts
        await db_session.refresh(cliente)
        assert cliente.last_name == "Gómez"

    async def test_ficha_editada_a_mano_no_pierde_lo_cargado_y_completa_lo_vacio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """``has_user_edits``: la ficha que el usuario tocó no pierde nada de lo
        que tiene, y lo que nunca cargó se completa — la misma política aditiva
        que ``_solo_completar`` en el import de clientes."""
        cliente = await _cliente(
            db_session, sample_tenant, last_name="Gómez", has_user_edits=True
        )

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(_fila_venta(apellido="Pérez", domicilio="Mitre 10")),
            column_mappings={**_MAPEO_VENTA, "domicilio": "customer:address"},
        )

        await db_session.refresh(cliente)
        assert cliente.last_name == "Gómez"
        assert cliente.address == "Mitre 10"
        assert counts["cross_fields_ya_tenian_dato"] == 1
        assert counts["cross_fields_aplicados"] == 1

    async def test_cliente_no_identificado_no_completa_nada_y_se_avisa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Sin ficha que matchee, la venta va al centinela «Local» y el
        centinela NUNCA recibe los datos."""
        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(_fila_venta(apellido="Pérez")),
            column_mappings=_MAPEO_VENTA,
        )

        assert counts["ventas_cliente_no_resuelto"] == 1
        assert counts["cross_fields_filas_sin_ficha"] == 1
        assert not counts.get("cross_fields_aplicados")
        locales = (
            await db_session.execute(
                select(Customer).where(Customer.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars().all()
        assert all(c.last_name is None for c in locales)

    async def test_condicion_de_iva_se_normaliza_y_una_invalida_no_se_guarda(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant)

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(
                _fila_venta(iva="Cualquiera"),
                _fila_venta(iva="Responsable Inscripto", fecha="2024-01-16"),
            ),
            column_mappings={"doc": "customer_dni", "iva": "customer:iva_condition"},
        )

        assert counts["cross_fields_invalidos"] == 1
        await db_session.refresh(cliente)
        assert cliente.iva_condition == "responsable_inscripto"

    async def test_sin_pedirlo_no_se_completa_y_se_informa_como_no_importada(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La relectura, el chat y la reparación no registran el ledger por
        campo: no completan fichas, y la columna se cuenta como no importada."""
        cliente = await _cliente(db_session, sample_tenant)

        counts = await _importar(
            db_session,
            sample_tenant,
            _ventas_planas(_fila_venta(apellido="Pérez")),
            column_mappings=_MAPEO_VENTA,
            apply_cross_fields=False,
        )

        assert counts["targets_cruzados_descartados"] == 1
        assert "cross_field_details" not in counts
        await db_session.refresh(cliente)
        assert cliente.last_name is None


def _multihoja_clientes_y_ventas() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
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
                "row_count": 1,
            },
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
        "clientes_detectados": [
            {"Nombre": "Cliente Nuevo", "DNI": "30999888", "__context__": "sheet:Clientes"}
        ],
        "ventas_detectadas": [
            {
                "Fecha": "2024-01-15",
                "Monto": "3000",
                "Documento": "30999888",
                "Apellido": "Pérez",
                "__context__": "sheet:Ventas",
            }
        ],
        "gastos_detectados": [],
        "proveedores_detectados": [],
        "stock_detectado": [],
    }


class TestMultiHoja:
    async def test_cliente_identificado_en_la_hoja_de_ventas_se_completa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant)
        summary = _multihoja_clientes_y_ventas()
        summary["mapping_contexts"] = summary["mapping_contexts"][1:]
        summary["clientes_detectados"] = []
        summary["ventas_detectadas"][0]["Documento"] = _DNI

        counts = await _importar(
            db_session,
            sample_tenant,
            summary,
            context_mappings={
                "sheet:Ventas": {
                    "Fecha": "transaction_date",
                    "Monto": "amount",
                    "Documento": "customer_dni",
                    "Apellido": "customer:last_name",
                }
            },
            context_confirmed={"sheet:Ventas": True},
        )

        assert counts["cross_fields_aplicados"] == 1
        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez"

    async def test_ficha_creada_por_la_hoja_de_clientes_del_mismo_archivo_no_se_toca(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La hoja de clientes crea la ficha y la de ventas la identifica: la
        ficha ya tiene su ítem de «creada por este archivo», y la venta no le
        agrega un segundo camino de escritura."""
        counts = await _importar(
            db_session,
            sample_tenant,
            _multihoja_clientes_y_ventas(),
            confirmed={"ventas": True, "clientes": True},
            context_mappings={
                "sheet:Clientes": {"Nombre": "name", "DNI": "dni"},
                "sheet:Ventas": {
                    "Fecha": "transaction_date",
                    "Monto": "amount",
                    "Documento": "customer_dni",
                    "Apellido": "customer:last_name",
                },
            },
            context_confirmed={"sheet:Clientes": True, "sheet:Ventas": True},
        )

        assert counts["clientes_creados"] == 1
        assert counts["ventas_cliente_identificado"] == 1
        assert counts["cross_fields_ficha_del_archivo"] == 1
        assert not counts.get("cross_fields_aplicados")
        nuevo = (
            await db_session.execute(
                select(Customer).where(
                    Customer.tenant_id == sample_tenant.tenant_id,
                    Customer.dni == "30999888",
                )
            )
        ).scalar_one()
        assert nuevo.last_name is None


def _compra(*, proveedor: str, **extra: Any) -> dict[str, Any]:
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
                "proveedor": proveedor,
                "forma_pago_prov": "transferencia",
                **extra,
            }
        ],
    }


class TestCompraCompletaLaFichaDelProveedor:
    async def test_link_only_proveedor_identificado_por_cuil(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA", cuil=_CUIL)
        db_session.add(prov)
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="Distribuidora", cuil_prov=_CUIL),
            confirmed={"gastos": True},
            column_mappings={
                "cuil_prov": "supplier_cuil",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert counts["cross_fields_aplicados"] == 1
        [detalle] = counts["cross_field_details"]
        assert detalle["action"] == "UPDATE_SUPPLIER_CROSS_FIELD"
        await db_session.refresh(prov)
        assert prov.payment_method == "transferencia"

    async def test_legacy_nombre_y_cuil_del_mismo_proveedor_completan(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """En `legacy` (el default) el proveedor sale del nombre; su ficha se
        completa porque además el CUIL de la fila identifica al MISMO."""
        assert get_settings().SUPPLIER_REFERENCE_CREATION_MODE == "legacy"
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora", cuil=_CUIL)
        db_session.add(prov)
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="Distribuidora", cuil_prov=_CUIL),
            confirmed={"gastos": True},
            column_mappings={
                "proveedor": "supplier_name",
                "cuil_prov": "supplier_cuil",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert counts["cross_fields_aplicados"] == 1
        await db_session.refresh(prov)
        assert prov.payment_method == "transferencia"

    async def test_legacy_solo_por_nombre_no_completa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El nombre es una señal débil: sin una clave fuerte que confirme la
        ficha, no se completa nada (y se avisa)."""
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora")
        db_session.add(prov)
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="Distribuidora"),
            confirmed={"gastos": True},
            column_mappings={
                "proveedor": "supplier_name",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert not counts.get("cross_fields_aplicados")
        assert counts["cross_fields_filas_sin_ficha"] == 1
        await db_session.refresh(prov)
        assert prov.payment_method is None

    async def test_legacy_proveedor_creado_por_nombre_en_este_import_no_se_completa(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El proveedor que este mismo import creó por nombre no recibe datos de
        costado, aunque una clave fuerte de la fila apunte a otro existente."""
        otro = Supplier(tenant_id=sample_tenant.tenant_id, name="Otro SA", cuil=_CUIL)
        db_session.add(otro)
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="Proveedor Nuevo", cuil_prov=_CUIL),
            confirmed={"gastos": True},
            column_mappings={
                "proveedor": "supplier_name",
                "cuil_prov": "supplier_cuil",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert counts["proveedores_creados_ids"]
        assert not counts.get("cross_fields_aplicados")
        for s in (
            await db_session.execute(
                select(Supplier).where(Supplier.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars():
            assert s.payment_method is None

    async def test_legacy_nombre_y_cuil_de_proveedores_distintos_no_completan(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El nombre dice X y el CUIL dice Y: la fila no identifica una ficha
        sin ambigüedad, así que no se completa ninguna de las dos."""
        por_nombre = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora")
        por_cuil = Supplier(tenant_id=sample_tenant.tenant_id, name="Otro SA", cuil=_CUIL)
        db_session.add_all([por_nombre, por_cuil])
        await db_session.flush()

        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="Distribuidora", cuil_prov=_CUIL),
            confirmed={"gastos": True},
            column_mappings={
                "proveedor": "supplier_name",
                "cuil_prov": "supplier_cuil",
                "forma_pago_prov": "supplier:payment_method",
            },
        )

        assert not counts.get("proveedores_creados_ids")
        assert not counts.get("cross_fields_aplicados")
        assert counts["cross_fields_filas_sin_ficha"] == 1
        await db_session.refresh(por_nombre)
        await db_session.refresh(por_cuil)
        assert por_nombre.payment_method is None
        assert por_cuil.payment_method is None

    async def test_producto_cruzado_sigue_sin_escribirse(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`product:*` queda afuera de B·6 (motor de costo). Por el importador
        se informa como no importado; el confirm lo rechaza con 422."""
        counts = await _importar(
            db_session,
            sample_tenant,
            _compra(proveedor="X", categoria_prod="Almacén"),
            confirmed={"gastos": True},
            column_mappings={"categoria_prod": "product:category"},
        )

        assert counts["targets_cruzados_descartados"] == 1


# ── Ledger y reversa ──────────────────────────────────────────────────────────


async def _archivo(session: AsyncSession, tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
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
    session.add(record)
    await session.flush()
    return record


async def _importar_y_registrar(
    session: AsyncSession,
    tenant: Tenant,
    archivo: UploadedFile,
    *filas: dict[str, Any],
    mapeo: dict[str, str],
) -> dict[str, Any]:
    """Lo que hace el confirm: importar con el buffer y grabar el ledger."""
    counts = await _importar(
        session,
        tenant,
        _ventas_planas(*filas),
        column_mappings=mapeo,
        uploaded_file_id=archivo.id,
    )
    await record_import_ledger(
        session,
        tenant_id=tenant.tenant_id,
        file_id=archivo.id,
        product_details=[],
        cross_field_details=counts.pop("cross_field_details", []),
    )
    await session.flush()
    return counts


_MAPEO_DOS = {**_MAPEO_VENTA, "domicilio": "customer:address"}


class TestReversa:
    async def test_un_item_de_ledger_por_campo_con_las_acciones_nuevas(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        await _cliente(db_session, sample_tenant)

        await _importar_y_registrar(
            db_session,
            sample_tenant,
            archivo,
            _fila_venta(apellido="Pérez", domicilio="Mitre 10"),
            mapeo=_MAPEO_DOS,
        )

        items = (
            await db_session.execute(
                select(DataRepairItem).where(DataRepairItem.source_file_id == archivo.id)
            )
        ).scalars().all()
        assert sorted((i.action, tuple(i.before_json or {})) for i in items) == [
            ("UPDATE_CUSTOMER_CROSS_FIELD", ("address",)),
            ("UPDATE_CUSTOMER_CROSS_FIELD", ("last_name",)),
        ]

    async def test_borrar_el_archivo_deja_el_campo_como_estaba(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant)
        await _importar_y_registrar(
            db_session, sample_tenant, archivo, _fila_venta(apellido="Pérez"), mapeo=_MAPEO_VENTA
        )
        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez"

        preview = await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        assert preview["campos_cruzados_a_restaurar"] == 1

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name is None
        assert contadores["campos_cruzados_restaurados"] == 1
        assert contadores["conservados"] == []

    async def test_se_revierte_aunque_el_cliente_tenga_ventas_de_otra_fuente(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La razón de las acciones propias: con `UPDATE_CUSTOMER` el cliente con
        ventas de otra fuente se conservaba ENTERO y el campo no volvía."""
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant)
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                customer_id=cliente.id,
                amount=Decimal("500"),
                quantity=1,
                transaction_date=date(2024, 2, 1),
                payment_method="cash",
            )
        )
        await _importar_y_registrar(
            db_session, sample_tenant, archivo, _fila_venta(apellido="Pérez"), mapeo=_MAPEO_VENTA
        )

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name is None
        assert contadores["campos_cruzados_restaurados"] == 1

    async def test_el_campo_cambiado_despues_se_conserva_y_el_otro_vuelve(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        cliente = await _cliente(db_session, sample_tenant)
        await _importar_y_registrar(
            db_session,
            sample_tenant,
            archivo,
            _fila_venta(apellido="Pérez", domicilio="Mitre 10"),
            mapeo=_MAPEO_DOS,
        )
        # El usuario corrige SÓLO el apellido, después del import.
        cliente.last_name = "Pérez Corregido"
        cliente.has_user_edits = True
        await db_session.flush()

        preview = await preview_file_deletion(db_session, archivo.id, sample_tenant.tenant_id)
        assert preview["campos_cruzados_a_restaurar"] == 1
        assert [c["fields"] for c in preview["conservados"]] == [["last_name"]]

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(cliente)
        assert cliente.last_name == "Pérez Corregido"
        assert cliente.address is None
        assert cliente.name == "Cliente Uno"
        assert contadores["campos_cruzados_restaurados"] == 1
        [conservado] = contadores["conservados"]
        assert conservado["reasons"] == ["campo_modificado_posteriormente"]
        assert conservado["fields"] == ["last_name"]
        assert conservado["id"] == str(cliente.id)

    async def test_un_ledger_sobre_el_centinela_no_lo_toca(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        from app.application.services.customer_sentinel import (
            resolve_or_create_local_sentinel,
        )

        archivo = await _archivo(db_session, sample_tenant)
        centinela_id = await resolve_or_create_local_sentinel(
            db_session, sample_tenant.tenant_id
        )
        centinela = await db_session.get(Customer, centinela_id)
        assert centinela is not None
        centinela.last_name = "No tocar"
        await db_session.flush()
        await record_import_ledger(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=archivo.id,
            product_details=[],
            cross_field_details=[
                {
                    "action": "UPDATE_CUSTOMER_CROSS_FIELD",
                    "before": {"last_name": None},
                    "after": {"last_name": "No tocar", "id": str(centinela_id), "kind": "customer"},
                }
            ],
        )

        contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)

        await db_session.refresh(centinela)
        assert centinela.last_name == "No tocar"
        assert contadores["campos_cruzados_restaurados"] == 0
