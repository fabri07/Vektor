"""El precio que la caja cobró, y el ticket que no cambia (B7·1).

Dos cosas se protegen acá:

1. **Una venta cobrada sin red entra al precio que se cobró.** Antes de B7 el
   precio salía del catálogo al recibir la operación, así que si el dueño lo
   cambiaba durante el corte los pagos dejaban de cuadrar y la venta —ya
   cobrada— se rechazaba. Ahora la caja lo declara con la firma del servidor.
2. **Y nadie puede declarar un precio que el servidor no emitió.** Entre el
   navegador y el handler hay una cola que se puede editar a mano.
"""

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.pos_price_token import VIGENCIA_SEGUNDOS, emitir
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_operation import PosOperation, PosOperationLine
from app.persistence.models.product import Product

_FECHA = f"{today_ar().isoformat()}T10:00:00"


async def _crear_producto(
    client: AsyncClient, headers: dict[str, Any], name: str, *, stock: int = 10,
    price: str = "100.00",
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _del_catalogo(client: AsyncClient, headers: dict[str, Any], pid: str) -> dict:
    """El producto tal como lo baja la caja, con su token."""
    resp = await client.get("/api/v1/pos/catalog", headers=headers)
    assert resp.status_code == 200, resp.text
    for item in resp.json()["items"]:
        if item["id"] == pid:
            return item
    raise AssertionError(f"{pid} no está en el catálogo")


def _linea(producto: dict, cantidad: int = 1) -> dict:
    """Una línea como la arma la caja: precio, factor y token del catálogo."""
    return {
        "product_id": producto["id"],
        "quantity": cantidad,
        "unit_price_ars": producto["sale_price_ars"],
        "base_units_per_sale_unit": producto["base_units_per_sale_unit"],
        "price_token": producto["price_token"],
    }


def _operacion(items: list[dict], tenders: list[dict], **extra: Any) -> dict:
    return {
        "client_operation_id": extra.pop("client_operation_id", f"op-{uuid.uuid4().hex[:16]}"),
        "operation_date": _FECHA,
        "items": items,
        "tenders": tenders,
        **extra,
    }


@pytest.fixture(autouse=True)
def patch_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


class TestPrecioFirmado:
    async def test_el_catalogo_emite_el_token(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)
        assert producto["price_token"]

    async def test_cobra_al_precio_declarado_cuando_el_catalogo_cambio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        """El caso real: corte de internet, el dueño sube el precio, sincroniza."""
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)

        subida = await client.patch(
            f"/api/v1/products/{pid}", json={"sale_price_ars": "1500.00"}, headers=auth_headers
        )
        assert subida.status_code == 200, subida.text

        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [_linea(producto, 2)],
                [{"payment_method": "cash", "amount_ars": "2000.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        linea = resp.json()["lines"][0]
        assert linea["unit_price_list"] == "1000.00"
        assert resp.json()["total_ars"] == "2000.00"

        # La divergencia queda en la línea y en la auditoría, con los dos números.
        fila = (
            await db_session.execute(
                select(PosOperationLine).where(PosOperationLine.id == uuid.UUID(linea["id"]))
            )
        ).scalar_one()
        assert fila.unit_price_catalog_ars == Decimal("1500.00")
        auditoria = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_PRICE_DIVERGENCE"
                )
            )
        ).scalars().all()
        assert len(auditoria) == 1
        assert auditoria[0].decision_data["lines"][0]["cobrado_ars"] == "1000.00"
        assert auditoria[0].decision_data["lines"][0]["catalogo_ars"] == "1500.00"

    async def test_sin_divergencia_no_hay_ni_columna_ni_auditoria(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [_linea(producto)], [{"payment_method": "cash", "amount_ars": "1000.00"}]
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["lines"][0]["unit_price_catalog_ars"] is None
        assert (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "POS_PRICE_DIVERGENCE"
                )
            )
        ).first() is None

    @pytest.mark.parametrize(
        "romper",
        [
            lambda p: {**p, "price_token": "1700000000." + "0" * 32},
            lambda p: {**p, "price_token": "basura"},
            lambda p: {**p, "unit_price_ars": "1.00"},
            lambda p: {**p, "base_units_per_sale_unit": 1000},
        ],
        ids=["firma cambiada", "token basura", "precio cambiado", "factor cambiado"],
    )
    async def test_un_precio_que_el_servidor_no_emitio_no_entra(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        romper,
    ) -> None:
        """Ni se cobra, ni se cae al catálogo en silencio: se rechaza."""
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [romper(_linea(producto))],
                [{"payment_method": "cash", "amount_ars": "1000.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "PRICE_TOKEN_INVALID"
        assert (await db_session.execute(select(PosOperation))).first() is None

    async def test_el_token_de_un_producto_no_sirve_para_otro(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        a = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        b = await _crear_producto(client, auth_headers, "Fideos", price="1000.00")
        token_de_a = (await _del_catalogo(client, auth_headers, a))["price_token"]
        producto_b = await _del_catalogo(client, auth_headers, b)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [{**_linea(producto_b), "price_token": token_de_a}],
                [{"payment_method": "cash", "amount_ars": "1000.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "PRICE_TOKEN_INVALID"

    async def test_un_token_viejo_no_cobra_para_siempre(
        self, client: AsyncClient, auth_headers: dict[str, Any], sample_tenant
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        vencido = emitir(
            sample_tenant.tenant_id,
            uuid.UUID(pid),
            Decimal("1000.00"),
            "unit",
            1,
            ahora=datetime.now(UTC) - timedelta(seconds=VIGENCIA_SEGUNDOS + 3600),
        )
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [
                    {
                        "product_id": pid,
                        "quantity": 1,
                        "unit_price_ars": "1000.00",
                        "base_units_per_sale_unit": 1,
                        "price_token": vencido,
                    }
                ],
                [{"payment_method": "cash", "amount_ars": "1000.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "PRICE_TOKEN_EXPIRED"

    async def test_los_tres_campos_del_precio_van_juntos(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Aceptar dos de tres y caer al catálogo sería un downgrade silencioso."""
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [{"product_id": pid, "quantity": 1, "unit_price_ars": "1.00"}],
                [{"payment_method": "cash", "amount_ars": "1.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text

    async def test_sin_precio_declarado_sigue_mandando_el_catalogo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Un payload anterior a B7, todavía en la cola, tiene que poder entrar."""
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [{"product_id": pid, "quantity": 1}],
                [{"payment_method": "cash", "amount_ars": "1000.00"}],
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["lines"][0]["unit_price_list"] == "1000.00"


class TestCobradoSinConexion:
    async def test_el_header_registra_la_afirmacion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)
        cuerpo = _operacion(
            [_linea(producto)],
            [{"payment_method": "cash", "amount_ars": "1000.00"}],
            charged_at=f"{today_ar().isoformat()}T09:30:00",
            client_session_id="turno-abc",
        )
        # Con caja habilitada: marcar una venta como cobrada sin conexión exige
        # una PC que el dueño haya habilitado, aunque el que cobre sea el dueño
        # (B11). Sin eso, la afirmación no quedaría atada a nada.
        alta = await client.post(
            "/api/v1/pos/terminals", json={"name": "Caja 1"}, headers=auth_headers
        )
        assert alta.status_code == 201, alta.text
        resp = await client.post(
            "/api/v1/pos/operations",
            json=cuerpo,
            headers={
                **auth_headers,
                "X-POS-Offline": "1",
                "X-POS-Terminal": alta.json()["secret"],
            },
        )
        assert resp.status_code == 201, resp.text
        fila = (
            await db_session.execute(
                select(PosOperation).where(
                    PosOperation.client_operation_id == cuerpo["client_operation_id"]
                )
            )
        ).scalar_one()
        assert fila.captured_offline is True
        assert fila.charged_at is not None
        assert fila.client_session_id == "turno-abc"

    async def test_el_header_no_cambia_la_huella_de_idempotencia(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El mismo pedido, reintentado por la cola, sigue siendo el mismo pedido.

        Si el marcador viajara en el cuerpo, el reintento cambiaría la huella y
        el servidor contestaría `IDEMPOTENCY_KEY_REUSED` con la venta hecha.
        """
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        producto = await _del_catalogo(client, auth_headers, pid)
        cuerpo = _operacion(
            [_linea(producto)], [{"payment_method": "cash", "amount_ars": "1000.00"}]
        )
        primera = await client.post(
            "/api/v1/pos/operations", json=cuerpo, headers=auth_headers
        )
        assert primera.status_code == 201, primera.text
        segunda = await client.post(
            "/api/v1/pos/operations",
            json=cuerpo,
            headers={**auth_headers, "X-POS-Offline": "1"},
        )
        assert segunda.status_code == 200, segunda.text
        assert segunda.json()["id"] == primera.json()["id"]

    async def test_una_fecha_de_cobro_futura_se_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        manana = today_ar() + timedelta(days=1)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [{"product_id": pid, "quantity": 1}],
                [{"payment_method": "cash", "amount_ars": "1000.00"}],
                charged_at=f"{manana.isoformat()}T09:00:00",
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 422, resp.text


class TestTicketCongelado:
    async def _cobrar(
        self, client: AsyncClient, headers: dict[str, Any], pid: str
    ) -> str:
        producto = await _del_catalogo(client, headers, pid)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [_linea(producto)], [{"payment_method": "cash", "amount_ars": "1000.00"}]
            ),
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    async def test_renombrar_el_producto_no_cambia_un_ticket_viejo(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _crear_producto(client, auth_headers, "Yerba Playadito", price="1000.00")
        op = await self._cobrar(client, auth_headers, pid)
        await client.patch(
            f"/api/v1/products/{pid}", json={"name": "Yerba Taragüi"}, headers=auth_headers
        )
        recibo = await client.get(
            f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers
        )
        assert recibo.status_code == 200, recibo.text
        assert recibo.json()["lines"][0]["product_name"] == "Yerba Playadito"

    async def test_un_producto_que_ya_no_existe_no_deja_el_ticket_sin_nombre(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        """`pos_operation_lines.product_id` es SET NULL: el ticket sobrevive igual.

        Se simula lo que hace la FK cuando el producto desaparece de verdad.
        Sin snapshot, el recibo decía "Producto eliminado".
        """
        pid = await _crear_producto(client, auth_headers, "Yerba Playadito", price="1000.00")
        op = await self._cobrar(client, auth_headers, pid)
        linea = (
            await db_session.execute(
                select(PosOperationLine).where(
                    PosOperationLine.operation_id == uuid.UUID(op)
                )
            )
        ).scalar_one()
        linea.product_id = None
        await db_session.flush()

        recibo = await client.get(
            f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers
        )
        assert recibo.status_code == 200, recibo.text
        assert recibo.json()["lines"][0]["product_name"] == "Yerba Playadito"
        assert recibo.json()["lines"][0]["quantity_display"] == "1 un."

    async def test_una_linea_sin_snapshot_sigue_leyendo_el_catalogo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        """Las líneas anteriores a B7 tienen el snapshot en NULL y no se inventa."""
        pid = await _crear_producto(client, auth_headers, "Yerba", price="1000.00")
        op = await self._cobrar(client, auth_headers, pid)
        linea = (
            await db_session.execute(
                select(PosOperationLine).where(
                    PosOperationLine.operation_id == uuid.UUID(op)
                )
            )
        ).scalar_one()
        linea.product_name_snapshot = None
        linea.sale_unit_snapshot = None
        linea.base_units_snapshot = None
        await db_session.flush()

        recibo = await client.get(
            f"/api/v1/pos/operations/{op}/receipt", headers=auth_headers
        )
        assert recibo.status_code == 200, recibo.text
        assert recibo.json()["lines"][0]["product_name"] == "Yerba"
        assert recibo.json()["lines"][0]["quantity_display"] == "1 un."

    async def test_el_snapshot_guarda_la_unidad_con_la_que_se_cobro(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
    ) -> None:
        """750 g a $1.234,56 el kilo: la cantidad impresa explica el importe."""
        pid = await _crear_producto(client, auth_headers, "Queso", stock=5000, price="1234.56")
        producto_orm = (
            await db_session.execute(select(Product).where(Product.id == uuid.UUID(pid)))
        ).scalar_one()
        producto_orm.sale_unit = "gram"
        producto_orm.base_units_per_sale_unit = 1000
        await db_session.flush()

        producto = await _del_catalogo(client, auth_headers, pid)
        resp = await client.post(
            "/api/v1/pos/operations",
            json=_operacion(
                [_linea(producto, 750)], [{"payment_method": "cash", "amount_ars": "925.92"}]
            ),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        recibo = await client.get(
            f"/api/v1/pos/operations/{resp.json()['id']}/receipt", headers=auth_headers
        )
        assert recibo.json()["lines"][0]["quantity_display"] == "0,750 kg"
