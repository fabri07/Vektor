"""B14 del POS — con la suscripción bloqueada la caja sigue cobrando y el análisis se pausa.

Lo estructural (qué rutas llevan qué gate) vive en
`test_subscription_write_gate.py`. Acá se prueba el COMPORTAMIENTO:

1. **La caja completa su circuito en `READ_ONLY`**: catálogo, turno, cobro,
   reintento, recibo, anulación y cierre. Una caja que deja de cobrar porque
   venció el plan le corta la venta al negocio.
2. **Las restricciones propias de la caja siguen**: sacarle el gate de
   suscripción no le saca la terminal, los permisos ni el PIN.
3. **El análisis da 402 `SUBSCRIPTION_INSIGHTS_LOCKED`**, sin suscripción 503,
   y con una suscripción paga vigente cada ruta devuelve su resultado normal.
4. **Respuesta perdida**: el cobro se commitea, la respuesta no llega, y el
   reintento con la misma clave devuelve la MISMA operación sin cobrar ni
   descontar stock dos veces — con la suscripción vigente y en `READ_ONLY`.

El chat en `READ_ONLY` ya lo cubre `integration/test_endpoints.py`
(`reserve()` corta antes del orquestador) y el agente que no registra ventas,
`test_subscription_write_gate.py::test_el_agente_no_registra_una_venta_en_solo_lectura`.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.business import Insight, MomentumProfile
from app.persistence.models.pos_operation import PosOperation, PosTender
from app.persistence.models.product import Product
from app.persistence.models.tenant import Subscription, Tenant
from app.tests.api.v1.test_cashier_access import _cajero
from app.tests.api.v1.test_grupo_c import _create_snapshot

P = "/api/v1"
_FECHA = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> Any:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _estado(db_session: AsyncSession, tenant: Tenant, status: str) -> None:
    """Un plan PAGO en `status`, con período vigente (FREE legado es exento)."""
    ahora = datetime.now(UTC)
    await db_session.execute(
        update(Subscription)
        .where(Subscription.tenant_id == tenant.tenant_id)
        .values(
            plan_code="control",
            status=status,
            seats_included=3,
            current_period_start=ahora - timedelta(days=1),
            current_period_end=ahora + timedelta(days=29),
        )
    )
    await db_session.commit()


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        f"{P}/pos/terminals", json={"name": f"Caja {uuid.uuid4().hex[:4]}"}, headers=headers
    )
    assert alta.status_code == 201, alta.text
    return {**headers, TERMINAL_HEADER: alta.json()["secret"]}


async def _producto(client: AsyncClient, headers: dict[str, str], *, stock: int = 10) -> str:
    resp = await client.post(
        f"{P}/products",
        json={
            "name": f"Yerba {uuid.uuid4().hex[:5]}",
            "sale_price_ars": "100.00",
            "stock_units": stock,
            # M·P2c: el stock cargado a mano lleva precio de compra.
            **({"unit_cost_ars": "50.00"} if stock else {}),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _venta(
    pid: str, *, turno: str = "turno-b14", cantidad: int = 1, **extra: Any
) -> dict[str, Any]:
    return {
        "client_operation_id": f"op-{uuid.uuid4().hex[:16]}",
        "operation_date": _FECHA,
        "client_session_id": turno,
        "items": [{"product_id": pid, "quantity": cantidad, "discount_ars": "0.00"}],
        "tenders": [{"payment_method": "cash", "amount_ars": f"{cantidad * 100}.00"}],
        **extra,
    }


async def _stock(db_session: AsyncSession, pid: str) -> int:
    db_session.expire_all()
    return int(
        (await db_session.execute(select(Product.stock_units).where(Product.id == uuid.UUID(pid))))
        .scalar_one()
    )


# ── 1. La caja completa su circuito en READ_ONLY ─────────────────────────────


class TestLaCajaCobraEnSoloLectura:
    async def test_circuito_completo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        db_session: AsyncSession,
    ) -> None:
        # Los productos se dan de alta ANTES: crear un producto sí es un alta
        # de negocio y sigue bloqueada.
        pid = await _producto(client, auth_headers)
        caja = await _caja(client, auth_headers)
        await _estado(db_session, sample_tenant, "READ_ONLY")

        catalogo = await client.get(f"{P}/pos/catalog", headers=caja)
        assert catalogo.status_code == 200, catalogo.text

        turno = await client.post(
            f"{P}/pos/cash-sessions",
            json={"client_session_id": "turno-b14", "opening_float_ars": "1000.00"},
            headers=caja,
        )
        assert turno.status_code == 201, turno.text

        cuerpo = _venta(pid, cantidad=2)
        cobro = await client.post(f"{P}/pos/operations", json=cuerpo, headers=caja)
        assert cobro.status_code == 201, cobro.text
        op_id = cobro.json()["id"]

        reintento = await client.post(f"{P}/pos/operations", json=cuerpo, headers=caja)
        assert reintento.status_code == 200, reintento.text
        assert reintento.json()["id"] == op_id

        recibo = await client.get(f"{P}/pos/operations/{op_id}/receipt", headers=caja)
        assert recibo.status_code == 200, recibo.text

        # Una venta sin red que sube con la suscripción ya vencida también entra.
        offline = await client.post(
            f"{P}/pos/operations",
            json=_venta(pid, charged_at=_FECHA),
            headers={**caja, "X-POS-Offline": "1"},
        )
        assert offline.status_code == 201, offline.text

        anulacion = await client.post(
            f"{P}/pos/operations/{op_id}/void", json={"reason": "prueba"}, headers=caja
        )
        assert anulacion.status_code == 200, anulacion.text
        assert anulacion.json()["status"] == "VOIDED"

        cierre = await client.post(
            f"{P}/pos/cash-sessions/{turno.json()['id']}/close",
            json={"counted_cash_ars": "1100.00"},
            headers=caja,
        )
        assert cierre.status_code == 200, cierre.text
        # Fondo 1000 + la venta offline de 100; la anulada no suma.
        assert Decimal(cierre.json()["expected_cash_ars"]) == Decimal("1100.00")

        # El arqueo diario del negocio tampoco se bloquea: es contar plata.
        arqueo = await client.post(
            f"{P}/cash-closes",
            json={"close_date": today_ar().isoformat(), "counted_total_ars": "100.00"},
            headers=auth_headers,
        )
        assert arqueo.status_code == 201, arqueo.text

    async def test_la_carga_manual_del_duenio_sigue_bloqueada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        db_session: AsyncSession,
    ) -> None:
        """La excepción es la CAJA, no toda venta: `manual-batch` es un alta como `/sales`."""
        await _estado(db_session, sample_tenant, "READ_ONLY")
        resp = await client.post(f"{P}/sales/manual-batch", json={}, headers=auth_headers)
        assert resp.status_code == 402
        assert resp.json()["detail"]["code"] == "SUBSCRIPTION_READ_ONLY"


# ── 2. Las restricciones propias de la caja siguen ───────────────────────────


class TestLaCajaConservaSusRestricciones:
    async def test_cajero_sin_terminal(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        db_session: AsyncSession,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        await _estado(db_session, sample_tenant, "READ_ONLY")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis, con_terminal=False)

        resp = await client.post(f"{P}/pos/operations", json=_venta(pid), headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "TERMINAL_REQUIRED"

    async def test_cajero_sin_permiso_de_descuento(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        db_session: AsyncSession,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        await _estado(db_session, sample_tenant, "READ_ONLY")
        _, headers = await _cajero(db_session, sample_tenant, fake_redis)

        cuerpo = _venta(pid, cantidad=2)
        cuerpo["items"][0]["discount_ars"] = "10.00"
        cuerpo["tenders"][0]["amount_ars"] = "190.00"
        resp = await client.post(f"{P}/pos/operations", json=cuerpo, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "POS_PERMISSION_DENIED"

    async def test_la_anulacion_del_cajero_sigue_pidiendo_pin(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sample_tenant: Tenant,
        db_session: AsyncSession,
        fake_redis: Any,
    ) -> None:
        pid = await _producto(client, auth_headers)
        _, headers = await _cajero(
            db_session, sample_tenant, fake_redis, {"void_ticket": True}, pin_abierto=False
        )
        cobro = await client.post(f"{P}/pos/operations", json=_venta(pid), headers=headers)
        assert cobro.status_code == 201, cobro.text
        await _estado(db_session, sample_tenant, "READ_ONLY")

        resp = await client.post(
            f"{P}/pos/operations/{cobro.json()['id']}/void",
            json={"reason": "prueba"},
            headers=headers,
        )
        assert resp.status_code == 428, resp.text


# ── 3. El análisis se pausa ──────────────────────────────────────────────────

#: Cada lectura analítica con cómo se reconoce su resultado NORMAL. Una clave
#: de la respuesta, no un 200 genérico: un 200 con otra forma también es falla.
_MES = f"from_date={today_ar().replace(day=1)}&to_date={today_ar()}"
_ANALISIS: list[tuple[str, str, str | None]] = [
    ("GET", "/insights", None),
    ("GET", "/insights/current", "insight"),
    ("GET", "/insights/actions", None),
    ("GET", "/insights/breakdown", "expenses_by_category"),
    ("GET", "/insights/cash-breakdown?days=30", "income_series"),
    ("GET", "/forecast/cash", "points"),
    ("GET", "/health-scores/current", None),
    ("GET", "/health-scores/summary", "current_score"),
    ("GET", "/health-scores/history", None),
    ("GET", "/health-scores/weekly", None),
    ("GET", "/health-scores/latest", None),
    ("GET", "/health-scores/margin-history", None),
    ("GET", "/health-scores/history/v2", None),
    ("GET", f"/economic-summary?{_MES}", "total_income_ars"),
    ("GET", "/momentum/profile", "best_score_ever"),
]


@pytest.mark.parametrize(("metodo", "ruta", "_clave"), _ANALISIS)
async def test_en_solo_lectura_el_analisis_da_402(
    metodo: str,
    ruta: str,
    _clave: str | None,
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    await _estado(db_session, sample_tenant, "READ_ONLY")
    resp = await client.request(metodo, f"{P}{ruta}", headers=auth_headers)
    assert resp.status_code == 402, f"{ruta}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == {
        "code": "SUBSCRIPTION_INSIGHTS_LOCKED",
        "reason": "solo_lectura",
    }


async def test_en_solo_lectura_el_informe_del_score_da_402(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
    await _estado(db_session, sample_tenant, "READ_ONLY")
    resp = await client.post(
        f"{P}/health-scores/{snap.id}/export",
        json={"format": "pdf", "narrative": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_INSIGHTS_LOCKED"


async def test_sin_suscripcion_el_analisis_da_503(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    """Un tenant sin suscripción es un dato roto nuestro, nunca acceso libre."""
    await db_session.execute(
        delete(Subscription).where(Subscription.tenant_id == sample_tenant.tenant_id)
    )
    await db_session.commit()
    resp = await client.get(f"{P}/insights/current", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "SUBSCRIPTION_UNAVAILABLE"


async def _datos_de_analisis(db_session: AsyncSession, tenant: Tenant) -> None:
    """Lo que cada ruta necesita para dar su resultado normal y no su vacío."""
    await _create_snapshot(db_session, tenant.tenant_id)
    db_session.add(
        Insight(
            tenant_id=tenant.tenant_id,
            insight_type="CASH_FLOW",
            title="Riesgo de caja",
            description="La cobertura de caja está por debajo del mínimo.",
            severity_code="HIGH",
        )
    )
    db_session.add(
        MomentumProfile(
            tenant_id=tenant.tenant_id,
            best_score_ever=72,
            milestones_json=[],
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()


@pytest.mark.parametrize(("metodo", "ruta", "clave"), _ANALISIS)
async def test_con_el_plan_vigente_cada_ruta_da_su_resultado(
    metodo: str,
    ruta: str,
    clave: str | None,
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    await _datos_de_analisis(db_session, sample_tenant)
    await _estado(db_session, sample_tenant, "ACTIVE")
    resp = await client.request(metodo, f"{P}{ruta}", headers=auth_headers)
    assert resp.status_code == 200, f"{ruta}: {resp.status_code} {resp.text}"
    if clave is not None:
        assert clave in resp.json(), f"{ruta}: {resp.json()}"


async def test_con_el_plan_vigente_el_informe_sale(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    snap = await _create_snapshot(db_session, sample_tenant.tenant_id)
    await _estado(db_session, sample_tenant, "ACTIVE")
    resp = await client.post(
        f"{P}/health-scores/{snap.id}/export",
        json={"format": "pdf", "narrative": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")


async def test_free_legado_sigue_viendo_el_analisis(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    """`sample_tenant` nace con su FREE legado `ACTIVE`, exento a propósito."""
    await _datos_de_analisis(db_session, sample_tenant)
    resp = await client.get(f"{P}/insights/current", headers=auth_headers)
    assert resp.status_code == 200


async def test_en_solo_lectura_las_correcciones_siguen(
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    """Corregir lo cargado no es ni un alta ni análisis."""
    venta = await client.post(
        f"{P}/sales",
        json={"amount": "500.00", "transaction_date": _FECHA, "payment_method": "cash"},
        headers=auth_headers,
    )
    assert venta.status_code == 201, venta.text
    await _estado(db_session, sample_tenant, "READ_ONLY")

    corregida = await client.patch(
        f"{P}/sales/{venta.json()['id']}", json={"amount": "600.00"}, headers=auth_headers
    )
    assert corregida.status_code == 200, corregida.text
    # El acknowledge de una sugerencia es una corrección: sin gate (404 porque no existe).
    ack = await client.patch(
        f"{P}/insights/actions/{uuid.uuid4()}/acknowledge", headers=auth_headers
    )
    assert ack.status_code == 404, ack.text


# ── 4. Respuesta perdida ─────────────────────────────────────────────────────


@pytest.mark.parametrize("estado", ["ACTIVE", "READ_ONLY"])
async def test_respuesta_perdida_el_reintento_devuelve_la_misma_operacion(
    estado: str,
    client: AsyncClient,
    auth_headers: dict[str, str],
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    pid = await _producto(client, auth_headers, stock=10)
    caja = await _caja(client, auth_headers)
    await _estado(db_session, sample_tenant, estado)
    cuerpo = _venta(pid, cantidad=3)

    # El servidor commitea y la caja nunca recibe la respuesta: se descarta.
    primero = await client.post(f"{P}/pos/operations", json=cuerpo, headers=caja)
    assert primero.status_code == 201, primero.text
    del primero

    reintento = await client.post(f"{P}/pos/operations", json=cuerpo, headers=caja)
    assert reintento.status_code == 200, reintento.text

    operaciones = (
        await db_session.execute(
            select(PosOperation).where(
                PosOperation.tenant_id == sample_tenant.tenant_id,
                PosOperation.client_operation_id == cuerpo["client_operation_id"],
            )
        )
    ).scalars().all()
    assert len(operaciones) == 1
    assert reintento.json()["id"] == str(operaciones[0].id)

    pagos = (
        await db_session.execute(
            select(func.count(), func.sum(PosTender.amount_ars)).where(
                PosTender.operation_id == operaciones[0].id
            )
        )
    ).one()
    assert pagos == (1, Decimal("300.00"))
    assert await _stock(db_session, pid) == 7
