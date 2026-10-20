"""B·5b: una compra de mercadería es un comprobante con artículos, por todas las rutas.

- La misma compra cargada por «Registrar compra», resuelta desde «Otros» o como
  remito da el mismo costo y el mismo contrato (envío e IVA).
- Resolverla desde «Otros» es idempotente por la fila, y borrar el archivo la
  revierte entera: gasto, envío, movimiento, stock.
- Ningún camino deja un gasto de mercadería sin artículos.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.domain.business_time import today_ar
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HOY = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga reintentos de kombu."""


async def _producto(db: AsyncSession, tid: uuid.UUID, nombre: str) -> Product:
    p = Product(
        tenant_id=tid,
        name=nombre,
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("0"),
        stock_units=0,
        provenance="REAL",
    )
    db.add(p)
    await db.commit()
    return p


async def _proveedor(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/suppliers", json={"name": "Distribuidora Sur"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _movimientos(db: AsyncSession, product_id: uuid.UUID) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.product_id == product_id,
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).scalars()
    )


def _linea(product_id: uuid.UUID, costo: str, cantidad: int) -> dict[str, Any]:
    return {
        "product_id": str(product_id),
        "unit_cost": costo,
        "quantity": cantidad,
        "sale_price_ars": "500",
    }


def _contrato(m: InventoryMovement) -> tuple[Any, ...]:
    return (
        m.unit_cost_final,
        m.shipping_treatment,
        m.tax_treatment,
        m.tax_rate,
        m.tax_amount,
    )


# ── El mismo comprobante por todas las rutas ──────────────────────────────────


