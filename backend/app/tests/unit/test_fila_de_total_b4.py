"""B·4: una fila «Total» / «Subtotal» no es una operación, y «Total Clean» sí.

La regla exige las dos cosas: etiqueta EXACTA de total y forma de agregado (es
la única celda de texto y hay al menos un número). Lo que la cumple va a
«Otros» con su motivo; nunca se importa (sumaría dos veces) ni se descarta en
silencio.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import (
    detect_libro_diario_header,
    parse_libro_diario,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.fila_de_total import (
    MOTIVO_FILA_DE_TOTAL,
    es_etiqueta_de_total,
    es_fila_de_total,
)
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

# ── Dominio ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "etiqueta",
    [
        "Total",
        "TOTAL:",
        "Subtotal",
        "Sub total",
        "Totales",
        "Total general",
        "Total mes",
        "Total del mes",
        "Total enero",
        "Total marzo 2026",
        "Total 2026",
        "Subtotal 03/2026",
        "  total  ",
    ],
)
def test_etiquetas_de_total(etiqueta: str) -> None:
    assert es_etiqueta_de_total(etiqueta)


@pytest.mark.parametrize(
    "etiqueta",
    [
        "Total Clean",
        "Totalmente nuevo",
        "Total de la seña",
        "Saldo",
        "Pago total proveedor",
        "Total compras",
        "",
    ],
)
def test_no_son_etiquetas_de_total(etiqueta: str) -> None:
    assert not es_etiqueta_de_total(etiqueta)


def test_fila_de_total_exige_forma_de_agregado() -> None:
    # Etiqueta + montos (+ fecha) → total.
    assert es_fila_de_total(["Total", "1.234,50", None, ""])
    assert es_fila_de_total([date(2026, 3, 31), "TOTAL", Decimal("900")])
    assert es_fila_de_total(["Subtotal", 12, "1.234.567", "45%"])
    # Sin ningún número no hay agregado: es un rótulo, no un total.
    assert not es_fila_de_total(["Total", None, ""])
    # Otra celda de texto (SKU, cliente, descripción) → es un dato.
    assert not es_fila_de_total(["Total", "TC-1", 500])
    assert not es_fila_de_total(["Total", "Juan Pérez", 500])
    # La etiqueta no es exacta.
    assert not es_fila_de_total(["Total Clean", 500])


# ── Libro Diario: el prefijo ya no descarta en silencio ───────────────────────

_LD: list[list[Any]] = [
    ["Fecha", "Detalle", "Dinero", None, "Mercadería", None],
    [None, None, "Ingreso", "Egreso", "Ingreso", "Egreso"],
    ["2026-05-01", "Venta del día", 120000, None, None, 80000],
    [None, "Saldo inicial", 50000, None, None, None],
    [None, "Saldo al 30/04", None, 3000, None, None],
    [None, "Acumulado", 170000, None, None, None],
    [None, "Total mes", 120000, None, None, None],
]


def test_libro_diario_manda_totales_y_saldos_a_otros_con_motivo() -> None:
    detected = detect_libro_diario_header(_LD)
    assert detected is not None
    parsed = parse_libro_diario(_LD, *detected)

    # Sólo la venta real se importa; ningún total ni saldo es venta ni gasto.
    assert [v["monto"] for v in parsed["ventas"]] == ["120000"]
    assert parsed["gastos"] == []
    motivos = {f["detalle"]: f["motivo"] for f in parsed["no_ventas"]}
    assert set(motivos) == {"Saldo inicial", "Saldo al 30/04", "Acumulado", "Total mes"}
    # «Saldo inicial» tiene su propio motivo del vocabulario de tipos.
    assert motivos["Saldo inicial"].lower().startswith("saldo inicial")
    assert motivos["Total mes"].startswith("Fila de total")
    assert motivos["Saldo al 30/04"].startswith("Fila de total")
    # El monto viaja con la fila, sea ingreso o egreso.
    egreso = next(f for f in parsed["no_ventas"] if f["detalle"] == "Saldo al 30/04")
    assert egreso["dinero_egreso"] == "3000"


# ── Import: camino de una hoja ────────────────────────────────────────────────


async def _upload(db: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    up = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        original_filename="ventas.csv",
        s3_key="k",
        content_type="text/csv",
        size_bytes=10,
        purpose="ingestion",
    )
    db.add(up)
    await db.flush()
    return up.id


def _ventas_summary() -> dict[str, Any]:
    rows = [
        {"fecha": "2026-03-30", "producto": "Yerba", "monto": "600"},
        {"fecha": "2026-03-31", "producto": "Azúcar", "monto": "400"},
        # Con fecha y monto: antes entraba como una venta de $1.000 más.
        {"fecha": "2026-03-31", "producto": "TOTAL", "monto": "1000"},
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "row_count": len(rows),
        "ventas_detectadas": rows,
        "gastos_detectados": [],
        "stock_detectado": [],
    }


async def _otros(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        )
        .scalars()
        .all()
    )


async def test_la_fila_total_de_ventas_va_a_otros_y_no_suma(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    upload_id = await _upload(db_session, tid)
    counts = await insert_confirmed_data(
        db_session, tid, _ventas_summary(), {"ventas": True}, uploaded_file_id=upload_id
    )
    assert counts["ventas"] == 2
    assert counts["filas_de_total"] == 1
    ventas = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert sum((v.amount for v in ventas), Decimal("0")) == Decimal("1000")
    otros = await _otros(db_session, tid)
    assert [o.context_label for o in otros] == [MOTIVO_FILA_DE_TOTAL]
    assert otros[0].uploaded_file_id == upload_id

    # Reconfirmar el mismo archivo no duplica la fila en la bandeja.
    await insert_confirmed_data(
        db_session, tid, _ventas_summary(), {"ventas": True}, uploaded_file_id=upload_id
    )
    assert len(await _otros(db_session, tid)) == 1
    assert len((await db_session.execute(select(SaleEntry))).scalars().all()) == 2


def _catalogo_summary() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Total Clean", "stock": "3"},
            {"producto": "Total", "sku": "T-1", "stock": "2"},
            {"producto": "Totales", "stock": "5"},
        ],
    }


async def test_catalogo_total_clean_es_producto_y_totales_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    upload_id = await _upload(db_session, tid)
    mappings = {"producto": "name", "sku": "sku", "stock": "stock_units"}
    for _ in range(2):  # la segunda es una reconfirmación: no crea el «Totales»
        await insert_confirmed_data(
            db_session,
            tid,
            _catalogo_summary(),
            {"productos": True},
            column_mappings=mappings,
            uploaded_file_id=upload_id,
        )
    nombres = {
        p.name
        for p in (
            await db_session.execute(select(Product).where(Product.tenant_id == tid))
        ).scalars()
    }
    # «Total Clean» no es una etiqueta exacta; «Total» con SKU tiene otra celda de
    # texto: los dos son productos. «Totales» con sólo un número es el total.
    assert {"Total Clean", "Total"} <= nombres
    assert "Totales" not in nombres
    otros = await _otros(db_session, tid)
    assert [o.context_label for o in otros] == [MOTIVO_FILA_DE_TOTAL]


# ── Import: camino de varias hojas ────────────────────────────────────────────


def _multihoja_summary() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "detalle", "monto"],
                "fields": None,
                "preview_rows": [],
                "row_count": 3,
            },
        ],
        "ventas_detectadas": [
            {"fecha": f, "detalle": d, "monto": m, "__context__": "sheet:Ventas"}
            for f, d, m in (
                ("2026-03-30", "Mostrador", "700"),
                ("2026-03-31", "Mostrador", "300"),
                ("2026-03-31", "Subtotal marzo", "1000"),
            )
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
    }


async def test_multihoja_subtotal_va_a_otros_con_su_hoja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    upload_id = await _upload(db_session, tid)
    for _ in range(2):
        counts = await insert_confirmed_data(
            db_session, tid, _multihoja_summary(), {"ventas": True}, uploaded_file_id=upload_id
        )
    assert len((await db_session.execute(select(SaleEntry))).scalars().all()) == 2
    otros = await _otros(db_session, tid)
    assert len(otros) == 1
    assert otros[0].context_label == MOTIVO_FILA_DE_TOTAL
    assert otros[0].context_id == "sheet:Ventas"
    assert counts.get("filas_de_total", 0) == 0  # la reconfirmación no la recaptura


# ── El total no se lleva parte del envío repartido ────────────────────────────


def test_el_total_de_la_hoja_no_entra_al_reparto_del_envio() -> None:
    from app.application.services.ingestion_import_service import (
        _planificar_costos_de_la_hoja,
    )
    from app.domain.purchase_cost import COMPARTIDO_SUBTOTAL
    from app.domain.purchase_cost_decision import PurchaseCostDecision
    from app.domain.purchase_shipping import SIN_COMPROBANTE_UNA_POR_HOJA

    rows = [
        {"producto": "Mate", "monto": "100", "envio": "40"},
        {"producto": "Bombilla", "monto": "300"},
        # El pie de la hoja repite la suma y el envío: sin la regla, por
        # subtotal se llevaba la mitad del flete.
        {"producto": "Total", "monto": "400", "envio": "40"},
    ]
    costos, _, _ = _planificar_costos_de_la_hoja(
        "sheet:Compras",
        rows,
        {"amount": "monto", "shipping_cost": "envio", "product_name": "producto"},
        {
            "sheet:Compras": PurchaseCostDecision(
                context_id="sheet:Compras", shared_shipping=COMPARTIDO_SUBTOTAL
            )
        },
        sin_comprobante=SIN_COMPROBANTE_UNA_POR_HOJA,
    )
    assert 2 not in costos
    assert costos[0].shipping_allocated + costos[1].shipping_allocated == Decimal("40")
    assert costos[0].shipping_allocated == Decimal("10")
