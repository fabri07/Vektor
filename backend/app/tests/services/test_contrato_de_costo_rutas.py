"""M·1b — cada ruta de compra deja el contrato del costo en su movimiento.

Contra el importador REAL (``insert_confirmed_data``) y por los DOS caminos, tabla
suelta y hoja de un libro: el mismo archivo tiene que dejar el mismo contrato
entre por donde entre (la asimetría que este importador ya pagó dos veces).
Y contra el embudo de las acciones del chat (``execute_pending_action``).
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.pending_action_service import execute_pending_action
from app.domain.business_time import today_ar
from app.domain.costo_de_referencia import CONTRATO_VERSION
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

_CTX = "table"
_COLS = {
    "fecha": "expense_date",
    "nro": "invoice_number",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_compra": "unit_price",
    "total": "amount",
    "envio": "shipping_cost",
    "iva": "taxes",
}

#: Un comprobante de dos líneas con el envío repetido (la forma real) e IVA por línea.
#: El precio va en «precio_compra» a propósito: el camino de tabla suelta NO lee el
#: mapeo explícito a ``unit_price`` en compras (sólo reconoce el costo unitario por
#: el nombre de la columna) y el multi-hoja sí. Es una asimetría previa a M·1b,
#: registrada aparte: arreglarla mueve la huella de las filas ya importadas.
_FILAS: list[dict[str, Any]] = [
    {"fecha": "2024-03-05", "nro": "0001-00000123", "proveedor": "Sur",
     "articulo": "Vela", "cantidad": "10", "precio_compra": "100", "total": "1000",
     "envio": "300", "iva": "210"},
    {"fecha": "2024-03-05", "nro": "0001-00000123", "proveedor": "Sur",
     "articulo": "Difusor", "cantidad": "5", "precio_compra": "400", "total": "2000",
     "envio": "300", "iva": "0"},
]


def _summary(*, multi: bool, cols: dict[str, str]) -> dict[str, Any]:
    filas = [{k: v for k, v in f.items() if k in cols} for f in _FILAS]
    return {
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
        "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
    }


async def _importar(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    multi: bool,
    decision: PurchaseCostDecision | None = None,
    cols: dict[str, str] | None = None,
) -> dict[str, InventoryMovement]:
    cols = cols or _COLS
    upload_id = uuid.uuid4()
    db.add(
        UploadedFile(
            id=upload_id,
            tenant_id=tenant_id,
            original_filename="compras.xlsx",
            s3_key=f"tests/{upload_id}.xlsx",
            content_type="text/csv",
            size_bytes=1,
            purpose="ingestion",
            status="uploaded",
            processing_status="DONE",
        )
    )
    await db.flush()
    await insert_confirmed_data(
        db,
        tenant_id,
        _summary(multi=multi, cols=cols),
        {},
        context_mappings={_CTX: dict(cols)},
        context_entity={_CTX: "expense"},
        context_confirmed={_CTX: True},
        source="ingestion",
        uploaded_file_id=upload_id,
        shipping_decisions={_CTX: "una_por_hoja"} if "envio" in cols else None,
        purchase_cost_decisions={_CTX: decision} if decision is not None else None,
    )
    await db.flush()
    filas = (
        await db.execute(
            select(Product.name, InventoryMovement)
            .join(Product, Product.id == InventoryMovement.product_id)
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.movement_type == "purchase",
            )
        )
    ).all()
    return dict(filas)  # type: ignore[arg-type]


_CAMINOS = pytest.mark.parametrize("multi", [False, True], ids=["tabla", "hoja"])


@_CAMINOS
async def test_sin_decision_el_envio_va_aparte_y_el_iva_queda_desconocido(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Default (``no_distribuir`` + ``monto_incluye``): el envío del comprobante
    es un gasto de logística → ``adicional``; el IVA mapeado NO prueba nada."""
    movs = await _importar(db_session, sample_tenant.tenant_id, multi=multi)
    vela = movs["Vela"]
    assert vela.cost_contract_version == CONTRATO_VERSION
    assert vela.shipping_treatment == "adicional"
    assert vela.tax_treatment == "desconocido"
    assert vela.tax_amount is None
    assert vela.tax_rate is None and vela.tax_recoverable is None
    assert vela.unit_cost_final == Decimal("100.00")
    assert vela.document_ref == "0001-00000123"


