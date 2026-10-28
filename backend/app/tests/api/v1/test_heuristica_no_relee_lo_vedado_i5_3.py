"""I5-3 — la heurística por nombre no vuelve a leer lo que el mapeo dejó en duda.

El reconocedor ya no propone «Stock mínimo» como stock ni «Código proveedor» como
SKU: los deja en duda y la columna no se mapea sola. Pero el confirm tenía una
tercera capa —`_STOCK_COLS`, `_SKU_COLS`, `_PROVEEDOR_COLS`, los montos, que
matchean por substring— y con el campo sin mapear el producto se guardaba igual
con `stock_units=12` (el umbral de reposición como stock de apertura),
`sku="PROV-77"` (el código del proveedor como identidad) y `marca="PROV-77"`.
Mandarlas a un campo propio, que es lo que la duda propone, las guardaba ahí Y
en stock/SKU/marca. Ahora lo vedado (duda por calificador, campo propio, otro
campo) no lo toma la heurística, y lo legítimo («Stock», «Código») sigue
entrando sin mapear, como antes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import (
    _normalize_col,
    duda_del_calificador,
    read_header,
)
from app.application.services.ingestion_import_service import (
    EmptyImportError,
    _vedadas_a_la_heuristica,
    check_nonempty_import,
    insert_confirmed_data,
)
from app.domain.header_semantics import tokenize
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_CTX = "sheet:catalogo"


def _summary(headers: list[str], fila: dict[str, Any]) -> dict[str, Any]:
    rows = [{**fila, "__context__": _CTX}]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": headers,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


_HEADERS_DUDA = ["Productos", "Código proveedor", "Stock mínimo", "Precio venta"]
_FILA_DUDA = {
    "Productos": "Vela aromática 200g",
    "Código proveedor": "PROV-77",
    "Stock mínimo": "12",
    "Precio venta": "2100",
}
_HEADERS_LEGITIMOS = ["Productos", "Código", "Stock", "Precio venta"]
_FILA_LEGITIMA = {
    "Productos": "Vela aromática 200g",
    "Código": "VEL-200",
    "Stock": "5",
    "Precio venta": "2100",
}


async def _archivo(
    db: AsyncSession, tenant: Tenant, headers: list[str], fila: dict[str, Any]
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="catalogo.xlsx",
        s3_key="uploads/test/uuid/catalogo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(headers, fila),
    )
    db.add(record)
    await db.commit()
    return record


@pytest_asyncio.fixture
async def catalogo_en_duda(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _archivo(db_session, sample_tenant, _HEADERS_DUDA, _FILA_DUDA)


@pytest_asyncio.fixture
async def catalogo_legitimo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _archivo(db_session, sample_tenant, _HEADERS_LEGITIMOS, _FILA_LEGITIMA)


def _m(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "product",
    }


async def _confirmar(
    client: AsyncClient, headers: dict[str, Any], file: UploadedFile, mapeos: list[dict[str, Any]]
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{file.id}/confirm",
        json={
            "column_mappings": mapeos,
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
        },
        headers=headers,
    )


class TestCatalogoPorHoja:
    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param([], id="sin_mapear"),
            pytest.param(
                [
                    _m("Código proveedor", "custom_field:codigo_proveedor"),
                    _m("Stock mínimo", "custom_field:stock_minimo"),
                ],
                id="a_campo_propio",
            ),
        ],
    )
    async def test_umbral_y_codigo_del_proveedor_no_son_stock_sku_ni_marca(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalogo_en_duda: UploadedFile,
        db_session: AsyncSession,
        extra: list[dict[str, Any]],
    ) -> None:
        mapeos = [_m("Productos", "name"), _m("Precio venta", "sale_price_ars"), *extra]
        resp = await _confirmar(client, auth_headers, catalogo_en_duda, mapeos)

        assert resp.status_code == 200, resp.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.sku != "PROV-77"
        assert not prod.stock_units
        cf = prod.custom_fields or {}
        assert cf.get("marca") != "PROV-77"
        if extra:
            # El dato no se pierde: queda en el campo propio que eligió el usuario.
            assert cf.get("codigo_proveedor") == "PROV-77"
            assert cf.get("stock_minimo") == "12"

    async def test_stock_y_codigo_legitimos_sin_mapear_siguen_entrando(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalogo_legitimo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Control: la protección no apaga la heurística para lo que no está en duda."""
        mapeos = [_m("Productos", "name"), _m("Precio venta", "sale_price_ars")]
        resp = await _confirmar(client, auth_headers, catalogo_legitimo, mapeos)

        assert resp.status_code == 200, resp.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.sku == "VEL-200"
        assert prod.stock_units == 5


