"""Ticket y reimpresión (B12).

Lo que se protege: que el ticket diga lo que se cobró (nombres, pagos, vuelto)
sin costos, que reimprimir no cree ni audite nada, y que un cajero no pueda
leer ni listar los tickets de otro cajero.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.tenant import Tenant
from app.tests.api.v1.test_cashier_access import (
    CLAVES_PROHIBIDAS,
    _cajero,
    _producto,
    _sin_celery,  # noqa: F401 — fixture autouse: sin broker en los cobros
    _venta,
)


def _claves(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _claves(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _claves(v)}
    return set()


async def _cobrar(client: AsyncClient, headers: dict[str, str], cuerpo: dict[str, Any]) -> str:
    resp = await client.post("/api/v1/pos/operations", json=cuerpo, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestRecibo:
    async def test_dice_lo_que_se_cobro_sin_costos(
        self, client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba 1kg", price="1000.00")
        op = await _cobrar(
            client,
            auth_headers,
            _venta(
                p["id"],
                items=[{"product_id": p["id"], "quantity": 2, "discount_ars": "50.00"}],
                tenders=[{"payment_method": "cash", "amount_ars": "1950.00"}],
                cash_received_ars="2000.00",
            ),
        )
        resp = await client.get(f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        r = resp.json()
        assert r["business_name"] == sample_tenant.display_name
        assert r["number"] == op.replace("-", "")[:8].upper()
        assert r["status"] == "COMPLETED"
        [linea] = r["lines"]
        assert linea["product_name"] == "Yerba 1kg"
        assert linea["quantity"] == 2
        assert linea["quantity_display"].startswith("2")
        assert linea["gross_ars"] == "2000.00"
        assert linea["discount_line_ars"] == "50.00"
        assert linea["line_total_ars"] == "1950.00"
        assert r["tenders"] == [{"payment_method": "cash", "amount_ars": "1950.00"}]
        assert r["total_ars"] == "1950.00"
        assert r["cash_change_ars"] == "50.00"
        # Venta sin cliente: queda en el centinela "Local", que no se imprime.
        assert r["customer_name"] is None
        assert r["cashier_name"]
        assert _claves(r).isdisjoint(CLAVES_PROHIBIDAS)

    async def test_las_lineas_suman_el_subtotal_con_descuento_global(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """El global va una vez, en los totales: bruto − descuento de línea suma el subtotal."""
        a = await _producto(client, auth_headers, "A", price="100.00")
        b = await _producto(client, auth_headers, "B", price="33.00")
        op = await _cobrar(
            client,
            auth_headers,
            _venta(
                a["id"],
                items=[
                    {"product_id": a["id"], "quantity": 3, "discount_ars": "10.00"},
                    {"product_id": b["id"], "quantity": 1},
                ],
                discount_ars="1.00",
                tenders=[{"payment_method": "cash", "amount_ars": "322.00"}],
            ),
        )
        r = (await client.get(f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers)).json()
        neto_antes_del_global = sum(
            Decimal(ln["gross_ars"]) - Decimal(ln["discount_line_ars"]) for ln in r["lines"]
        )
        assert neto_antes_del_global == Decimal(r["subtotal_ars"]) == Decimal("323.00")
        assert Decimal(r["subtotal_ars"]) - Decimal(r["discount_ars"]) == Decimal(r["total_ars"])
        assert sum(Decimal(ln["line_total_ars"]) for ln in r["lines"]) == Decimal(r["total_ars"])

    async def test_un_ticket_anulado_se_reimprime_como_anulado(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        op = await _cobrar(client, auth_headers, _venta(p["id"]))
        anulada = await client.post(
            f"/api/v1/pos/operations/{op}/void", json={"reason": "error"}, headers=auth_headers
        )
        assert anulada.status_code == 200, anulada.text
        resp = await client.get(f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers)
        assert resp.json()["status"] == "VOIDED"

    async def test_reimprimir_no_crea_ni_audita_nada(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        op = await _cobrar(client, auth_headers, _venta(p["id"]))

        async def contar() -> tuple[int, int]:
            ops = await db_session.scalar(select(func.count()).select_from(PosOperation))
            aud = await db_session.scalar(select(func.count()).select_from(DecisionAuditLog))
            return int(ops or 0), int(aud or 0)

        antes = await contar()
        for _ in range(3):
            await client.get(f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers)
            await client.get("/api/v1/pos/operations", headers=auth_headers)
        assert await contar() == antes

    async def test_de_otro_negocio_es_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        second_auth_headers: dict[str, str],
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        op = await _cobrar(client, auth_headers, _venta(p["id"]))
        resp = await client.get(f"/api/v1/pos/operations/{op}/receipt", headers=second_auth_headers)
        assert resp.status_code == 404
        lista = await client.get("/api/v1/pos/operations", headers=second_auth_headers)
        assert lista.json() == []


class TestCajero:
    async def test_ve_sus_tickets_y_no_los_de_otro_cajero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, ana = await _cajero(db_session, sample_tenant, fake_redis)
        _, beto = await _cajero(db_session, sample_tenant, fake_redis)
        de_ana = await _cobrar(client, ana, _venta(p["id"]))
        de_beto = await _cobrar(client, beto, _venta(p["id"]))

        propio = await client.get(f"/api/v1/pos/operations/{de_ana}/receipt", headers=ana)
        assert propio.status_code == 200, propio.text
        assert propio.json()["terminal_name"], "cobró desde una caja: tiene que figurar"
        ajeno = await client.get(f"/api/v1/pos/operations/{de_beto}/receipt", headers=ana)
        assert ajeno.status_code == 404

        lista = await client.get("/api/v1/pos/operations", headers=ana)
        assert lista.status_code == 200, lista.text
        assert [t["id"] for t in lista.json()] == [de_ana]

    async def test_el_duenio_lista_todos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        p = await _producto(client, auth_headers, "Yerba")
        _, ana = await _cajero(db_session, sample_tenant, fake_redis)
        de_ana = await _cobrar(client, ana, _venta(p["id"]))
        del_duenio = await _cobrar(client, auth_headers, _venta(p["id"]))
        lista = await client.get("/api/v1/pos/operations", headers=auth_headers)
        # Como conjunto: dos cobros en el mismo instante empatan en `created_at`.
        assert {t["id"] for t in lista.json()} == {del_duenio, de_ana}
