"""F-H6.a — una planilla de compras ya puede declarar cantidad y precio unitario.

Hasta acá `CANONICAL_FIELDS["expense"]` no tenía esos campos: el importador leía
el costo por heurística de headers o no lo leía, y **ésa es la causa de que el
costo de una compra entre mal**. Sin el target explícito tampoco se podía derivar
el monto (F-H4 dejó las compras afuera por exactamente este motivo).

Lo que estos tests fijan es el orden de precedencia: lo que el usuario declaró
gana, la heurística rellena, y una columna declarada para un campo no se relee
como otro.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.line_amount import AMOUNT_ORIGINAL_FIELD, AMOUNT_SOURCE_FIELD
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_CTX = "sheet:Compras"
_PRODUCTO = "Vela aromatica 200g"


def _summary(filas: list[dict[str, Any]], headers: list[str]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": headers,
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def _importar(
    db: AsyncSession,
    tenant: Tenant,
    filas: list[dict[str, Any]],
    headers: list[str],
    mapeo: dict[str, str],
) -> dict[str, Any]:
    counts = await insert_confirmed_data(
        db,
        tenant.tenant_id,
        _summary(filas, headers),
        {"gastos": True},
        context_mappings={_CTX: mapeo},
        context_confirmed={_CTX: True},
    )
    await db.flush()
    return counts


async def _gasto(db: AsyncSession, tenant: Tenant) -> ExpenseEntry:
    filas = (
        (
            await db.execute(
                select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(filas) == 1, f"se esperaba 1 gasto, hay {len(filas)}"
    return filas[0]


async def _producto(db: AsyncSession, tenant: Tenant) -> Product:
    filas = (
        (await db.execute(select(Product).where(Product.tenant_id == tenant.tenant_id)))
        .scalars()
        .all()
    )
    assert len(filas) == 1, f"se esperaba 1 producto, hay {len(filas)}"
    return filas[0]


_HEADERS = ["fecha", "articulo", "cantidad", "precio_unitario", "total"]


def _fila(**over: Any) -> dict[str, Any]:
    return {
        "fecha": "2024-03-05",
        "articulo": _PRODUCTO,
        "cantidad": "10",
        "precio_unitario": "1200",
        "total": "12000",
        **over,
    }


_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_unitario": "unit_price",
    "total": "amount",
}


@pytest.mark.asyncio
class TestElPrecioUnitarioDeclarado:
    async def test_el_costo_del_producto_sale_del_target_mapeado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        await _importar(db_session, sample_tenant, [_fila()], _HEADERS, _MAPEO)

        producto = await _producto(db_session, sample_tenant)
        assert producto.unit_cost_ars == Decimal("1200")
        assert producto.stock_units == 10

    async def test_gana_sobre_la_heuristica_aunque_la_columna_se_llame_distinto(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Lo declarado no se filtra por el nombre de la columna.

        Las guardas de "no tomar un costo total como unitario" existen para la
        HEURÍSTICA; aplicarlas sobre un mapeo explícito sería descartar la
        decisión del usuario por cómo tituló su planilla.
        """
        headers = ["fecha", "articulo", "cantidad", "costo_total", "total"]
        fila = {
            "fecha": "2024-03-05",
            "articulo": _PRODUCTO,
            "cantidad": "10",
            "costo_total": "1200",
            "total": "12000",
        }
        mapeo = {**_MAPEO}
        del mapeo["precio_unitario"]
        mapeo["costo_total"] = "unit_price"

        await _importar(db_session, sample_tenant, [fila], headers, mapeo)

        assert (await _producto(db_session, sample_tenant)).unit_cost_ars == Decimal("1200")

    async def test_sin_mapeo_la_heuristica_sigue_funcionando(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Control: el mapeo explícito no reemplaza a la heurística, la precede.

        Un libro de compras que no declara nada tiene que seguir entrando igual
        que antes de F-H6 (regresión de `test_ingestion_flat_mapping_fallbacks`).
        La columna se llama `costo_unitario` porque ES uno de los headers que la
        heurística conoce — ver el test de abajo.
        """
        headers = ["fecha", "articulo", "cantidad", "costo_unitario", "total"]
        fila = {
            "fecha": "2024-03-05",
            "articulo": _PRODUCTO,
            "cantidad": "10",
            "costo_unitario": "1200",
            "total": "12000",
        }
        mapeo = {k: v for k, v in _MAPEO.items() if k != "precio_unitario"}
        await _importar(db_session, sample_tenant, [fila], headers, mapeo)

        assert (await _producto(db_session, sample_tenant)).unit_cost_ars == Decimal("1200")

    async def test_la_heuristica_no_conoce_precio_unitario_y_por_eso_hace_falta_el_target(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El motivo concreto de F-H6.a, medido.

        `_COSTO_UNITARIO_COLS` tiene `costo_unitario`, `precio_costo` y
        `precio_compra`, pero NO `precio_unitario` — y "precio unitario" es como
        titula la columna media planilla de compras. Sin mapearlo, el costo se
        pierde: el producto queda sin costo y el margen, en cero. Con el target
        explícito (el test de más arriba) entra.
        """
        mapeo = {k: v for k, v in _MAPEO.items() if k != "precio_unitario"}
        await _importar(db_session, sample_tenant, [_fila()], _HEADERS, mapeo)

        assert (await _producto(db_session, sample_tenant)).unit_cost_ars is None


@pytest.mark.asyncio
class TestElMontoDeUnaCompraTambienSeCalcula:
    """F-H4 para compras: existía la fórmula, faltaban los campos."""

    async def test_sin_columna_de_total_el_monto_es_precio_por_cantidad(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        headers = ["fecha", "articulo", "cantidad", "precio_unitario"]
        mapeo = {k: v for k, v in _MAPEO.items() if k != "total"}
        fila = {k: v for k, v in _fila().items() if k != "total"}

        counts = await _importar(db_session, sample_tenant, [fila], headers, mapeo)

        assert counts["gastos"] == 1
        assert counts["montos_calculados"] == 1
        gasto = await _gasto(db_session, sample_tenant)
        assert gasto.amount == Decimal("12000.00")  # 1200 × 10
        assert (gasto.custom_fields or {})[AMOUNT_SOURCE_FIELD] == "calculated"

    async def test_un_total_que_no_cuadra_se_reporta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar(
            db_session, sample_tenant, [_fila(total="11500")], _HEADERS, _MAPEO
        )

        assert counts["montos_discrepantes"] == 1
        gasto = await _gasto(db_session, sample_tenant)
        assert gasto.amount == Decimal("12000.00")
        assert (gasto.custom_fields or {})[AMOUNT_ORIGINAL_FIELD] == "11500"

    async def test_el_total_declarado_que_cuadra_entra_tal_cual(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        counts = await _importar(db_session, sample_tenant, [_fila()], _HEADERS, _MAPEO)

        assert counts["montos_discrepantes"] == 0
        assert counts["montos_calculados"] == 0
        gasto = await _gasto(db_session, sample_tenant)
        assert gasto.amount == Decimal("12000")
        assert AMOUNT_SOURCE_FIELD not in (gasto.custom_fields or {})

    async def test_la_heuristica_del_monto_no_relee_la_columna_del_precio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """`_GASTO_AMOUNT_COLS` contiene "costo" y "compra": sin excluir lo ya
        declarado, una columna «precio_compra» mapeada a `unit_price` volvía a
        entrar como el monto de la línea — el mismo defecto que F-H4 corrigió en
        ventas."""
        headers = ["fecha", "articulo", "cantidad", "precio_compra"]
        fila = {
            "fecha": "2024-03-05",
            "articulo": _PRODUCTO,
            "cantidad": "10",
            "precio_compra": "1200",
        }
        mapeo = {
            "fecha": "expense_date",
            "articulo": "product_name",
            "cantidad": "quantity",
            "precio_compra": "unit_price",
        }

        counts = await _importar(db_session, sample_tenant, [fila], headers, mapeo)

        assert counts["gastos"] == 1
        # 1200 × 10, no 1200 leído como total de la línea.
        assert (await _gasto(db_session, sample_tenant)).amount == Decimal("12000.00")


# ── F-H6.b: el envío de un remito se cobra una vez ──────────────────────────

_HEADERS_ENVIO = ["fecha", "articulo", "cantidad", "precio_unitario", "comprobante", "envio"]
_MAPEO_ENVIO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_unitario": "unit_price",
    "comprobante": "invoice_number",
    "envio": "shipping_cost",
    "proveedor": "supplier_name",
}


def _linea_remito(
    articulo: str,
    *,
    comprobante: str = "A-0001-12345",
    envio: str = "2000",
) -> dict[str, Any]:
    return {
        "fecha": "2024-03-05",
        "articulo": articulo,
        "cantidad": "1",
        "precio_unitario": "1000",
        "comprobante": comprobante,
        "envio": envio,
        "proveedor": "Distribuidora Sur",
    }


async def _logistica(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    filas = (
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant.tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                )
            )
        )
        .scalars()
        .all()
    )
    return list(filas)