async def _upload(db: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    up = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, original_filename="stock.csv", s3_key="k",
        content_type="text/csv", size_bytes=10, purpose="ingestion",
    )
    db.add(up)
    await db.flush()
    return up.id


class TestCatalogoPlano:
    """El camino de una sola hoja resuelve las columnas con `_find_col`."""

    async def test_umbral_y_codigo_del_proveedor_no_son_stock_ni_sku(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        await insert_confirmed_data(
            db_session,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "stock",
                "has_producto": True,
                "stock_detectado": [
                    {
                        "producto": "Vela aromática 200g",
                        "código_proveedor": "PROV-77",
                        "stock_mínimo": "12",
                        "precio_venta": "2100",
                    }
                ],
            },
            {"productos": True},
            column_mappings={"producto": "name", "precio_venta": "sale_price_ars"},
            uploaded_file_id=await _upload(db_session, tid),
        )
        await db_session.flush()
        prod = (
            await db_session.execute(select(Product).where(Product.tenant_id == tid))
        ).scalars().one()
        assert prod.sku != "PROV-77"
        assert not prod.stock_units
        assert (prod.custom_fields or {}).get("marca") != "PROV-77"

    async def test_stock_y_codigo_legitimos_sin_mapear_siguen_entrando(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        await insert_confirmed_data(
            db_session,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "stock",
                "has_producto": True,
                "stock_detectado": [
                    {
                        "producto": "Vela aromática 200g",
                        "código": "VEL-200",
                        "stock": "5",
                        "precio_venta": "2100",
                    }
                ],
            },
            {"productos": True},
            column_mappings={"producto": "name", "precio_venta": "sale_price_ars"},
            uploaded_file_id=await _upload(db_session, tid),
        )
        await db_session.flush()
        prod = (
            await db_session.execute(select(Product).where(Product.tenant_id == tid))
        ).scalars().one()
        assert prod.sku == "VEL-200"
        assert prod.stock_units == 5


async def test_venta_no_se_vincula_por_el_codigo_del_proveedor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una hoja de ventas con «Código proveedor» sin mapear: ese código no es el
    SKU del negocio y no puede atribuirle la venta a otro producto que lo tenga."""
    tid = sample_tenant.tenant_id
    otro = Product(
        tenant_id=tid, name="Otro artículo", sku="PROV-77",
        sale_price_ars=Decimal("100"), unit_cost_ars=Decimal("50"), stock_units=10,
    )
    db_session.add(otro)
    await db_session.flush()
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "sheet:Ventas", "entity_type": "sale", "source_kind": "sheet",
             "headers": ["fecha", "producto", "código proveedor", "monto"], "fields": None,
             "preview_rows": [], "row_count": 1},
        ],
        "stock_detectado": [],
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "producto": "Vela", "código proveedor": "PROV-77",
             "monto": "2000", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"ventas": True},
        context_mappings={
            "sheet:Ventas": {
                "fecha": "transaction_date", "producto": "product_name", "monto": "amount"
            },
        },
        uploaded_file_id=await _upload(db_session, tid),
    )
    await db_session.flush()
    vinculadas = (
        await db_session.execute(select(SaleEntry).where(SaleEntry.product_id == otro.id))
    ).scalars().all()
    assert vinculadas == []
    # La venta va a «Otros» (sin producto, B·5e), pero como la de «Vela»: con el
    # código del proveedor como SKU se le atribuía a «Otro artículo».
    otros = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert [o.row_data.get("producto") for o in otros] == ["Vela"]


async def test_el_numero_de_venta_no_es_el_monto_del_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F-H4 sin columna de monto: el total es precio × cantidad. «Nº Venta» sin
    mapear contiene `venta` y la heurística lo leía como el monto que declaró el
    archivo: cada fila quedaba como discrepancia contra su número de orden."""
    tid = sample_tenant.tenant_id
    ctx = "sheet:Ventas"
    headers = ["fecha", "producto", "precio_unit", "cant", "Nº Venta"]
    fila = {"fecha": "2024-03-10", "producto": "Vela", "precio_unit": "150.50",
            "cant": "3", "Nº Venta": "1001"}
    counts = await insert_confirmed_data(
        db_session,
        tid,
        {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "mapping_contexts": [
                {"context_id": ctx, "entity_type": "sale", "source_kind": "sheet",
                 "headers": headers, "fields": None, "preview_rows": [], "row_count": 1}
            ],
            "ventas_detectadas": [{**fila, "__context__": ctx}],
            "gastos_detectados": [],
            "stock_detectado": [],
        },
        {"ventas": True},
        context_mappings={
            ctx: {"fecha": "transaction_date", "producto": "product_name",
                  "precio_unit": "unit_price", "cant": "quantity"}
        },
        context_confirmed={ctx: True},
    )
    assert counts["montos_calculados"] == 1
    assert counts["montos_discrepantes"] == 0


