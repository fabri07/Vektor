"""B·5b-2: las compras de mercadería sin artículos de un archivo van a «Otros».

Efecto económico medido con valores, sobre un Libro Diario de ejemplo:
venta 120.000 en efectivo, compra de mercadería 50.000 por transferencia, luz
15.000 por transferencia.

| métrica                                  | antes   | después |
|------------------------------------------|---------|---------|
| Resultado del Resumen económico          | 55.000  | 105.000 |
(el test suma además un gasto histórico de 7.000 en efectivo que no se toca:
98.000 de resultado, 22.000 de salida líquida y 113.000 de efectivo esperado)
| Salida líquida estimada (BSL)            | 65.000  | 15.000  |
| Costo de inventario real del BSL         | 50.000  | 0       |
| Efectivo esperado del arqueo             | 120.000 | 120.000 |

La compra no se pierde: queda en «Otros» con su motivo y se resuelve por líneas
(`resolve-purchase`), y ahí vuelve a contar con sus artículos. Un gasto de
mercadería sin producto que YA estaba cargado no se toca.
"""

from __future__ import annotations

import io
from datetime import datetime, time
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.respaldo_service import MOTIVO_VENTA_SIN_PRODUCTO
from app.domain.business_time import today_ar
from app.domain.comprobante_de_compra import MOTIVO_COMPRA_SIN_ARTICULOS
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.state.business_state_service import compute_business_state
from app.tests._respaldo import archivo_de_prueba, productos_con_compra
from app.tests.state.test_business_state_service import FakeRedis

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HOY = today_ar().isoformat()


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada import paga reintentos de kombu."""


def _libro(detalle_venta: str = "Venta del día") -> bytes:
    wb = Workbook()
    ld = wb.active
    ld.title = "Libro Diario"
    for fila in (
        ["Fecha", "Detalle", "Dinero", None, "Mercadería", None],
        [None, None, "Ingreso", "Egreso", "Ingreso", "Egreso"],
        [_HOY, detalle_venta, 120000, None, None, None],
        [None, "Compra textiles", None, 50000, 50000, None],
        [None, "Pago luz", None, 15000, None, None],
    ):
        ld.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _historico(db_session: AsyncSession, tid: Any) -> Any:
    """Un gasto de mercadería sin producto cargado ANTES de B·5b: queda igual."""
    historico = ExpenseEntry(
        tenant_id=tid,
        amount=Decimal("7000"),
        category="INVENTORY",
        expense_type="COGS",
        transaction_date=datetime.combine(today_ar(), time(12)),
        description="Compra vieja",
        payment_method="cash",
        provenance="REAL",
    )
    db_session.add(historico)
    await db_session.commit()
    return historico.id


async def _otros(db_session: AsyncSession, tid: Any) -> list[tuple[Any, ...]]:
    filas = (
        await db_session.execute(
            select(UnclassifiedRecord)
            .where(UnclassifiedRecord.tenant_id == tid)
            .order_by(UnclassifiedRecord.context_label)
        )
    ).scalars().all()
    # El motivo de B·5e lleva un detalle después del rótulo; se compara por prefijo.
    def _motivo(label: str | None) -> str | None:
        if label and label.startswith(MOTIVO_VENTA_SIN_PRODUCTO):
            return MOTIVO_VENTA_SIN_PRODUCTO
        return label

    return [
        (o.row_data.get("detalle"), _motivo(o.context_label), o.suggested_entity)
        for o in filas
    ]


async def _numeros(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession, tid: Any
) -> tuple[Decimal, Decimal, Decimal, Decimal, float]:
    """(ingresos, gastos, resultado del Resumen económico, salida líquida del BSL,
    efectivo esperado del arqueo)."""
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": today_ar().replace(day=1).isoformat(), "to_date": _HOY},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    estado = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    prev = await client.get(
        f"/api/v1/cash-closes/preview?close_date={_HOY}", headers=auth_headers
    )
    assert prev.status_code == 200, prev.text
    efectivo = {b["payment_method"]: b["expected_ars"] for b in prev.json()["breakdown"]}
    return (
        Decimal(str(body["total_income_ars"])),
        Decimal(str(body["total_expenses_ars"])),
        Decimal(str(body["net_result_ars"])),
        estado.liquid_outflow_est,
        efectivo.get("cash", 0.0),
    )


async def _el_historico_sigue_igual(db_session: AsyncSession, historico_id: Any) -> None:
    viejo = await db_session.get(ExpenseEntry, historico_id)
    assert viejo is not None and viejo.voided_at is None
    assert (viejo.amount, viejo.category, viejo.expense_type) == (
        Decimal("7000.00"), "INVENTORY", "COGS"
    )


async def test_libro_diario_la_compra_va_a_otros_y_los_numeros_cambian_asi(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La venta del libro es de un producto con compra previa (B·5e), así que entra.

    Cuentas:
    - Resumen económico: ingresos 120.000; gastos 15.000 de luz + 7.000 histórico
      = 22.000 (la compra textiles está en «Otros»); resultado 98.000.
    - Salida líquida del BSL: 15.000 + 7.000 = 22.000.
    - Efectivo esperado del arqueo: 120.000 de la venta − 7.000 del histórico =
      113.000 (la compra y la luz son por transferencia).
    """
    tid = sample_tenant.tenant_id
    historico_id = await _historico(db_session, tid)
    await productos_con_compra(db_session, tid, ["Vela"])

    summary = parse_uploaded_content(_libro(detalle_venta="Vela"), _XLSX, "libro.xlsx")
    counts = await insert_confirmed_data(
        db_session, tid, summary, {"ventas": True, "gastos": True}
    )
    await db_session.commit()
    assert counts["ventas"] == 1
    assert counts["gastos"] == 1
    assert counts["compras_sin_articulos"] == 1
    assert await _otros(db_session, tid) == [
        ("Compra textiles", MOTIVO_COMPRA_SIN_ARTICULOS, "expense")
    ]
    assert await _numeros(client, auth_headers, db_session, tid) == (
        Decimal("120000"), Decimal("22000"), Decimal("98000"), Decimal("22000"), 113000.0
    )
    await _el_historico_sigue_igual(db_session, historico_id)