async def test_mismo_comprobante_mismo_costo_por_compra_manual_otros_y_remito(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Dos artículos con IVA 21 % incluido y envío de 50 aparte: 10 × $100 y
    5 × $300. Las tres rutas dejan el mismo contrato por línea."""
    tid = sample_tenant.tenant_id
    sid = await _proveedor(client, auth_headers)
    tax = {"treatment": "incluido", "rate": "0.21"}

    # 1) «Registrar compra».
    a1 = await _producto(db_session, tid, "Yerba A")
    b1 = await _producto(db_session, tid, "Azúcar A")
    resp = await client.post(
        "/api/v1/purchases/manual",
        json={
            "supplier_id": sid,
            "transaction_date": _HOY,
            "payment_method": "transfer",
            "shipping": {"amount": "50.00", "mode": "gasto_aparte"},
            "tax": tax,
            "lines": [
                _linea(a1.id, "100.00", 10),
                _linea(b1.id, "300.00", 5),
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text

    # 2) Resolver desde «Otros».
    a2 = await _producto(db_session, tid, "Yerba B")
    b2 = await _producto(db_session, tid, "Azúcar B")
    rec = UnclassifiedRecord(
        tenant_id=tid,
        source="ingestion",
        context_label="Compra sin artículos",
        row_data={"detalle": "Compra", "monto": "2500"},
        suggested_entity="expense",
        status="PENDING",
    )
    db_session.add(rec)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/others/{rec.id}/resolve-purchase",
        json={
            "transaction_date": _HOY,
            "supplier_name": "Distribuidora Sur",
            "shipping": {"amount": "50.00", "mode": "gasto_aparte"},
            "tax": tax,
            "lines": [
                {"product_id": str(a2.id), "quantity": 10, "amount": "1000.00"},
                {"product_id": str(b2.id), "quantity": 5, "amount": "1500.00"},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    # 3) Remito (el envío del remito siempre va aparte).
    resp = await client.post(
        f"/api/v1/suppliers/{sid}/receipts",
        json={
            "currency": "ARS",
            "transaction_date": _HOY,
            "shipping_cost": "50.00",
            "tax": tax,
            "lines": [
                {"product_name": "Yerba C", "qty": 10, "unit_price": "100.00"},
                {"product_name": "Azúcar C", "qty": 5, "unit_price": "300.00"},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    await db_session.commit()

    c = {
        p.name: p.id
        for p in (
            await db_session.execute(select(Product).where(Product.tenant_id == tid))
        ).scalars()
    }
    async def _de(nombre: str) -> tuple[Any, ...]:
        return _contrato((await _movimientos(db_session, c[nombre]))[0])

    yerbas = [await _de(n) for n in ("Yerba A", "Yerba B", "Yerba C")]
    azucares = [await _de(n) for n in ("Azúcar A", "Azúcar B", "Azúcar C")]
    assert len(set(yerbas)) == 1, yerbas
    assert len(set(azucares)) == 1, azucares
    assert yerbas[0] == (
        Decimal("100.00"), "adicional", "incluido", Decimal("0.2100"), Decimal("17.36")
    )

    # El envío aparte es un gasto de logística en las tres rutas.
    envios = (
        await db_session.execute(
            select(ExpenseEntry.amount).where(
                ExpenseEntry.tenant_id == tid, ExpenseEntry.category == "LOGISTICS"
            )
        )
    ).scalars().all()
    assert sorted(envios) == [Decimal("50.00")] * 3


async def test_envio_repartido_entra_al_costo_por_subtotal(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    sid = await _proveedor(client, auth_headers)
    a, b = await _producto(db_session, tid, "A"), await _producto(db_session, tid, "B")
    resp = await client.post(
        "/api/v1/purchases/manual",
        json={
            "supplier_id": sid,
            "transaction_date": _HOY,
            "shipping": {"amount": "400.00", "mode": "repartir"},
            "lines": [
                _linea(a.id, "100.00", 10),
                _linea(b.id, "300.00", 10),
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    await db_session.commit()
    ma, mb = (await _movimientos(db_session, a.id))[0], (await _movimientos(db_session, b.id))[0]
    # 400 por subtotal (1000 / 3000): 100 y 300.
    assert (ma.unit_cost_final, ma.shipping_treatment) == (Decimal("110.00"), "incluido")
    assert mb.unit_cost_final == Decimal("330.00")
    gastos = (
        await db_session.execute(
            select(ExpenseEntry.amount)
            .where(ExpenseEntry.tenant_id == tid)
            .order_by(ExpenseEntry.amount)
        )
    ).scalars().all()
    # Σ gastos = Σ importes + envío; sin gasto de logística aparte.
    assert gastos == [Decimal("1100.00"), Decimal("3300.00")]


# ── «Otros»: idempotente, con procedencia, y el borrado lo revierte ───────────


def _libro_con_una_compra_sin_fecha() -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Compras"
    hoja.append(["fecha", "articulo", "cantidad", "total", "proveedor"])
    hoja.append(["2024-03-05", "Vela aromatica 200g", 5, 6000, "Distribuidora Sur"])
    hoja.append(["cuando pueda", "Difusor bambu", 3, 3600, "Distribuidora Sur"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    contenido = _libro_con_una_compra_sin_fecha()
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/compras.xlsx",
        content_type=_XLSX_MIME,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, "compras.xlsx"),
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def test_resolver_desde_otros_es_idempotente_y_borrar_el_archivo_la_revierte(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    archivo: UploadedFile,
) -> None:
    tid = sample_tenant.tenant_id
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": "fecha", "target_field": "expense_date"},
                {"source_column": "articulo", "target_field": "product_name"},
                {"source_column": "cantidad", "target_field": "quantity"},
                {"source_column": "total", "target_field": "amount"},
                {"source_column": "proveedor", "target_field": "supplier_name"},
            ],
            "confirmed_fields": {"gastos": True},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    fila = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalar_one()
    difusor = await _producto(db_session, tid, "Difusor bambu")
    vela = await _producto(db_session, tid, "Vela de soja")
    cuerpo = {
        "transaction_date": "2024-03-06T00:00:00",
        "shipping": {"amount": "120.00", "mode": "gasto_aparte"},
        "lines": [
            {"product_id": str(difusor.id), "quantity": 3, "amount": "3000.00"},
            {"product_id": str(vela.id), "quantity": 2, "amount": "600.00"},
        ],
    }
    url = f"/api/v1/others/{fila.id}/resolve-purchase"
    ok = await client.post(url, json=cuerpo, headers=auth_headers)
    assert ok.status_code == 200, ok.text
    await db_session.commit()

    # Un reintento (respuesta perdida, doble click) no duplica: la fila ya no está PENDING.
    otra_vez = await client.post(url, json=cuerpo, headers=auth_headers)
    assert otra_vez.status_code == 409
    assert otra_vez.json()["detail"]["code"] == "RECORD_ALREADY_RESOLVED"

    await db_session.refresh(difusor)
    assert difusor.stock_units == 3
    resueltos = (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid,
                ExpenseEntry.source_row_ref.like("%unclassified:%"),
            )
        )
    ).scalars().all()
    # Dos líneas + el envío aparte, todos con la procedencia de la fila.
    assert len(resueltos) == 3
    assert {g.source_upload_id for g in resueltos} == {archivo.id}
    movs = await _movimientos(db_session, difusor.id)
    assert movs[0].source_upload_id == archivo.id

    difusor_id, vela_id = difusor.id, vela.id
    borrado = await client.request(
        "DELETE", f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
    )
    assert borrado.status_code == 200, borrado.text
    db_session.expire_all()
    vivos = (
        await db_session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid, ExpenseEntry.voided_at.is_(None)
            )
        )
    ).scalars().all()
    assert vivos == [], [(g.amount, g.category) for g in vivos]
    assert await _movimientos(db_session, difusor_id) == []
    assert await _movimientos(db_session, vela_id) == []
    dif = await db_session.get(Product, difusor_id)
    assert dif is not None and dif.stock_units == 0


# ── Ningún camino deja mercadería sin artículos ───────────────────────────────


async def test_reclasificar_como_mercaderia_pide_los_articulos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    rec = UnclassifiedRecord(
        tenant_id=sample_tenant.tenant_id,
        source="ingestion",
        context_label="x",
        row_data={"detalle": "Compra", "monto": "900"},
        suggested_entity="expense",
        status="PENDING",
    )
    db_session.add(rec)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={
            "entity_type": "expense",
            "fields": {"amount": "900", "expense_date": _HOY, "category": "Mercadería"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PURCHASE_NEEDS_ITEMS"


async def test_resolver_sin_lineas_ni_body_viejo_pide_los_articulos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    rec = UnclassifiedRecord(
        tenant_id=sample_tenant.tenant_id,
        source="ingestion",
        context_label="x",
        row_data={"monto": "900"},
        suggested_entity="expense",
        status="PENDING",
    )
    db_session.add(rec)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/others/{rec.id}/resolve-purchase",
        json={"transaction_date": _HOY},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PURCHASE_NEEDS_ITEMS"


async def test_remito_sin_fecha_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    sid = await _proveedor(client, auth_headers)
    resp = await client.post(
        f"/api/v1/suppliers/{sid}/receipts",
        json={"currency": "ARS", "lines": [{"product_name": "X", "qty": 1, "unit_price": "10.00"}]},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RECEIPT_DATE_REQUIRED"


@pytest.mark.parametrize(
    ("shipping", "tax"),
    [
        ({"amount": "50", "mode": "desconocido"}, None),
        ({"amount": "0", "mode": "repartir"}, None),
        (None, {"treatment": "incluido"}),
        (None, {"treatment": "no_aplica", "rate": "0.21"}),
    ],
)
async def test_envio_o_iva_incoherente_422_sin_efectos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    shipping: dict[str, str] | None,
    tax: dict[str, str] | None,
) -> None:
    sid = await _proveedor(client, auth_headers)
    a = await _producto(db_session, sample_tenant.tenant_id, "A")
    resp = await client.post(
        "/api/v1/purchases/manual",
        json={
            "supplier_id": sid,
            "transaction_date": _HOY,
            **({"shipping": shipping} if shipping else {}),
            **({"tax": tax} if tax else {}),
            "lines": [_linea(a.id, "10", 1)],
        },
        headers=auth_headers,
    )
    assert resp.status_code in (400, 422), resp.text
    assert await _movimientos(db_session, a.id) == []
