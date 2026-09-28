"""Salida pendiente de stock: capturarla y resolverla (B11).

Lo que se protege acá: que una venta ya cobrada sin conexión **entre igual**
aunque el stock no alcance —rechazarla no devuelve ni la plata ni la
mercadería—, que eso NO se relaje sin dejar rastro, y que los **dos** chequeos
de inventario (el agregado y el temporal) entiendan lo mismo.
"""

import unittest.mock
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_integrity_service import (
    check_tenant_inventory_integrity,
)
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
)
from app.application.services.inventory_temporal_service import (
    check_products_temporal_divergence,
)
from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.tests.conftest import TEST_PIN, _pin_window_key
from app.utils.security import create_access_token, hash_password

_FECHA = f"{today_ar().isoformat()}T10:00:00"
_COBRADO = f"{today_ar().isoformat()}T09:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    """Cabeceras de una caja habilitada: `X-POS-Terminal` además del token."""
    alta = await client.post(
        "/api/v1/pos/terminals", json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


async def _producto(
    client: AsyncClient, headers: dict[str, str], *, stock: int, price: str = "100.00"
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": f"Yerba {uuid.uuid4().hex[:5]}",
            "sale_price_ars": price,
            "stock_units": stock,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _venta(items: list[dict[str, Any]], total: str, **extra: Any) -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "charged_at": _COBRADO,
        "items": items,
        "tenders": [{"payment_method": "cash", "amount_ars": total}],
        **extra,
    }


async def _stock(db_session: AsyncSession, pid: str) -> int:
    producto = (
        await db_session.execute(select(Product).where(Product.id == uuid.UUID(pid)))
    ).scalar_one()
    await db_session.refresh(producto)
    return producto.stock_units


async def _anclar(db_session: AsyncSession, tenant_id: uuid.UUID, pid: str, qty: int) -> None:
    """Stock inicial de catálogo: sin ancla, los chequeos saltean el producto."""
    db_session.add(
        InventoryMovement(
            tenant_id=tenant_id,
            product_id=uuid.UUID(pid),
            movement_type="adjustment",
            qty=qty,
            source_type=SOURCE_CATALOG_INITIAL_STOCK,
        )
    )
    await db_session.flush()


class TestCaptura:
    async def test_sin_el_header_sigue_rechazando_por_stock(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """El camino online no cambia: el cajero ve el error y decide."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers, stock=3)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 5}], "500.00"),
            headers=caja,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "INSUFFICIENT_STOCK"
        assert (await db_session.execute(select(PosOperation))).first() is None

    async def test_con_el_header_la_venta_entra_y_queda_la_deuda(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers, stock=3)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 5}], "500.00"),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["total_ars"] == "500.00"

        # El stock no se toca: la deuda queda anotada, no se fuerza a negativo.
        assert await _stock(db_session, pid) == 3
        deuda = (
            await db_session.execute(select(PosStockDiscrepancy))
        ).scalar_one()
        # La salida PENDIENTE es la cantidad completa, no el faltante de 2.
        assert deuda.pending_units == 5
        assert deuda.stock_at_capture == 3
        assert deuda.status == "OPEN"
        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_STOCK_CAPTURED"
                )
            )
        ).scalar_one()
        assert auditoria.decision_data["pending_units"] == 5
        assert auditoria.decision_data["charged_at"] is not None

    async def test_la_linea_que_alcanza_descuenta_y_la_que_no_no(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        caja = await _caja(client, auth_headers)
        alcanza = await _producto(client, auth_headers, stock=10)
        no_alcanza = await _producto(client, auth_headers, stock=1)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(
                [
                    {"product_id": alcanza, "quantity": 2},
                    {"product_id": no_alcanza, "quantity": 4},
                ],
                "600.00",
            ),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 201, resp.text
        assert await _stock(db_session, alcanza) == 8
        assert await _stock(db_session, no_alcanza) == 1
        deudas = (await db_session.execute(select(PosStockDiscrepancy))).scalars().all()
        assert len(deudas) == 1
        assert str(deudas[0].product_id) == no_alcanza

    async def test_sin_decir_cuando_se_cobro_no_se_captura(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """El header solo no alcanza: la afirmación tiene que estar fechada."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers, stock=3)
        cuerpo = _venta([{"product_id": pid, "quantity": 5}], "500.00")
        del cuerpo["charged_at"]
        resp = await client.post(
            "/api/v1/pos/operations", json=cuerpo, headers={**caja, "X-POS-Offline": "1"}
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "OFFLINE_WITHOUT_CHARGED_AT"

    async def test_un_cero_en_el_header_no_es_un_si(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """`bool("0")` es True en Python, y ese header relaja el invariante de stock."""
        caja = await _caja(client, auth_headers)
        pid = await _producto(client, auth_headers, stock=3)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 5}], "500.00"),
            headers={**caja, "X-POS-Offline": "0"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "INSUFFICIENT_STOCK"
        assert (await db_session.execute(select(PosStockDiscrepancy))).first() is None

    async def test_sin_caja_habilitada_no_se_captura_ni_siquiera_el_dueno(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """El dueño cobra sin terminal, pero no relaja el stock sin una PC habilitada."""
        pid = await _producto(client, auth_headers, stock=3)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 5}], "500.00"),
            headers={**auth_headers, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "TERMINAL_REQUIRED_FOR_CAPTURE"


class TestResolucion:
    async def _capturar(
        self, client: AsyncClient, headers: dict[str, str], *, stock: int, vendidas: int
    ) -> tuple[str, str]:
        caja = await _caja(client, headers)
        pid = await _producto(client, headers, stock=stock)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta(
                [{"product_id": pid, "quantity": vendidas}], f"{vendidas * 100}.00"
            ),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 201, resp.text
        lista = await client.get("/api/v1/pos/stock-discrepancies", headers=headers)
        assert lista.status_code == 200, lista.text
        return pid, lista.json()[0]["id"]

    async def test_el_ajuste_de_entrada_deja_el_stock_correcto(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "El conteo estaba mal."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "RESOLVED"
        # 3 que había + 2 que entraron − 5 que salieron = 0.
        assert await _stock(db_session, pid) == 0

    async def test_el_ajuste_se_recalcula_al_resolver_no_con_el_numero_viejo(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """Entre la venta y la conciliación pudo entrar mercadería.

        Con el faltante de la captura (2) el ajuste sobraría y el stock quedaría
        inflado. Se mide ahora: había 3, entraron 4 más, se vendieron 5 → no
        falta nada y el ajuste es cero.
        """
        pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        producto = (
            await db_session.execute(select(Product).where(Product.id == uuid.UUID(pid)))
        ).scalar_one()
        producto.stock_units = 7
        await db_session.flush()

        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "Llegó una compra."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert await _stock(db_session, pid) == 2
        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_STOCK_DISCREPANCY_RESOLVED"
                )
            )
        ).scalar_one()
        assert auditoria.decision_data["unidades_ajustadas"] == 0

    async def test_resolver_dos_veces_no_duplica_nada(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        cuerpo = {"resolution": "ajuste_de_entrada", "reason": "El conteo estaba mal."}
        primera = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json=cuerpo, headers=auth_headers,
        )
        assert primera.status_code == 200, primera.text
        segunda = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json=cuerpo, headers=auth_headers,
        )
        assert segunda.status_code == 200, segunda.text
        assert await _stock(db_session, pid) == 0
        ajustes = (
            (
                await db_session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.movement_type == "adjustment",
                        InventoryMovement.source_type == "reconciliation",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ajustes) == 1

    async def test_anular_el_ticket_cierra_la_deuda_y_no_toca_el_stock(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """La venta no existió: no hay nada que ajustar ni que descontar."""
        pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "anular_ticket", "reason": "Se tipeó mal la cantidad."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["resolution"] == "anular_ticket"
        assert await _stock(db_session, pid) == 3
        operacion = (await db_session.execute(select(PosOperation))).scalar_one()
        await db_session.refresh(operacion)
        assert operacion.status == "VOIDED"

    async def test_anular_por_la_ruta_normal_tambien_cierra_la_deuda(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        """Si no, un ticket anulado dejaría una deuda sobre una venta que ya no existe."""
        _pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        operacion = (await db_session.execute(select(PosOperation))).scalar_one()
        anulacion = await client.post(
            f"/api/v1/pos/operations/{operacion.id}/void",
            json={"reason": "Error de carga."},
            headers=auth_headers,
        )
        assert anulacion.status_code == 200, anulacion.text
        fila = (
            await db_session.execute(
                select(PosStockDiscrepancy).where(
                    PosStockDiscrepancy.id == uuid.UUID(deuda)
                )
            )
        ).scalar_one()
        await db_session.refresh(fila)
        assert fila.status == "RESOLVED"
        assert fila.resolution == "anular_ticket"

    async def test_el_cajero_no_ve_ni_resuelve_diferencias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        fake_redis: Any,
    ) -> None:
        """Qué pasó con el stock no es una decisión de caja."""
        _pid, deuda = await self._capturar(client, auth_headers, stock=3, vendidas=5)
        cajero = User(
            user_id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            email=f"cajero-{uuid.uuid4().hex[:6]}@kiosco.com",
            full_name="Cajero",
            password_hash=hash_password("Secure123"),
            role_code="CASHIER",
            is_active=True,
            pin_hash=hash_password(TEST_PIN),
            pos_permissions={"void_ticket": True},
        )
        db_session.add(cajero)
        await db_session.commit()
        await fake_redis.set(
            _pin_window_key(sample_tenant.tenant_id, cajero.user_id), "1", ex=600
        )
        headers = {
            "Authorization": "Bearer "
            + create_access_token(
                {
                    "sub": str(cajero.user_id),
                    "tenant_id": str(sample_tenant.tenant_id),
                    "role_code": "CASHIER",
                }
            )
        }
        lista = await client.get("/api/v1/pos/stock-discrepancies", headers=headers)
        assert lista.status_code == 403, lista.text
        resolver = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "x"},
            headers=headers,
        )
        assert resolver.status_code == 403, resolver.text


def _sin_divergencia(agregado: dict[str, Any], temporal: Any) -> None:
    """Los dos chequeos miraron el producto Y no encontraron nada.

    Sin el `checked >= 1` el test es vacío: un producto que los chequeos
    saltean (sin ancla, o con ledger no auditable) devuelve cero divergencias
    por no haberlo mirado, y el test pasaría aunque la exclusión no existiera.
    """
    assert agregado["checked"] >= 1, agregado
    assert agregado["divergences"] == [], agregado
    assert temporal.checked >= 1, temporal
    assert temporal.divergences == [], temporal


class TestLosDosChequeos:
    """El agregado y el temporal tienen que entender lo mismo.

    Si sólo uno excluyera la venta capturada, el mismo dato saldría como
    divergencia en un chequeo y no en el otro, y nadie sabría cuál creer.
    """

    async def _escenario(
        self, client: AsyncClient, headers: dict[str, str], db_session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> tuple[str, str]:
        caja = await _caja(client, headers)
        pid = await _producto(client, headers, stock=3)
        await _anclar(db_session, tenant_id, pid, 3)
        # 30 y no 5: los dos chequeos ignoran una diferencia de hasta
        # `absolute_floor_units` (5), así que con 5 el test no probaría nada —
        # daría cero divergencias aunque la exclusión no existiera.
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 30}], "3000.00"),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 201, resp.text
        lista = await client.get("/api/v1/pos/stock-discrepancies", headers=headers)
        return pid, lista.json()[0]["id"]

    async def test_con_la_deuda_abierta_ninguno_reporta_divergencia(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        pid, _deuda = await self._escenario(
            client, auth_headers, db_session, sample_tenant.tenant_id
        )
        _sin_divergencia(
            await check_tenant_inventory_integrity(db_session, sample_tenant.tenant_id),
            await check_products_temporal_divergence(
                db_session, sample_tenant.tenant_id, product_ids=[uuid.UUID(pid)]
            ),
        )

    async def test_tras_resolver_ninguno_reporta_divergencia(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        pid, deuda = await self._escenario(
            client, auth_headers, db_session, sample_tenant.tenant_id
        )
        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "El conteo estaba mal."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        # 3 que había + 27 que entraron − 30 que salieron = 0.
        assert await _stock(db_session, pid) == 0
        _sin_divergencia(
            await check_tenant_inventory_integrity(db_session, sample_tenant.tenant_id),
            await check_products_temporal_divergence(
                db_session, sample_tenant.tenant_id, product_ids=[uuid.UUID(pid)]
            ),
        )


class TestElFaltanteSeMideALaFechaDeLaVenta:
    """Una compra posterior no puede hacer desaparecer el faltante.

    El ajuste se **fecha el día de la venta**: el dueño está afirmando que esas
    unidades estaban ese día. Midiendo el faltante sólo contra el stock de hoy,
    una compra cargada después daba faltante cero —ningún ajuste— y dos cosas
    quedaban mal: el stock perdía las unidades que de verdad existían, y el
    chequeo temporal seguía viendo el saldo en negativo en la fecha original.
    """

    async def _escenario(
        self, client: AsyncClient, headers: dict[str, str], db_session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> tuple[str, str]:
        caja = await _caja(client, headers)
        pid = await _producto(client, headers, stock=3)
        await _anclar(db_session, tenant_id, pid, 3)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_venta([{"product_id": pid, "quantity": 30}], "3000.00"),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert resp.status_code == 201, resp.text
        # Al día siguiente entra una compra de 40: el stock de hoy alcanza de
        # sobra, pero el día de la venta seguía sin alcanzar.
        producto = (
            await db_session.execute(select(Product).where(Product.id == uuid.UUID(pid)))
        ).scalar_one()
        db_session.add(
            InventoryMovement(
                tenant_id=tenant_id,
                product_id=uuid.UUID(pid),
                movement_type="purchase",
                qty=40,
                occurred_at=datetime.combine(today_ar(), time(12, 0))
                + timedelta(days=1),
            )
        )
        producto.stock_units = 43
        await db_session.flush()
        lista = await client.get("/api/v1/pos/stock-discrepancies", headers=headers)
        return pid, lista.json()[0]["id"]

    async def test_el_ajuste_entra_por_lo_que_faltaba_ese_dia(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        pid, deuda = await self._escenario(
            client, auth_headers, db_session, sample_tenant.tenant_id
        )
        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "Estaban, no cargadas."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_STOCK_DISCREPANCY_RESOLVED"
                )
            )
        ).scalar_one()
        # Faltaban 27 el día de la venta (había 3 y salieron 30), no 0.
        assert auditoria.decision_data["unidades_ajustadas"] == 27
        # 43 de hoy + 27 que estaban y no figuraban − 30 que salieron = 40.
        assert await _stock(db_session, pid) == 40

    async def test_resolver_deja_el_chequeo_temporal_en_cero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        pid, deuda = await self._escenario(
            client, auth_headers, db_session, sample_tenant.tenant_id
        )
        resp = await client.post(
            f"/api/v1/pos/stock-discrepancies/{deuda}/resolve",
            json={"resolution": "ajuste_de_entrada", "reason": "Estaban, no cargadas."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        _sin_divergencia(
            await check_tenant_inventory_integrity(db_session, sample_tenant.tenant_id),
            await check_products_temporal_divergence(
                db_session, sample_tenant.tenant_id, product_ids=[uuid.UUID(pid)]
            ),
        )


class TestExcepcionesAlDescuento:
    """Enumera las ÚNICAS ventas vivas que pueden no haber descontado stock.

    "Toda venta viva descuenta stock" es un invariante de `CLAUDE.md`. B11 abre
    su segunda excepción, y una excepción sin lista es una excepción que crece
    sola: este test falla si aparece una venta viva sin movimiento que no caiga
    en ninguna de las dos.
    """

    async def test_solo_la_importada_y_la_capturada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        from app.application.services.pos_discrepancy_service import (
            ventas_con_salida_pendiente,
        )
        from app.application.services.stock_service import sale_source_event_id

        caja = await _caja(client, auth_headers)
        # (1) Venta de caja normal: descuenta.
        normal = await _producto(client, auth_headers, stock=10)
        assert (
            await client.post(
                "/api/v1/pos/operations",
                json=_venta([{"product_id": normal, "quantity": 2}], "200.00"),
                headers=caja,
            )
        ).status_code == 201
        # (2) Venta importada: no descuenta, y es la primera excepción.
        importado = await _producto(client, auth_headers, stock=10)
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("100.00"),
                quantity=1,
                transaction_date=today_ar(),
                payment_method="cash",
                product_id=uuid.UUID(importado),
                source_upload_id=uuid.uuid4(),
            )
        )
        await db_session.flush()
        # (3) Venta de caja capturada: la segunda excepción.
        capturado = await _producto(client, auth_headers, stock=1)
        assert (
            await client.post(
                "/api/v1/pos/operations",
                json=_venta([{"product_id": capturado, "quantity": 4}], "400.00"),
                headers={**caja, "X-POS-Offline": "1"},
            )
        ).status_code == 201

        pendientes = await ventas_con_salida_pendiente(db_session, sample_tenant.tenant_id)
        movimientos = {
            m.source_event_id
            for m in (
                (
                    await db_session.execute(
                        select(InventoryMovement).where(
                            InventoryMovement.tenant_id == sample_tenant.tenant_id,
                            InventoryMovement.movement_type == "sale",
                            InventoryMovement.voided_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        ventas = (
            (
                await db_session.execute(
                    select(SaleEntry).where(
                        SaleEntry.tenant_id == sample_tenant.tenant_id,
                        SaleEntry.voided_at.is_(None),
                        SaleEntry.product_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        sin_descontar = [
            v for v in ventas if sale_source_event_id(v.id) not in movimientos
        ]
        assert sin_descontar, "el escenario tiene que producir ventas sin descuento"
        for venta in sin_descontar:
            importada = venta.source_upload_id is not None
            capturada = venta.id in pendientes
            assert importada or capturada, (
                f"La venta {venta.id} está viva y no descontó stock, y no es ni "
                "importada ni una salida pendiente de caja. Si es una excepción "
                "nueva, va documentada en CLAUDE.md y enumerada acá."
            )