@pytest.mark.asyncio
class TestElEnvioSeCobraUnaVez:
    async def test_diez_lineas_del_mismo_remito_son_un_solo_envio(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """El caso que motiva la fase: $2.000 de flete, no $20.000."""
        filas = [_linea_remito(f"Articulo {i}") for i in range(10)]
        headers = [*_HEADERS_ENVIO, "proveedor"]

        counts = await _importar(db_session, sample_tenant, filas, headers, _MAPEO_ENVIO)

        envios = await _logistica(db_session, sample_tenant)
        assert len(envios) == 1
        assert envios[0].amount == Decimal("2000.00")
        assert envios[0].expense_type == "OPEX"
        assert counts["envios"] == 1
        assert counts["envios_repetidos_colapsados"] == 1

    async def test_dos_remitos_son_dos_envios(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        filas = [
            _linea_remito("Vela", comprobante="A-0001-11111", envio="2000"),
            _linea_remito("Sahumerio", comprobante="A-0001-11111", envio="2000"),
            _linea_remito("Difusor", comprobante="A-0001-22222", envio="3500"),
        ]
        headers = [*_HEADERS_ENVIO, "proveedor"]

        await _importar(db_session, sample_tenant, filas, headers, _MAPEO_ENVIO)

        envios = await _logistica(db_session, sample_tenant)
        assert sorted(e.amount for e in envios) == [Decimal("2000.00"), Decimal("3500.00")]

    async def test_sin_comprobante_no_se_cobra_y_se_reporta(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Regla no-invention: sin identidad, un 2.000 repetido diez veces es
        indistinguible de diez envíos de 2.000."""
        filas = [_linea_remito(f"Articulo {i}") for i in range(10)]
        headers = [*_HEADERS_ENVIO, "proveedor"]
        mapeo = {k: v for k, v in _MAPEO_ENVIO.items() if k != "comprobante"}

        counts = await _importar(db_session, sample_tenant, filas, headers, mapeo)

        assert await _logistica(db_session, sample_tenant) == []
        assert counts["envios_sin_comprobante"] == 10
        # Las compras en sí entran igual: el envío no bloquea el resto de la hoja.
        assert counts["gastos"] == 10

    async def test_reconfirmar_no_cobra_el_envio_dos_veces(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """La huella del cargo vive en su propio namespace: la clave es el
        comprobante + la cifra, no una fila arbitraria del grupo."""
        import uuid as _uuid

        from app.persistence.models.file import UploadedFile

        subido = UploadedFile(
            id=_uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            original_filename="compras.xlsx",
            s3_key="test/compras.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=512,
            purpose="ingestion",
        )
        db_session.add(subido)
        await db_session.flush()

        filas = [_linea_remito("Vela"), _linea_remito("Sahumerio")]
        headers = [*_HEADERS_ENVIO, "proveedor"]
        for _ in range(2):
            await insert_confirmed_data(
                db_session,
                sample_tenant.tenant_id,
                _summary(filas, headers),
                {"gastos": True},
                context_mappings={_CTX: _MAPEO_ENVIO},
                context_confirmed={_CTX: True},
                uploaded_file_id=subido.id,
            )
            await db_session.flush()

        assert len(await _logistica(db_session, sample_tenant)) == 1
