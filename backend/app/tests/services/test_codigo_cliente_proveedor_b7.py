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
from app.application.services.identity_resolution import IdentityKey
from app.config.settings import get_settings
from app.domain.external_code import clave_de_codigo_externo
from app.domain.purchase_cost_decision import PurchaseCostDecision
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
        assert counts["compras_proveedor_no_resuelto"] == 1
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


# ── Revisión de B·7 ──────────────────────────────────────────────────────────


class TestRevisionClasificacion:
    async def test_codigo_cargado_con_sistema_vincula_desde_la_venta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Una ficha cuyo código vino de Tango: la venta compara como código
        propio y antes no la encontraba nunca (y avisaba «desconocido»)."""
        cliente = await _cliente(
            db_session, sample_tenant, dni=_DNI, external_code="C-1", external_source="Tango"
        )

        counts, venta = await _importar_venta(
            db_session, sample_tenant, {"cod": "customer_external_code"}, cod="C-1"
        )

        assert venta.customer_id == cliente.id
        assert counts["ventas_cliente_identificado"] == 1
        assert not counts.get("ventas_cliente_codigo_desconocido")

    async def test_el_codigo_propio_gana_sobre_el_de_otro_sistema(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        propio = await _cliente(db_session, sample_tenant, external_code="C-1")
        await _cliente(
            db_session, sample_tenant, name="De Tango", external_code="C-1",
            external_source="Tango",
        )

        _, venta = await _importar_venta(
            db_session, sample_tenant, {"cod": "customer_external_code"}, cod="C-1"
        )

        assert venta.customer_id == propio.id

    async def test_codigo_en_dos_sistemas_es_conflicto_y_no_desconocido(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        a = await _cliente(
            db_session, sample_tenant, external_code="C-1", external_source="Tango"
        )
        b = await _cliente(
            db_session, sample_tenant, name="Otro", external_code="C-1",
            external_source="Bejerman",
        )

        counts, venta = await _importar_venta(
            db_session, sample_tenant, {"cod": "customer_external_code"}, cod="C-1"
        )

        assert counts["ventas_cliente_codigo_en_varias_fichas"] == 1
        assert not counts.get("ventas_cliente_codigo_desconocido")
        assert counts["ventas_cliente_no_resuelto"] == 1
        assert venta.customer_id not in (a.id, b.id)

    async def test_la_traza_muestra_el_codigo_que_fallo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _cliente(db_session, sample_tenant, dni=_DNI)

        _, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "doc": "customer_dni", "nom": "customer_name"},
            cod="C-999",
            doc=_DNI,
            nom="Juan Pérez",
        )

        assert (venta.custom_fields or {}).get("_customer_reference_raw") == "C-999"

    async def test_el_codigo_va_antes_que_el_nombre_anonimo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        cliente = await _cliente(db_session, sample_tenant, external_code="C-1")

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "nom": "customer_name"},
            cod="C-1",
            nom="Consumidor final",
        )

        assert venta.customer_id == cliente.id
        assert counts["ventas_cliente_identificado"] == 1

    async def test_sin_codigo_el_nombre_anonimo_sigue_siendo_anonimo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _cliente(db_session, sample_tenant, external_code="C-1")

        counts, _ = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "nom": "customer_name"},
            cod="",
            nom="Consumidor final",
        )

        assert counts["ventas_cliente_anonimo"] == 1

    async def test_el_email_de_otra_ficha_no_desmiente_al_codigo(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        a = await _cliente(db_session, sample_tenant, external_code="C-1")
        await _cliente(db_session, sample_tenant, name="Otro", email="compartido@x.com")

        counts, venta = await _importar_venta(
            db_session,
            sample_tenant,
            {"cod": "customer_external_code", "mail": "customer_email"},
            cod="C-1",
            mail="compartido@x.com",
        )

        assert venta.customer_id == a.id
        assert not counts.get("ventas_cliente_codigo_en_conflicto")


async def test_codigo_a_ficha_a_con_email_compartido_indexado_como_b_vincula_a_a() -> None:
    """El email lo comparten A y B y el índice se quedó con B (gana el primero):
    no es conflicto contra el código, que dice A."""
    a, b = object(), object()
    clave = clave_de_codigo_externo("C-1")
    assert clave is not None
    indice = {
        IdentityKey("code", clave): a,
        IdentityKey("email", "compartido@x.com"): b,
    }

    ref = importer._classify_row_reference(
        {"external_code": "C-1", "email": "compartido@x.com"},
        doc_fields=("cuit", "dni"),
        existing_index=indice,
    )

    assert ref.outcome == "matched"
    assert ref.entity is a
    assert ref.matched_key_type == "code"


# ── Compras por hoja (multihoja) y por tabla suelta, con envío ───────────────

_CTX = "sheet:Compras"
_COLS_COMPRA = {
    "fecha": "expense_date",
    "nro": "invoice_number",
    "cod": "supplier_external_code",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
}


def _filas_compra(cod: str) -> list[dict[str, Any]]:
    return [
        {"fecha": "2024-03-05", "nro": "0001-00000123", "cod": cod,
         "proveedor": "Distribuidora", "articulo": "Vela", "cantidad": "2",
         "total": "6000", "envio": "2000", "__context__": _CTX},
        {"fecha": "2024-03-05", "nro": "0001-00000123", "cod": cod,
         "proveedor": "Distribuidora", "articulo": "Difusor", "cantidad": "3",
         "total": "3600", "envio": "2000", "__context__": _CTX},
    ]


async def _importar_compras_por_hoja(
    session: AsyncSession,
    tenant: Tenant,
    cod: str,
    *,
    multi: bool,
    con_envio: bool = True,
) -> dict[str, Any]:
    cols = dict(_COLS_COMPRA)
    filas = _filas_compra(cod)
    if not con_envio:
        cols.pop("envio")
        for f in filas:
            f.pop("envio")
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": multi,
        "row_count": len(filas),
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": list(cols),
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": filas,
    }
    counts = await importer.insert_confirmed_data(
        session,
        tenant.tenant_id,
        summary,
        {},
        context_mappings={_CTX: cols},
        context_entity={_CTX: "expense"},
        context_confirmed={_CTX: True},
        shipping_decisions={_CTX: "una_por_hoja"},
        purchase_cost_decisions={_CTX: PurchaseCostDecision(context_id=_CTX)},
    )
    await session.flush()
    return counts


async def _gastos(
    session: AsyncSession, tenant: Tenant, *, logistica: bool
) -> list[ExpenseEntry]:
    q = select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
    q = q.where(
        ExpenseEntry.category == "LOGISTICS"
        if logistica
        else ExpenseEntry.category != "LOGISTICS"
    )
    return list((await session.execute(q)).scalars())


@pytest.mark.parametrize("multi", [False, True], ids=["tabla", "multihoja"])
class TestCompraPorHojaConCodigo:
    """`legacy` (el modo por defecto) por los dos caminos del importador."""

    async def test_codigo_que_resuelve_vincula_la_compra_y_el_envio_sin_duplicar(
        self, db_session: AsyncSession, sample_tenant: Tenant, multi: bool
    ) -> None:
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distri SA", external_code="P-7")
        db_session.add(prov)
        await db_session.flush()

        counts = await _importar_compras_por_hoja(db_session, sample_tenant, "P-7", multi=multi)

        assert counts.get("envios") == 1
        assert not counts.get("proveedores_creados_ids")
        envios = await _gastos(db_session, sample_tenant, logistica=True)
        assert [g.supplier_id for g in envios] == [prov.id]
        compras = await _gastos(db_session, sample_tenant, logistica=False)
        assert {g.supplier_id for g in compras} == {prov.id}
        nombres = [s.name for s in await _proveedores(db_session, sample_tenant)]
        assert "Distribuidora" not in nombres
        assert nombres.count("Distri SA") == 1

    async def test_codigo_desconocido_no_crea_proveedor_ni_por_el_envio(
        self, db_session: AsyncSession, sample_tenant: Tenant, multi: bool
    ) -> None:
        counts = await _importar_compras_por_hoja(db_session, sample_tenant, "P-999", multi=multi)

        assert counts.get("envios") == 1
        assert counts["compras_proveedor_codigo_desconocido"] == 2
        assert not counts.get("proveedores_creados_ids")
        assert "Distribuidora" not in {
            s.name for s in await _proveedores(db_session, sample_tenant)
        }
        (envio,) = await _gastos(db_session, sample_tenant, logistica=True)
        assert envio.supplier_id is None

    async def test_sin_envio_codigo_que_resuelve_vincula_sin_crear(
        self, db_session: AsyncSession, sample_tenant: Tenant, multi: bool
    ) -> None:
        prov = Supplier(tenant_id=sample_tenant.tenant_id, name="Distri SA", external_code="P-7")
        db_session.add(prov)
        await db_session.flush()

        counts = await _importar_compras_por_hoja(
            db_session, sample_tenant, "P-7", multi=multi, con_envio=False
        )

        assert counts["compras_proveedor_identificado"] == 2
        assert not counts.get("proveedores_creados_ids")
        compras = await _gastos(db_session, sample_tenant, logistica=False)
        assert {g.supplier_id for g in compras} == {prov.id}

    async def test_sin_envio_codigo_desconocido_no_crea_y_cuenta(
        self, db_session: AsyncSession, sample_tenant: Tenant, multi: bool
    ) -> None:
        counts = await _importar_compras_por_hoja(
            db_session, sample_tenant, "P-999", multi=multi, con_envio=False
        )

        assert counts["compras_proveedor_codigo_desconocido"] == 2
        # B·7 (revisión): cuenta también como no resuelta, como en `link_only`.
        assert counts["compras_proveedor_no_resuelto"] == 2
        assert not counts.get("proveedores_creados_ids")
        assert "Distribuidora" not in {
            s.name for s in await _proveedores(db_session, sample_tenant)
        }