@_CAMINOS
async def test_envio_repartido_e_iva_sumado_quedan_incluidos(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """``por_subtotal`` reparte $300 sobre $1.210 + $2.000 y ``monto_sin_ajustes``
    suma el IVA: los dos quedan ``incluido`` en el costo final de la línea."""
    movs = await _importar(
        db_session,
        sample_tenant.tenant_id,
        multi=multi,
        decision=PurchaseCostDecision(
            context_id=_CTX, base="monto_sin_ajustes", shared_shipping="por_subtotal"
        ),
    )
    vela, difusor = movs["Vela"], movs["Difusor"]
    assert vela.shipping_treatment == difusor.shipping_treatment == "incluido"
    assert vela.tax_treatment == "incluido"
    assert vela.tax_amount == Decimal("21.00")
    # (1000 + 210 + 300 × 1210/3210) / 10 = 132.31
    assert vela.unit_cost_final == Decimal("132.31")
    # Lo facturado sigue intacto en `unit_cost`.
    assert vela.unit_cost == Decimal("100.00")
    # Un IVA de 0 en la celda no se lee como «no aplica»: queda desconocido.
    assert difusor.tax_treatment == "desconocido"
    assert difusor.tax_amount is None


@_CAMINOS
async def test_hoja_sin_ajustes_deja_todo_desconocido(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    cols = {k: v for k, v in _COLS.items() if k not in ("envio", "iva", "nro")}
    movs = await _importar(db_session, sample_tenant.tenant_id, multi=multi, cols=cols)
    vela = movs["Vela"]
    assert vela.cost_contract_version == CONTRATO_VERSION
    assert (vela.shipping_treatment, vela.tax_treatment) == ("desconocido", "desconocido")
    assert vela.unit_cost_final == Decimal("100.00")
    assert vela.document_ref is None


async def _accion(
    db: AsyncSession, tenant: Tenant, user: User, action_type: str, payload: dict[str, Any]
) -> None:
    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type=action_type,
        payload=payload,
        risk_level="MEDIUM",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(action)
    await db.flush()
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await execute_pending_action(action, db)
    await db.flush()


async def _producto(db: AsyncSession, tenant: Tenant) -> Product:
    p = Product(
        id=uuid.uuid4(), tenant_id=tenant.tenant_id, name="Yerba",
        sale_price_ars=Decimal("2500"), unit_cost_ars=Decimal("1500"), stock_units=0,
    )
    db.add(p)
    await db.flush()
    return p


async def test_compra_por_chat_escribe_el_contrato_declarado(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _accion(
        db_session, sample_tenant, sample_user, "REGISTER_PURCHASE",
        {"amount": 1600, "product_id": str(p.id), "qty": 1, "unit_cost": 1600,
         "transaction_date": today_ar().isoformat()},
    )
    mov = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == p.id)
        )
    ).scalar_one()
    assert mov.cost_contract_version == CONTRATO_VERSION
    assert mov.unit_cost_final == Decimal("1600")
    assert (mov.shipping_treatment, mov.tax_treatment) == ("desconocido", "desconocido")


async def test_alta_de_stock_por_chat_es_un_ajuste_manual_con_el_costo_del_producto(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    """M·P2c: ``UPDATE_STOCK`` («sumá 5») no es una compra: deja un ajuste manual
    (no un ``purchase``), con el costo del producto declarado como contrato, igual
    que la corrección de stock de la pantalla de productos."""
    p = await _producto(db_session, sample_tenant)
    await _accion(
        db_session, sample_tenant, sample_user, "UPDATE_STOCK",
        {"product_id": str(p.id), "qty_change": 5},
    )
    mov = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == p.id)
        )
    ).scalar_one()
    assert (mov.movement_type, mov.source_type, mov.qty) == (
        "adjustment", "manual_adjustment", 5,
    )
    assert mov.unit_cost_final == Decimal("1500")
    assert mov.shipping_treatment == "desconocido"
    assert p.stock_units == 5


async def test_baja_de_stock_por_chat_es_un_ajuste_manual_no_una_venta(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _accion(
        db_session, sample_tenant, sample_user, "UPDATE_STOCK",
        {"product_id": str(p.id), "qty_change": 5},
    )
    await _accion(
        db_session, sample_tenant, sample_user, "UPDATE_STOCK",
        {"product_id": str(p.id), "qty_change": -2},
    )
    movs = (
        await db_session.execute(
            select(InventoryMovement.movement_type, InventoryMovement.qty).where(
                InventoryMovement.product_id == p.id
            )
        )
    ).all()
    assert sorted(m.qty for m in movs) == [-2, 5]
    assert {m.movement_type for m in movs} == {"adjustment"}
    assert p.stock_units == 3


async def test_stock_fijado_por_chat_con_update_product_deja_el_delta(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _accion(
        db_session, sample_tenant, sample_user, "UPDATE_PRODUCT",
        {"product_id": str(p.id), "stock_units": 12, "sale_price_ars": 2600},
    )
    mov = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == p.id)
        )
    ).scalar_one()
    assert (mov.source_type, mov.qty) == ("manual_adjustment", 12)
    assert p.stock_units == 12
    assert p.sale_price_ars == Decimal("2600")


async def test_compra_por_chat_con_costo_negativo_no_rompe_ni_inventa_contrato(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: User
) -> None:
    """Revisión M·1b: un ``unit_cost`` negativo del LLM no es un costo. Antes de
    M·1b se guardaba igual; ahora no puede convertirse en un 500 a mitad de la
    acción (con el gasto ya creado) ni en un contrato: queda sin contrato."""
    p = await _producto(db_session, sample_tenant)
    await _accion(
        db_session, sample_tenant, sample_user, "REGISTER_PURCHASE",
        {"amount": 1600, "product_id": str(p.id), "qty": 1, "unit_cost": -5,
         "transaction_date": today_ar().isoformat()},
    )
    mov = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.product_id == p.id)
        )
    ).scalar_one()
    assert mov.cost_contract_version is None
    assert mov.unit_cost_final is None
