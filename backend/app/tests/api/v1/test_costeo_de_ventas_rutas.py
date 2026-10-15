"""M·2a — el costo congelado contra las rutas reales que crean ventas.

El listener costea en el commit de ``get_db_session`` (que el fixture ``client``
no hace: ``_ventas`` lo hace en su lugar). Acá se prueba que cada ruta llega
costeada sin llamar a nada, que el reintento idempotente del POS no
recalcula y que el costo no sale en las respuestas.
"""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import now_ar_naive, today_ar
from app.persistence.models.transaction import SaleEntry


def _ahora() -> str:
    """El momento de la venta, calculado EN el test y no al importar el módulo.

    Una constante de módulo se fija al recolectar: si la suite cruza la
    medianoche argentina, la venta queda en el día anterior al alta del producto
    y su costo (sellado con la hora del alta) es POSTERIOR a la venta, así que
    —con razón— no se usa y la venta sale ``sin_costo`` (falló así en el CI del
    2026-10-12 a las 00:08). Tampoco una hora fija: a las 00:05 sería futura.
    """
    return now_ar_naive().replace(microsecond=0).isoformat()


async def _producto(client: AsyncClient, headers: dict[str, Any], costo: str = "60.00") -> str:
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": f"Vela {uuid.uuid4().hex[:6]}",
            "sale_price_ars": "100.00",
            "unit_cost_ars": costo,
            "stock_units": 50,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _ventas(db: AsyncSession, product_id: str) -> list[SaleEntry]:
    # El fixture ``client`` comparte la sesión del test y NO confirma: el commit
    # de ``get_db_session`` en producción es este.
    await db.commit()
    db.expire_all()
    return list(
        (
            await db.execute(
                select(SaleEntry).where(SaleEntry.product_id == uuid.UUID(product_id))
            )
        ).scalars()
    )


def _sin_costo_en(cuerpo: Any) -> None:
    texto = str(cuerpo)
    for clave in ("cost_ars", "cost_unit_ars", "cost_origin", "cost_confirmed"):
        assert clave not in texto


class TestRutas:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> Any:
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_venta_pos_queda_costeada_y_el_reintento_no_recalcula(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await _producto(client, auth_headers)
        cuerpo = {
            "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
            "operation_date": _ahora(),
            "items": [{"product_id": pid, "quantity": 3}],
            "tenders": [{"payment_method": "cash", "amount_ars": "300.00"}],
        }
        resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        _sin_costo_en(resp.json())

        [venta] = await _ventas(db_session, pid)
        # M·P2c: el alta con stock deja una apertura (como un catálogo importado
        # en «ya la tenía»), así que el costo sale del stock inicial. Envío
        # desconocido: estimación, nunca confirmada.
        assert venta.cost_origin == "stock_inicial"
        assert venta.cost_unit_ars == 60
        assert venta.cost_ars == 180
        assert venta.cost_confirmed is False

        # Cambia el costo y se reintenta la MISMA operación (respuesta perdida).
        await client.patch(
            f"/api/v1/products/{pid}", json={"unit_cost_ars": "90.00"}, headers=auth_headers
        )
        reintento = await client.post(
            "/api/v1/pos/operations", json=cuerpo, headers=auth_headers
        )
        assert reintento.status_code in (200, 201), reintento.text
        [misma] = await _ventas(db_session, pid)
        assert misma.id == venta.id
        assert misma.cost_unit_ars == 60

    async def test_post_sales_queda_costeada(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await _producto(client, auth_headers, costo="40.00")
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "200.00",
                "quantity": 2,
                "product_id": pid,
                "transaction_date": _ahora(),
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        _sin_costo_en(resp.json())
        [venta] = await _ventas(db_session, pid)
        assert venta.cost_ars == 80
        assert venta.cost_snapshot_version == 1

    async def test_sales_bulk_queda_costeada(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        """``period_date`` es un ``date``: el costeo no puede romperse con eso."""
        pid = await _producto(client, auth_headers, costo="40.00")
        resp = await client.post(
            "/api/v1/sales/bulk",
            json={
                "period_type": "daily",
                "period_date": today_ar().isoformat(),
                "total_amount_ars": "300.00",
                "entries": [{"product_id": pid, "quantity": 3, "amount_ars": "300.00"}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        [venta] = await _ventas(db_session, pid)
        assert venta.cost_snapshot_version == 1
        assert venta.cost_ars == 120

    async def test_el_cliente_no_puede_mandar_el_costo(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await _producto(client, auth_headers, costo="40.00")
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "100.00",
                "quantity": 1,
                "product_id": pid,
                "transaction_date": _ahora(),
                "payment_method": "cash",
                "cost_ars": "1.00",
                "cost_unit_ars": "1.00",
                "cost_confirmed": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code in (201, 422), resp.text
        for venta in await _ventas(db_session, pid):
            assert venta.cost_unit_ars == 40
            assert venta.cost_confirmed is False
