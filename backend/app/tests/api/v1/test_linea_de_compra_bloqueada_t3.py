"""T3 · B-3: una línea de compra no se anula ni se edita como gasto suelto.

Anular o editar el gasto de una compra dejaba vivo su movimiento ``purchase``:
stock, costo y respaldo de ventas como si la compra existiera. Hasta que exista
la anulación de la compra entera, se bloquea (decisión del dueño, 2026-10-26):

- ``DELETE /expenses/{id}`` de una línea (o del envío aparte) → 409 ``PURCHASE_LINE``.
- ``PATCH`` que cambia monto, categoría, tipo o fecha → 409; editar descripción,
  proveedor o notas (mandando monto, categoría y fecha iguales, como la pantalla)
  sigue andando.
- ``RECLASSIFY_EXPENSE`` del chat no la saca de mercadería.
- Un gasto operativo común no cambia.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import (
    ReclassifyPurchaseLineError,
    execute_pending_action,
)
from app.domain.business_time import today_ar
from app.domain.linea_de_compra import es_linea_de_compra
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_HOY = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada escritura paga reintentos de kombu."""


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


async def _compra_manual(
    client: AsyncClient, headers: dict[str, str], producto: Product
) -> None:
    """«Registrar compra»: 50 × $100 con envío de $30 aparte."""
    resp = await client.post(
        "/api/v1/suppliers", json={"name": f"Distribuidora {producto.name}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/v1/purchases/manual",
        json={
            "supplier_id": str(resp.json()["id"]),
            "transaction_date": _HOY,
            "payment_method": "transfer",
            "shipping": {"amount": "30.00", "mode": "gasto_aparte"},
            "lines": [
                {
                    "product_id": str(producto.id),
                    "unit_cost": "100.00",
                    "quantity": 50,
                    "sale_price_ars": "500",
                }
            ],
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text


async def _gastos_de_la_compra(
    db: AsyncSession, tid: uuid.UUID
) -> tuple[ExpenseEntry, ExpenseEntry]:
    db.expire_all()
    gastos = list(
        (
            await db.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
        ).scalars()
    )
    linea = next(g for g in gastos if g.category == "INVENTORY")
    envio = next(g for g in gastos if g.category == "LOGISTICS")
    return linea, envio


async def _movimientos_vivos(db: AsyncSession, product_id: uuid.UUID) -> list[InventoryMovement]:
    db.expire_all()
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.product_id == product_id,
                    InventoryMovement.movement_type == "purchase",
                    InventoryMovement.voided_at.is_(None),
                )
            )
        ).scalars()
    )


async def _gasto(db: AsyncSession, gid: uuid.UUID) -> ExpenseEntry:
    db.expire_all()
    g = (await db.execute(select(ExpenseEntry).where(ExpenseEntry.id == gid))).scalar_one()
    return g


def _como_la_pantalla(gasto: dict[str, Any], **cambios: Any) -> dict[str, Any]:
    """Lo que manda `updateMutation` de `expenses/page.tsx`: SIEMPRE monto,
    categoría y fecha, aunque sólo se haya tocado la descripción."""
    cuerpo = {
        "amount": float(gasto["amount"]),
        "category": gasto["category"],
        "tax_kind": None,
        "expense_date": gasto["transaction_date"],
        "description": gasto["description"],
        "is_recurring": gasto["is_recurring"],
        "payment_method": gasto["payment_method"],
        "supplier_name": gasto["supplier_name"],
        "notes": gasto["notes"],
    }
    cuerpo.update(cambios)
    return cuerpo


def _es_409_de_compra(resp: Any) -> None:
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "PURCHASE_LINE"
    assert "ajuste de stock" in detail["message"]
    assert "anulación de compras todavía no existe" in detail["message"]


# ── DELETE ────────────────────────────────────────────────────────────────────


async def test_anular_una_linea_de_compra_da_409_y_la_compra_sigue_entera(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Yerba")
    pid = p.id
    await _compra_manual(client, auth_headers, p)
    linea, _envio = await _gastos_de_la_compra(db_session, tid)
    linea_id = linea.id

    resp = await client.delete(f"/api/v1/expenses/{linea_id}", headers=auth_headers)

    _es_409_de_compra(resp)
    assert (await _gasto(db_session, linea_id)).voided_at is None
    movs = await _movimientos_vivos(db_session, pid)
    assert len(movs) == 1 and movs[0].qty == 50
    db_session.expire_all()
    producto = await db_session.get(Product, pid)
    assert producto is not None and producto.stock_units == 50


async def test_anular_el_envio_aparte_de_una_compra_tambien_da_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El envío es OPEX y no toca el costo, pero es parte del comprobante:
    anularlo suelto dejaría la compra a medias."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Azúcar")
    await _compra_manual(client, auth_headers, p)
    _linea, envio = await _gastos_de_la_compra(db_session, tid)

    resp = await client.delete(f"/api/v1/expenses/{envio.id}", headers=auth_headers)

    _es_409_de_compra(resp)
    assert (await _gasto(db_session, envio.id)).voided_at is None


