"""B·7 — cliente o proveedor por su código, contra el importador real.

`customer_external_code` en una venta y `supplier_external_code` en una compra:

- el código va primero: identifica a la ficha aunque la fila no traiga otra clave;
- un código que ninguna ficha tiene NO vincula —aunque el DNI de la fila sí
  matchee— ni crea un maestro, y se cuenta para avisar;
- un código que apunta a una ficha y otra clave a otra es conflicto: no vincula;
- en el modo proveedor `legacy` el código va antes de crear por nombre: un código
  desconocido o en conflicto no crea un proveedor;
- el sistema del código es siempre «propio» (`external_source=None`): el
  `external_source` de la venta es el de la operación (E6b).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.config.settings import get_settings
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

pytestmark = pytest.mark.asyncio

_DNI = "30111222"
_CUIL = "20-12345678-6"


async def _cliente(session: AsyncSession, tenant: Tenant, **campos: Any) -> Customer:
    c = Customer(tenant_id=tenant.tenant_id, **{"name": "Cliente Uno", **campos})
    session.add(c)
    await session.flush()
    return c


def _ventas(**fila: Any) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "3000", **fila}],
    }


async def _importar_venta(
    session: AsyncSession, tenant: Tenant, mapeo: dict[str, str], **fila: Any
) -> tuple[dict[str, Any], SaleEntry]:
    counts = await importer.insert_confirmed_data(
        session, tenant.tenant_id, _ventas(**fila), {"ventas": True}, column_mappings=mapeo
    )
    await session.flush()
    venta = (
        await session.execute(select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id))
    ).scalar_one()
    return counts, venta


class TestVentaPorCodigoDeCliente:
    async def test_el_codigo_solo_identifica_al_cliente(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, external_code="C-0042")

        counts, venta = await _importar_venta(
            db_session, sample_tenant, {"cod": "customer_external_code"}, cod="c-0042"
        )

        assert counts["ventas_cliente_identificado"] == 1
        assert venta.customer_id == cliente.id

    async def test_codigo_desconocido_no_vincula_aunque_el_dni_matchee(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, dni=_DNI, external_code="C-1")

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "doc": "customer_dni"},
            cod="C-999",
            doc=_DNI,
        )

        assert counts["ventas_cliente_codigo_desconocido"] == 1
        assert counts["ventas_cliente_no_resuelto"] == 1
        assert not counts.get("ventas_cliente_identificado")
        assert venta.customer_id != cliente.id
        assert (venta.custom_fields or {}).get("_customer_reference_raw") is not None
        # Nunca se crea un cliente desde la venta: sólo el de siempre + «Local».
        total = (
            await db_session.execute(
                select(func.count()).select_from(Customer).where(
                    Customer.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalar_one()
        assert total == 2

    async def test_codigo_y_dni_de_clientes_distintos_es_conflicto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        por_codigo = await _cliente(db_session, sample_tenant, external_code="C-1")
        por_dni = await _cliente(db_session, sample_tenant, name="Otro", dni=_DNI)

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "doc": "customer_dni"},
            cod="C-1",
            doc=_DNI,
        )

        assert counts["ventas_cliente_codigo_en_conflicto"] == 1
        assert venta.customer_id not in (por_codigo.id, por_dni.id)

    async def test_codigo_y_dni_del_mismo_cliente_vinculan(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, dni=_DNI, external_code="C-1")

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "doc": "customer_dni"},
            cod="C-1",
            doc=_DNI,
        )

        assert venta.customer_id == cliente.id
        assert not counts.get("ventas_cliente_codigo_desconocido")
        assert not counts.get("ventas_cliente_codigo_en_conflicto")

    async def test_celda_de_codigo_vacia_resuelve_por_las_otras_claves(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, dni=_DNI)

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "doc": "customer_dni"},
            cod="",
            doc=_DNI,
        )

        assert venta.customer_id == cliente.id
        assert not counts.get("ventas_cliente_codigo_desconocido")

    async def test_el_sistema_de_la_operacion_no_es_el_del_codigo_del_cliente(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`external_source` de la venta es el sistema de la OPERACIÓN: el código
        del cliente se compara como código propio del negocio."""
        cliente = await _cliente(db_session, sample_tenant, external_code="C-1")

        _, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "sis": "external_source"},
            cod="C-1",
            sis="Tango",
        )

        assert venta.customer_id == cliente.id