async def test_el_numero_de_venta_no_es_el_monto_en_el_camino_plano(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Gemelo del anterior por el camino de una sola hoja (`venta_col`)."""
    filas = [{"fecha": "2024-03-10", "producto": "Vela", "precio_unit": "150.50",
              "cant": "3", "Nº Venta": "1001"}]
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "ventas",
            "multi_sheet": False,
            "has_venta": True,
            "row_count": 1,
            "headers": ["fecha", "producto", "precio_unit", "cant", "Nº Venta"],
            "ventas_detectadas": filas,
            "preview_rows": filas,
        },
        {"ventas": True},
        column_mappings={"fecha": "transaction_date", "producto": "product_name",
                         "precio_unit": "unit_price", "cant": "quantity"},
    )
    assert counts["montos_discrepantes"] == 0


def _summary_compras(fila: dict[str, Any]) -> dict[str, Any]:
    ctx = "sheet:Compras"
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": ctx, "label": "Compras", "entity_type": "expense",
             "source_kind": "sheet", "headers": list(fila), "fields": None,
             "preview_rows": [], "row_count": 1}
        ],
        "gastos_detectados": [{**fila, "__context__": ctx}],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


async def test_el_numero_de_compra_no_es_el_monto_del_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    fila = {"fecha": "2024-03-05", "articulo": "Vela", "cantidad": "10",
            "precio_unitario": "1200", "Nº Compra": "4471"}
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary_compras(fila),
        {"gastos": True},
        context_mappings={"sheet:Compras": {
            "fecha": "expense_date", "articulo": "product_name",
            "cantidad": "quantity", "precio_unitario": "unit_price",
        }},
        context_confirmed={"sheet:Compras": True},
    )
    assert counts["montos_calculados"] == 1
    assert counts["montos_discrepantes"] == 0


async def test_compra_no_se_vincula_por_el_codigo_del_proveedor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    otro = Product(
        tenant_id=tid, name="Otro artículo", sku="PROV-77",
        sale_price_ars=Decimal("100"), unit_cost_ars=Decimal("50"), stock_units=10,
    )
    db_session.add(otro)
    await db_session.flush()
    fila = {"fecha": "2024-03-05", "articulo": "Vela", "código proveedor": "PROV-77",
            "cantidad": "10", "total": "12000"}
    await insert_confirmed_data(
        db_session,
        tid,
        _summary_compras(fila),
        {"gastos": True},
        context_mappings={"sheet:Compras": {
            "fecha": "expense_date", "articulo": "product_name",
            "cantidad": "quantity", "total": "amount",
        }},
        context_confirmed={"sheet:Compras": True},
    )
    await db_session.flush()
    gastos = (
        await db_session.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
    ).scalars().all()
    assert len(gastos) == 1
    assert gastos[0].product_id != otro.id


class TestLoQueSeVeda:
    @pytest.mark.parametrize(
        ("header", "entidad"),
        [
            ("Stock mínimo", "product"),
            ("Cantidad vendida", "product"),
            ("Código proveedor", "product"),
            ("Código proveedor", "expense"),
            ("N° Venta", "sale"),
            ("Nº Venta", "sale"),
            ("Nº Venta", "expense"),
        ],
    )
    def test_duda_por_calificador(self, header: str, entidad: str) -> None:
        assert duda_del_calificador(_normalize_col(header), entidad)

    @pytest.mark.parametrize(
        ("header", "entidad"),
        [
            # La duda genérica («esta hoja no tiene un campo para eso») o la de
            # «Precio» sin calificador no dicen que la columna sea otra cosa.
            ("Precio", "sale"),
            ("Precio", "product"),
            ("Stock", "sale"),
            ("Stock", "product"),
            ("Código", "product"),
            ("Total", "sale"),
            ("Monto no gravado", "sale"),
        ],
    )
    def test_sin_calificador_no_hay_duda(self, header: str, entidad: str) -> None:
        assert duda_del_calificador(_normalize_col(header), entidad) is None

    def test_vedadas_suma_mapeadas_campos_propios_y_dudas(self) -> None:
        headers = ["Nº Venta", "Total", "Fecha", "Notas"]
        vedadas = _vedadas_a_la_heuristica(
            headers, "sale", {"transaction_date": "Fecha"}, {"notas": "Notas"}
        )
        assert vedadas == {"Nº Venta", "Fecha", "Notas"}


class TestNumeroConOrdinal:
    """MEDIO: «Nº» con U+00BA se plegaba a `no`, excluido a propósito."""

    @pytest.mark.parametrize("entidad", ["sale", "expense", "product"])
    def test_n_ordinal_venta_no_es_monto(self, entidad: str) -> None:
        lectura = read_header(_normalize_col("Nº Venta"), entidad)
        assert lectura.outcome != "unico"
        assert "identificador" in lectura.qualifiers

    def test_monto_no_gravado_sigue_siendo_monto(self) -> None:
        lectura = read_header(_normalize_col("Monto no gravado"), "sale")
        assert lectura.outcome == "unico"
        assert "identificador" not in lectura.qualifiers

    def test_el_ordinal_en_medio_de_palabra_no_cuenta(self) -> None:
        # La n tiene que abrir la palabra: «desdenº» no es «desde» + «nº».
        assert "n°" in tokenize("nº_venta")
        assert "n°" not in tokenize("desdenº_venta")


async def test_la_marca_calcula_las_vedadas_una_vez_por_fila(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendimiento (revalidación de I5-3): la búsqueda de la marca recalculaba las
    vedadas por CADA clave de la fila —columnas² por fila de catálogo, decenas de
    segundos de más en un confirm sincrónico grande—. Con 40 columnas, las
    llamadas por fila no pueden crecer con el ancho de la hoja."""
    from app.application.services import ingestion_import_service as svc

    llamadas = 0
    original = svc._vedadas_a_la_heuristica

    def _contada(*args: Any, **kwargs: Any) -> set[str]:
        nonlocal llamadas
        llamadas += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "_vedadas_a_la_heuristica", _contada)
    relleno = {f"Dato {i}": str(i) for i in range(40)}
    fila = {"Productos": "Vela aromática 200g", "Precio venta": "2100", **relleno}
    summary = _summary(list(fila), fila)
    await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        context_mappings={_CTX: {"Productos": "name", "Precio venta": "sale_price_ars"}},
        context_confirmed={_CTX: True},
        uploaded_file_id=await _upload(db_session, sample_tenant.tenant_id),
    )
    await db_session.flush()
    prod = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().one()
    assert prod.name == "Vela aromática 200g"
    assert 0 < llamadas < 10, llamadas