async def test_el_editor_de_campos_no_le_saca_el_grupo_al_envio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El PATCH reemplaza `custom_fields` entero: sin conservar el grupo, un
    `{}` dejaba al envío como gasto suelto y anulable."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Aceite")
    await _compra_manual(client, auth_headers, p)
    _linea, envio = await _gastos_de_la_compra(db_session, tid)
    envio_id = envio.id

    resp = await client.patch(
        f"/api/v1/expenses/{envio_id}", json={"custom_fields": {}}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_purchase_line"] is True

    resp = await client.delete(f"/api/v1/expenses/{envio_id}", headers=auth_headers)
    _es_409_de_compra(resp)
    assert (await _gasto(db_session, envio_id)).voided_at is None


# ── PATCH ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cambio",
    [
        {"amount": 1234.0},
        {"category": "SUPPLIES"},
        {"expense_date": f"{today_ar().isoformat()}T08:15"},
    ],
    ids=["monto", "categoria", "fecha"],
)
async def test_editar_monto_categoria_o_fecha_de_una_linea_de_compra_da_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    cambio: dict[str, Any],
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Fideos")
    await _compra_manual(client, auth_headers, p)
    linea, _envio = await _gastos_de_la_compra(db_session, tid)
    actual = (await client.get(f"/api/v1/expenses/{linea.id}", headers=auth_headers)).json()
    assert actual["is_purchase_line"] is True

    resp = await client.patch(
        f"/api/v1/expenses/{linea.id}",
        json=_como_la_pantalla(actual, description="otra cosa", **cambio),
        headers=auth_headers,
    )

    _es_409_de_compra(resp)
    g = await _gasto(db_session, linea.id)
    assert g.amount == Decimal("5000.00")
    assert g.category == "INVENTORY"
    assert g.expense_type == "COGS"
    assert g.transaction_date == datetime.fromisoformat(_HOY)
    assert g.description != "otra cosa"


async def test_cambiar_el_tipo_de_una_linea_de_compra_da_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Arroz")
    await _compra_manual(client, auth_headers, p)
    linea, _envio = await _gastos_de_la_compra(db_session, tid)

    resp = await client.patch(
        f"/api/v1/expenses/{linea.id}", json={"expense_type": "OPEX"}, headers=auth_headers
    )

    _es_409_de_compra(resp)
    assert (await _gasto(db_session, linea.id)).expense_type == "COGS"