async def test_libro_diario_la_venta_sin_producto_tambien_va_a_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """«Venta del día» no dice qué se vendió: B·5e la manda a «Otros» junto con la
    compra sin artículos.

    Cuentas:
    - Resumen económico: ingresos 0; gastos 15.000 de luz + 7.000 histórico =
      22.000; resultado −22.000.
    - Salida líquida del BSL: 15.000 + 7.000 = 22.000.
    - Efectivo esperado del arqueo: 0 de ventas − 7.000 del histórico = −7.000.
    """
    tid = sample_tenant.tenant_id
    historico_id = await _historico(db_session, tid)

    summary = parse_uploaded_content(_libro(), _XLSX, "libro.xlsx")
    counts = await insert_confirmed_data(
        db_session, tid, summary, {"ventas": True, "gastos": True},
        uploaded_file_id=await archivo_de_prueba(db_session, tid),
    )
    await db_session.commit()
    assert counts["ventas"] == 0
    assert counts["ventas_sin_producto"] == 1
    assert counts["gastos"] == 1
    assert counts["compras_sin_articulos"] == 1
    assert sorted(await _otros(db_session, tid)) == sorted([
        ("Compra textiles", MOTIVO_COMPRA_SIN_ARTICULOS, "expense"),
        ("Venta del día", MOTIVO_VENTA_SIN_PRODUCTO, "sale"),
    ])
    assert await _numeros(client, auth_headers, db_session, tid) == (
        Decimal("0"), Decimal("22000"), Decimal("-22000"), Decimal("22000"), -7000.0
    )
    await _el_historico_sigue_igual(db_session, historico_id)


async def test_un_gasto_que_solo_se_parece_a_un_producto_no_pasa_a_mercaderia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin cantidad, el parecido del texto con un producto no prueba una compra:
    «Pago luz» sigue siendo luz (OPEX), no una compra COGS sin artículos."""
    from app.persistence.models.product import Product

    tid = sample_tenant.tenant_id
    db_session.add(Product(tenant_id=tid, name="Lampara luz led", sale_price_ars=Decimal("100")))
    await db_session.commit()
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": _HOY, "gasto": "15000", "producto": "luz", "categoria": "Luz"}
        ],
    }
    await insert_confirmed_data(db_session, tid, summary, {"gastos": True})
    gasto = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert (gasto.category, gasto.expense_type, gasto.product_id) == ("UTILITIES", "OPEX", None)


# ── Revisión de B·5b-2 ────────────────────────────────────────────────────────


def _gastos_planos(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": filas,
    }


async def _otros_de(db: AsyncSession, tid: Any) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        ).scalars()
    )


async def test_una_hoja_mercaderia_sin_cantidad_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El camino de tabla suelta, con su propia aserción."""
    tid = sample_tenant.tenant_id
    counts = await insert_confirmed_data(
        db_session,
        tid,
        _gastos_planos(
            [{"fecha": _HOY, "gasto": "9000", "producto": "Fardo", "categoria": "Mercadería"}]
        ),
        {"gastos": True},
    )
    assert counts["compras_sin_articulos"] == 1
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []
    assert [o.context_label for o in await _otros_de(db_session, tid)] == [
        MOTIVO_COMPRA_SIN_ARTICULOS
    ]


async def test_cantidad_con_decimales_de_un_producto_nuevo_no_deja_un_cogs_suelto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Revisión: «2,5» pasaba el primer filtro pero no creaba el producto nuevo, y
    quedaba un gasto COGS sin producto ni stock. El control final lo manda a «Otros»."""
    tid = sample_tenant.tenant_id
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "sheet:Compras", "entity_type": "expense", "source_kind": "sheet",
             "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
             "fields": None, "preview_rows": [], "row_count": 1},
        ],
        "gastos_detectados": [
            {"fecha": _HOY, "producto": "Queso cremoso", "cantidad": Decimal("2.5"),
             "monto": "2000", "categoria": "Mercadería", "__context__": "sheet:Compras"}
        ],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }
    counts = await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"gastos": True},
        context_mappings={
            "sheet:Compras": {"fecha": "expense_date", "producto": "product_name",
                              "cantidad": "quantity", "monto": "amount",
                              "categoria": "category"},
        },
    )
    assert counts["compras_sin_articulos"] == 1
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []


async def test_sku_exacto_sin_cantidad_es_una_compra_sin_cantidad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Revisión: un SKU exacto sin categoría ni cantidad es mercadería sin cuántos
    → «Otros»; antes se desvinculaba y quedaba OPEX, esquivando la regla."""
    from app.persistence.models.product import Product

    tid = sample_tenant.tenant_id
    db_session.add(
        Product(tenant_id=tid, name="Coca 500", sku="CC-1", sale_price_ars=Decimal("100"))
    )
    await db_session.commit()
    counts = await insert_confirmed_data(
        db_session,
        tid,
        _gastos_planos([{"fecha": _HOY, "gasto": "500", "sku": "CC-1", "producto": "Coca 500"}]),
        {"gastos": True},
    )
    assert counts["compras_sin_articulos"] == 1
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []
