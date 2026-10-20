"""B·5b-2: las compras de mercadería sin artículos de un archivo van a «Otros».

Efecto económico medido con valores, sobre un Libro Diario de ejemplo:
venta 120.000 en efectivo, compra de mercadería 50.000 por transferencia, luz
15.000 por transferencia.

| métrica                                  | antes   | después |
|------------------------------------------|---------|---------|
| Resultado del Resumen económico          | 55.000  | 105.000 |
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
from app.domain.business_time import today_ar
from app.domain.comprobante_de_compra import MOTIVO_COMPRA_SIN_ARTICULOS
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.state.business_state_service import compute_business_state
from app.tests.state.test_business_state_service import FakeRedis

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HOY = today_ar().isoformat()


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada import paga reintentos de kombu."""


def _libro() -> bytes:
    wb = Workbook()
    ld = wb.active
    ld.title = "Libro Diario"
    for fila in (
        ["Fecha", "Detalle", "Dinero", None, "Mercadería", None],
        [None, None, "Ingreso", "Egreso", "Ingreso", "Egreso"],
        [_HOY, "Venta del día", 120000, None, None, None],
        [None, "Compra textiles", None, 50000, 50000, None],
        [None, "Pago luz", None, 15000, None, None],
    ):
        ld.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_libro_diario_la_compra_va_a_otros_y_los_numeros_cambian_asi(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    # Un gasto de mercadería sin producto cargado ANTES de B·5b: queda igual.
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
    historico_id = historico.id

    summary = parse_uploaded_content(_libro(), _XLSX, "libro.xlsx")
    counts = await insert_confirmed_data(
        db_session, tid, summary, {"ventas": True, "gastos": True}
    )
    await db_session.commit()
    assert counts["ventas"] == 1
    assert counts["gastos"] == 1
    assert counts["compras_sin_articulos"] == 1
    otros = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalars().all()
    assert [(o.row_data.get("detalle"), o.context_label, o.suggested_entity) for o in otros] == [
        ("Compra textiles", MOTIVO_COMPRA_SIN_ARTICULOS, "expense")
    ]

    # Resumen económico del mes: 120.000 − (15.000 de luz + 7.000 histórico).
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": today_ar().replace(day=1).isoformat(), "to_date": _HOY},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(str(body["total_income_ars"])) == Decimal("120000")
    assert Decimal(str(body["total_expenses_ars"])) == Decimal("22000")
    assert Decimal(str(body["net_result_ars"])) == Decimal("98000")

    # BSL: la compra ya no es salida líquida ni costo de inventario real; el
    # gasto viejo (efectivo) sigue contando.
    estado = await compute_business_state(tid, db_session, FakeRedis())  # type: ignore[arg-type]
    assert estado.liquid_outflow_est == Decimal("22000")

    # Arqueo: el efectivo esperado sale de la venta en efectivo menos los gastos
    # en efectivo (el histórico); la compra por transferencia nunca lo tocó.
    prev = await client.get(
        f"/api/v1/cash-closes/preview?close_date={_HOY}", headers=auth_headers
    )
    assert prev.status_code == 200, prev.text
    efectivo = {b["payment_method"]: b["expected_ars"] for b in prev.json()["breakdown"]}
    assert efectivo["cash"] == 113000.0

    viejo = await db_session.get(ExpenseEntry, historico_id)
    assert viejo is not None and viejo.voided_at is None
    assert (viejo.amount, viejo.category, viejo.expense_type) == (
        Decimal("7000.00"), "INVENTORY", "COGS"
    )


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