def _gasto_plano_sin_mapeo(col: str) -> dict[str, Any]:
    filas = [{"fecha": "2024-03-10", "concepto": "Luz", col: "1500"}]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": False,
        "has_gasto": True,
        "row_count": 1,
        "headers": list(filas[0]),
        "gastos_detectados": filas,
        "preview_rows": filas,
    }


class TestTotalDelComprobanteSinMapeo:
    """Cambio declarado de I5-3: `por_comprobante` veda el monto. Un archivo SIN
    mapeo (chat `IMPORT_TABULAR_FILE`, confirm plano sin mapeos) cuyo único monto
    es el total del comprobante ya no se importa: no se inserta nada y
    `check_nonempty_import` corta con `EmptyImportError`, que pide mapear. Es lo
    conservador: repetido en cada línea, ese total inflaría cada fila."""

    @pytest.mark.parametrize("col", ["total_factura", "monto_factura", "total_comprobante"])
    async def test_el_total_del_comprobante_no_es_el_monto_y_pide_mapear(
        self, db_session: AsyncSession, sample_tenant: Tenant, col: str
    ) -> None:
        tid = sample_tenant.tenant_id
        summary = _gasto_plano_sin_mapeo(col)
        counts = await insert_confirmed_data(
            db_session, tid, summary, {"gastos": True},
            column_mappings=None, uploaded_file_id=await _upload(db_session, tid),
        )
        await db_session.flush()
        gastos = (
            await db_session.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
        ).scalars().all()
        assert gastos == []
        assert not counts.get("gastos")
        with pytest.raises(EmptyImportError):
            check_nonempty_import(counts, summary, {"gastos": True})

    async def test_control_un_total_a_secas_sigue_entrando(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        summary = _gasto_plano_sin_mapeo("total")
        counts = await insert_confirmed_data(
            db_session, tid, summary, {"gastos": True},
            column_mappings=None, uploaded_file_id=await _upload(db_session, tid),
        )
        await db_session.flush()
        gastos = (
            await db_session.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
        ).scalars().all()
        assert [g.amount for g in gastos] == [Decimal("1500")]
        check_nonempty_import(counts, summary, {"gastos": True})