async def test_editar_solo_descripcion_proveedor_y_notas_de_una_linea_de_compra_anda(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La pantalla manda monto, categoría y fecha iguales: no es un cambio."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Galletitas")
    await _compra_manual(client, auth_headers, p)
    linea, _envio = await _gastos_de_la_compra(db_session, tid)
    actual = (await client.get(f"/api/v1/expenses/{linea.id}", headers=auth_headers)).json()

    resp = await client.patch(
        f"/api/v1/expenses/{linea.id}",
        json=_como_la_pantalla(
            actual,
            description="Compra de galletitas (factura 12)",
            supplier_name="Distribuidora Norte",
            notes="revisar factura",
        ),
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    g = await _gasto(db_session, linea.id)
    assert g.description == "Compra de galletitas (factura 12)"
    assert g.supplier_name == "Distribuidora Norte"
    assert g.notes == "revisar factura"
    assert g.amount == Decimal("5000.00")
    assert g.category == "INVENTORY"
    assert (g.custom_fields or {}).get("purchase_group_id")


async def test_linea_importada_bloqueada_y_la_fecha_del_editor_se_compara_al_minuto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una línea de un libro de compras importado (sin grupo, con producto y
    archivo) también es línea de compra. La base guarda segundos; el
    `datetime-local` del editor manda minutos: guardar sólo la descripción no es
    un cambio de fecha."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Lavandina")
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="compras.xlsx",
        s3_key="compras.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ingestion",
    )
    db_session.add(f)
    g = ExpenseEntry(
        tenant_id=tid,
        product_id=p.id,
        amount=Decimal("700.00"),
        category="INVENTORY",
        expense_type="COGS",
        transaction_date=datetime(2026, 10, 1, 9, 30, 27, 500000),
        description="Lavandina x 7",
        payment_method="cash",
        provenance="REAL",
        source_upload_id=f.id,
        source_row_ref="sheet:Compras:row:3",
    )
    db_session.add(g)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/expenses/{g.id}", headers=auth_headers)
    _es_409_de_compra(resp)

    actual = (await client.get(f"/api/v1/expenses/{g.id}", headers=auth_headers)).json()
    resp = await client.patch(
        f"/api/v1/expenses/{g.id}",
        json=_como_la_pantalla(actual, expense_date="2026-10-01T09:30", description="Lavandina"),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    guardado = await _gasto(db_session, g.id)
    assert guardado.description == "Lavandina"
    assert guardado.voided_at is None
    assert guardado.transaction_date == datetime(2026, 10, 1, 9, 30, 27, 500000)


# ── Un gasto operativo común no cambia ────────────────────────────────────────


async def test_un_gasto_operativo_comun_se_edita_y_se_anula_como_siempre(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    resp = await client.post(
        "/api/v1/expenses",
        json={
            "amount": "15000.00",
            "category": "RENT",
            "expense_date": _HOY,
            "description": "Alquiler",
            "payment_method": "transfer",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    gid = resp.json()["id"]
    assert resp.json()["is_purchase_line"] is False

    resp = await client.patch(
        f"/api/v1/expenses/{gid}",
        json=_como_la_pantalla(resp.json(), amount=16000.0, category="UTILITIES"),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    g = await _gasto(db_session, uuid.UUID(gid))
    assert g.amount == Decimal("16000.00")
    assert g.category == "UTILITIES"

    resp = await client.delete(f"/api/v1/expenses/{gid}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert (await _gasto(db_session, uuid.UUID(gid))).voided_at is not None


# ── Chat: RECLASSIFY_EXPENSE ──────────────────────────────────────────────────


def _accion(tid: uuid.UUID, payload: dict[str, object]) -> PendingAction:
    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = tid
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.RECLASSIFY_EXPENSE
    action.payload = payload
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None
    return action


async def test_el_chat_no_saca_de_mercaderia_una_linea_de_compra(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Reclasificación masiva con un gasto común y una línea de compra: falla
    entera con mensaje para el usuario y no toca ninguno."""
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, "Harina")
    pid = p.id
    await _compra_manual(client, auth_headers, p)
    linea, _envio = await _gastos_de_la_compra(db_session, tid)
    comun = ExpenseEntry(
        tenant_id=tid,
        amount=Decimal("900"),
        category="OTHER",
        expense_type="OPEX",
        transaction_date=datetime.fromisoformat(_HOY),
        description="bolsas",
        payment_method="cash",
        provenance="REAL",
    )
    db_session.add(comun)
    await db_session.commit()
    linea_id, comun_id = linea.id, comun.id

    accion = _accion(
        tid,
        {"expense_ids": [str(comun_id), str(linea_id)], "target": "insumo", "category": "SUPPLIES"},
    )
    with pytest.raises(ReclassifyPurchaseLineError) as exc:
        await execute_pending_action(accion, db_session)

    assert "compra de mercadería" in exc.value.user_message
    assert (await _gasto(db_session, linea_id)).category == "INVENTORY"
    assert (await _gasto(db_session, linea_id)).expense_type == "COGS"
    assert (await _gasto(db_session, comun_id)).category == "OTHER"
    assert len(await _movimientos_vivos(db_session, pid)) == 1


# ── La definición ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({"product_id": uuid.uuid4(), "category": "INVENTORY", "expense_type": "COGS"}, True),
        # Categoría legacy de mercadería, con producto.
        ({"product_id": uuid.uuid4(), "category": "mercaderia", "expense_type": None}, True),
        # Envío aparte de una compra: sin producto, pero del grupo.
        (
            {
                "product_id": None,
                "category": "LOGISTICS",
                "expense_type": "OPEX",
                "custom_fields": {"purchase_group_id": str(uuid.uuid4())},
            },
            True,
        ),
        # Mercadería sin producto (legado de antes de B·5b): no tiene movimiento.
        ({"product_id": None, "category": "INVENTORY", "expense_type": "COGS"}, False),
        ({"product_id": None, "category": "RENT", "expense_type": "OPEX"}, False),
    ],
    ids=["linea", "categoria-legacy", "envio-del-grupo", "mercaderia-sin-producto", "opex"],
)
def test_es_linea_de_compra(campos: dict[str, Any], esperado: bool) -> None:
    assert (
        es_linea_de_compra(
            product_id=campos["product_id"],
            category=campos["category"],
            expense_type=campos["expense_type"],
            custom_fields=campos.get("custom_fields"),
        )
        is esperado
    )
