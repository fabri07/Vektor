"""E0-B-1 — el recálculo del score sale DESPUÉS del commit del request.

Gastos (alta, edición, anulación), compra manual, productos (alta, edición,
baja) y el submit del onboarding encolaban ``trigger_score_recalculation.delay()``
en el medio del handler: el commit lo hace ``get_db_session`` recién al salir, y
el worker —que abre su propia sesión— podía leer la base sin el cambio y
persistir un score viejo hasta la próxima escritura (Beat no está desplegado).
Además el ``.delay()`` iba sin ``try``: con el broker caído, el gasto del usuario
se revertía con un 500 por un recálculo que es accesorio.

Por qué un cliente propio y no el ``client`` de la suite
--------------------------------------------------------
``client`` pisa ``get_db_session`` por una sesión que NO commitea: ningún
``after_commit`` corre dentro de un request, así que con él el recálculo nunca
saldría y no habría nada que medir. ``cliente_que_commitea`` hace lo mismo que
``get_db_session`` (commit al salir, rollback ante error) sobre la sesión del
test, y el ``.delay`` interceptado anota si en ese momento la transacción seguía
abierta. Con el ``.delay()`` directo de antes, la anota abierta.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.main import create_app
from app.persistence.db.session import get_db_session
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

pytestmark = pytest.mark.asyncio

_HOY = f"{today_ar().isoformat()}T10:00:00"


@dataclass
class Encolado:
    tenant_id: str
    motivo: str
    transaccion_abierta: bool


@pytest.fixture
def encolados(
    mock_score_trigger: MagicMock, db_session: AsyncSession
) -> list[Encolado]:
    """Cada ``.delay`` del recálculo, con el estado de la transacción en ese momento."""
    lista: list[Encolado] = []

    def _anotar(tenant_id: str, motivo: str) -> None:
        lista.append(Encolado(tenant_id, motivo, db_session.in_transaction()))

    mock_score_trigger.side_effect = _anotar
    return lista


@pytest_asyncio.fixture
async def cliente_que_commitea(
    db_session: AsyncSession, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient, None]:
    """Como ``client``, pero la sesión commitea al salir, igual que ``get_db_session``."""
    from app.jobs.celery_app import celery_app  # noqa: PLC0415
    from app.main import limiter  # noqa: PLC0415
    from app.persistence.db.redis_client import get_redis  # noqa: PLC0415

    # Los eventos del bus (stock) también salen en el `after_commit`: sin broker
    # en la suite, que no paguen reintentos de kombu.
    monkeypatch.setattr(celery_app, "send_task", MagicMock())
    limiter._storage.reset()
    app = create_app()

    async def _sesion() -> AsyncGenerator[AsyncSession, None]:
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    async def _redis() -> AsyncGenerator[Any, None]:
        yield fake_redis

    app.dependency_overrides[get_db_session] = _sesion
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _despues_del_commit(encolados: list[Encolado], tenant: Tenant, motivo: str) -> None:
    assert [(e.tenant_id, e.motivo) for e in encolados] == [
        (str(tenant.tenant_id), motivo)
    ], encolados
    assert not encolados[0].transaccion_abierta, (
        f"{motivo}: el recálculo se encoló con la transacción del request abierta; "
        "el worker puede leer la base sin el cambio."
    )


_GASTO = {
    "amount": "500000.00",
    "category": "OTHER",
    "expense_date": today_ar().isoformat(),
    "description": "Alquiler",
}


# ── Gastos ───────────────────────────────────────────────────────────────────
async def test_el_alta_de_un_gasto_recalcula_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    encolados: list[Encolado],
) -> None:
    resp = await cliente_que_commitea.post(
        "/api/v1/expenses", json=_GASTO, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    _despues_del_commit(encolados, sample_tenant, "expense_entry_created")


async def test_editar_y_anular_un_gasto_recalculan_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    encolados: list[Encolado],
) -> None:
    gid = (
        await cliente_que_commitea.post("/api/v1/expenses", json=_GASTO, headers=auth_headers)
    ).json()["id"]
    encolados.clear()

    resp = await cliente_que_commitea.patch(
        f"/api/v1/expenses/{gid}",
        json={**_GASTO, "description": "Alquiler local"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    _despues_del_commit(encolados, sample_tenant, "expense_entry_updated")
    encolados.clear()

    resp = await cliente_que_commitea.delete(f"/api/v1/expenses/{gid}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    _despues_del_commit(encolados, sample_tenant, "expense_entry_deleted")


async def test_un_broker_caido_no_revierte_el_gasto(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    mock_score_trigger: MagicMock,
    db_session: AsyncSession,
) -> None:
    """Antes el ``.delay()`` iba sin ``try`` dentro del handler: un broker caído
    devolvía 500 y revertía el gasto, invitando al dueño a cargarlo de nuevo."""
    mock_score_trigger.side_effect = ConnectionError("redis caído")
    resp = await cliente_que_commitea.post(
        "/api/v1/expenses", json=_GASTO, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    mock_score_trigger.assert_called_once()

    db_session.expunge_all()
    gastos = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert [g.amount for g in gastos] == [Decimal("500000.00")]


# ── Compra manual ────────────────────────────────────────────────────────────
async def test_la_compra_manual_recalcula_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
    encolados: list[Encolado],
) -> None:
    producto = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Yerba 1kg",
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("0"),
        stock_units=0,
        provenance="REAL",
    )
    db_session.add(producto)
    await db_session.commit()
    proveedor = await cliente_que_commitea.post(
        "/api/v1/suppliers", json={"name": "Distribuidora Norte"}, headers=auth_headers
    )
    assert proveedor.status_code == 201, proveedor.text
    encolados.clear()

    resp = await cliente_que_commitea.post(
        "/api/v1/purchases/manual",
        json={
            "supplier_id": str(proveedor.json()["id"]),
            "transaction_date": _HOY,
            "payment_method": "transfer",
            "lines": [
                {
                    "product_id": str(producto.id),
                    "unit_cost": "100.00",
                    "quantity": 10,
                    "sale_price_ars": "500",
                }
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    _despues_del_commit(encolados, sample_tenant, "manual_purchase_created")


# ── Productos ────────────────────────────────────────────────────────────────
async def test_alta_edicion_y_baja_de_producto_recalculan_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    encolados: list[Encolado],
) -> None:
    resp = await cliente_que_commitea.post(
        "/api/v1/products",
        json={"name": "Galletitas", "sale_price_ars": "1000.00"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    _despues_del_commit(encolados, sample_tenant, "product_created")
    encolados.clear()

    resp = await cliente_que_commitea.patch(
        f"/api/v1/products/{pid}", json={"sale_price_ars": "1200.00"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    _despues_del_commit(encolados, sample_tenant, "product_updated")
    encolados.clear()

    resp = await cliente_que_commitea.delete(f"/api/v1/products/{pid}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    _despues_del_commit(encolados, sample_tenant, "product_deleted")


# ── Onboarding ───────────────────────────────────────────────────────────────
async def test_el_primer_score_del_onboarding_sale_despues_del_commit(
    cliente_que_commitea: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    encolados: list[Encolado],
) -> None:
    """El submit tomaba el perfil con ``FOR UPDATE`` y encolaba sin commitear: el
    primer score del negocio podía calcularse sin las estimaciones ni el capital."""
    resp = await cliente_que_commitea.post(
        "/api/v1/onboarding/submit",
        json={
            "weekly_sales_estimate_ars": 50000,
            "monthly_inventory_cost_ars": 80000,
            "monthly_fixed_expenses_ars": 30000,
            "cash_on_hand_ars": 20000,
            "product_count_estimate": 10,
            "supplier_count_estimate": 3,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    # El mensaje es el de siempre: `(tenant_id, snapshot_id)`.
    _despues_del_commit(encolados, sample_tenant, str(uuid.UUID(resp.json()["snapshot_id"])))