def _compra(**fila: Any) -> dict[str, Any]:
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
                **fila,
            }
        ],
    }


async def _importar_compra(
    session: AsyncSession, tenant: Tenant, mapeo: dict[str, str], **fila: Any
) -> tuple[dict[str, Any], ExpenseEntry]:
    counts = await importer.insert_confirmed_data(
        session, tenant.tenant_id, _compra(**fila), {"gastos": True}, column_mappings=mapeo
    )
    await session.flush()
    gasto = (
        await session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
        )
    ).scalar_one()
    return counts, gasto


async def _proveedores(session: AsyncSession, tenant: Tenant) -> list[Supplier]:
    return list(
        (
            await session.execute(select(Supplier).where(Supplier.tenant_id == tenant.tenant_id))
        ).scalars()
    )


_MAPEO_COMPRA = {"cod": "supplier_external_code", "proveedor": "supplier_name"}


class TestCompraPorCodigoLegacy:
    """`legacy` es el modo por defecto: sin código, el proveedor se crea por nombre."""

    async def test_el_codigo_identifica_al_proveedor_aunque_el_nombre_difiera(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        assert get_settings().SUPPLIER_REFERENCE_CREATION_MODE == "legacy"
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distri SA", external_code="P-7")
        db_session.add(prov)
        await db_session.flush()

        counts, gasto = await _importar_compra(
            db_session, sample_tenant, _MAPEO_COMPRA, cod="P-7", proveedor="Distribuidora"
        )

        assert gasto.supplier_id == prov.id
        assert counts["compras_proveedor_identificado"] == 1
        assert not counts.get("proveedores_creados_ids")
        assert [s.name for s in await _proveedores(db_session, sample_tenant)] == ["Distri SA"]

    async def test_codigo_desconocido_no_crea_por_nombre(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts, gasto = await _importar_compra(
            db_session, sample_tenant, _MAPEO_COMPRA, cod="P-999", proveedor="Distribuidora"
        )

        assert counts["compras_proveedor_codigo_desconocido"] == 1
        assert not counts.get("proveedores_creados_ids")
        nombres = {s.name for s in await _proveedores(db_session, sample_tenant)}
        assert "Distribuidora" not in nombres
        # Compra de mercadería sin proveedor identificado → «No identificado».
        assert counts["sin_proveedor"] == 1
        assert (gasto.custom_fields or {}).get("_supplier_resolution") == "unresolved"

    async def test_codigo_en_conflicto_no_vincula_ni_crea(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        por_codigo = Supplier(tenant_id=sample_tenant.tenant_id, name="A", external_code="P-7")
        por_cuil = Supplier(tenant_id=sample_tenant.tenant_id, name="B", cuil=_CUIL)
        db_session.add_all([por_codigo, por_cuil])
        await db_session.flush()

        counts, gasto = await _importar_compra(
            db_session,
            sample_tenant,
            {**_MAPEO_COMPRA, "cuil": "supplier_cuil"},
            cod="P-7",
            proveedor="Distribuidora",
            cuil=_CUIL,
        )

        assert counts["compras_proveedor_codigo_en_conflicto"] == 1
        assert gasto.supplier_id not in (por_codigo.id, por_cuil.id)
        assert not counts.get("proveedores_creados_ids")

    async def test_sin_codigo_en_la_celda_sigue_creando_por_nombre(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts, gasto = await _importar_compra(
            db_session, sample_tenant, _MAPEO_COMPRA, cod=None, proveedor="Distribuidora"
        )

        assert counts["proveedores_creados_ids"]
        assert {s.name for s in await _proveedores(db_session, sample_tenant)} >= {
            "Distribuidora"
        }
        assert gasto.supplier_name == "Distribuidora"


class TestCompraPorCodigoLinkOnly:
    async def test_el_codigo_solo_identifica_y_desconocido_no_vincula(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")
        prov = Supplier(
            tenant_id=sample_tenant.tenant_id, name="Distri", external_code="P-7", cuil=_CUIL
        )
        db_session.add(prov)
        await db_session.flush()

        counts, gasto = await _importar_compra(
            db_session,
            sample_tenant,
            {"cod": "supplier_external_code", "cuil": "supplier_cuil"},
            cod="P-8",
            cuil=_CUIL,
        )

        assert counts["compras_proveedor_codigo_desconocido"] == 1
        assert gasto.supplier_id != prov.id
