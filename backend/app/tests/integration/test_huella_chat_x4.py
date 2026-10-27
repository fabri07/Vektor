"""La huella anti-duplicado del chat no descarta operaciones distintas (X4-1).

Revisión profunda 2026-10-26: la huella de `operation_fingerprints` era
``sha256(tenant:action_type:amount:fecha)`` y no vencía nunca. Dos gastos
distintos del mismo monto el mismo día («5000 de luz» y «5000 de flete») daban la
misma huella: el segundo salía «ya fue registrada», quedaba SUCCEEDED y no se
persistía. Una venta sin fecha (``date: None``) dejaba la clave ``…:None`` tomada
para siempre: toda venta posterior de ese monto sin fecha se perdía, días después.

Ahora la huella son los campos determinísticos de la operación más el mensaje
crudo del usuario (nunca el texto que redacta el LLM), y vence a los 5 minutos:
sigue frenando el doble envío y la doble confirmación de la MISMA operación, no
dos operaciones distintas.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import MENSAJE_DUPLICADO_CHAT
from app.application.agents.shared.schemas import AgentResponse, RiskLevel
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.user import User
from app.tests._respaldo import producto_con_compra
from app.tests.conftest import add_business_profile
from app.utils.security import create_access_token

from .conftest import FakeRedis, make_tenant, make_user


def _gasto(mensaje: str, descripcion: str, categoria: str) -> AgentResponse:
    """Gasto auto-ejecutable, con la fecha SIN hora que fija el agente de gastos.

    ``descripcion`` y ``categoria`` las redacta el LLM: el mismo mensaje puede
    volver con otras."""
    return AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_cash",
        status="success",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        result={
            "action_type": "REGISTER_EXPENSE",
            "structured_data": {
                "amount": "5000",
                "category": categoria,
                "description": descripcion,
                "transaction_date": "2026-10-20",
                "payment_method": "cash",
                "notes": mensaje,
            },
            "summary": f"Gasto registrado: {descripcion} por $5000",
        },
    )


def _venta(producto: Product, medio: str = "cash") -> AgentResponse:
    """Venta para aprobar con ``date: None``: la forma en que la deja el agente
    de ventas cuando el mensaje no dice fecha."""
    return AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_income",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        result={
            "action_type": "REGISTER_SALE",
            "structured_data": {
                "amount": "1000",
                "quantity": 1,
                "date": None,
                "payment_method": medio,
                "product_id": str(producto.id),
                "product_description": producto.name,
            },
            "summary": "Registrar venta por $1000",
        },
    )


@pytest_asyncio.fixture
async def negocio(session: AsyncSession) -> Tenant:
    tenant = await make_tenant(session)
    await add_business_profile(session, tenant.tenant_id)
    await session.commit()
    return tenant


@pytest_asyncio.fixture
async def ac(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()
    redis = FakeRedis()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _redis() -> AsyncIterator[FakeRedis]:
        yield redis

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def dueno(session: AsyncSession, negocio: Tenant) -> dict[str, str]:
    user = await make_user(
        session, negocio.tenant_id, role_code="OWNER", email=f"x4+{uuid.uuid4().hex[:6]}@test.com"
    )
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(negocio.tenant_id), "role_code": "OWNER"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _contar(session: AsyncSession, modelo: Any, tenant: Tenant) -> int:
    return int(
        (
            await session.execute(
                select(func.count(modelo.id)).where(modelo.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
    )


async def _chat(
    ac: AsyncClient, headers: dict[str, str], respuesta: AgentResponse, mensaje: str
) -> Any:
    with patch("app.api.v1.agent.ChatOrchestrator") as orquestador:
        orquestador.return_value.handle = AsyncMock(return_value=respuesta)
        resp = await ac.post("/api/v1/agent/chat", json={"message": mensaje}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _vender(
    ac: AsyncClient,
    headers: dict[str, str],
    respuesta: AgentResponse,
    mensaje: str = "vendí una vela a 1000",
) -> Any:
    """Venta por el camino real: el chat la deja pendiente y el dueño confirma."""
    data = await _chat(ac, headers, respuesta, mensaje)
    pendiente = data["pending_action_id"]
    assert pendiente, data
    with patch("app.application.services.stock_service.EventBus.emit"):
        resp = await ac.post(f"/api/v1/agent/confirm/{pendiente}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _envejecer_huellas(session: AsyncSession, tenant: Tenant, dias: int) -> None:
    """Corre las huellas del negocio ``dias`` hacia atrás: «la venta de ayer»."""
    await session.execute(
        update(OperationFingerprint)
        .where(OperationFingerprint.tenant_id == tenant.tenant_id)
        .values(executed_at=datetime.now(UTC) - timedelta(days=dias))
    )
    await session.commit()


# ── Operaciones distintas: se registran las dos ──────────────────────────────


async def test_dos_gastos_distintos_del_mismo_monto_el_mismo_dia_se_registran_los_dos(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    await _chat(ac, dueno, _gasto("pagué 5000 de luz", "Luz", "UTILITIES"), "pagué 5000 de luz")
    segundo = await _chat(
        ac, dueno, _gasto("pagué 5000 de flete", "Flete", "LOGISTICS"), "pagué 5000 de flete"
    )

    assert segundo["message"] != MENSAJE_DUPLICADO_CHAT
    gastos = (
        (
            await session.execute(
                select(ExpenseEntry.description).where(ExpenseEntry.tenant_id == negocio.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(gastos) == ["Flete", "Luz"]


async def test_dos_ventas_sin_fecha_del_mismo_monto_en_dias_distintos_se_registran_las_dos(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    producto = await producto_con_compra(session, negocio.tenant_id)

    primera = await _vender(ac, dueno, _venta(producto))
    assert primera["execution_status"] == "SUCCEEDED", primera
    await _envejecer_huellas(session, negocio, dias=1)
    segunda = await _vender(ac, dueno, _venta(producto))

    assert segunda["execution_status"] == "SUCCEEDED", segunda
    assert await _contar(session, SaleEntry, negocio) == 2


async def test_dos_ventas_sin_fecha_con_otro_medio_de_pago_al_toque_se_registran_las_dos(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    producto = await producto_con_compra(session, negocio.tenant_id)

    # El MISMO mensaje: lo que distingue las dos ventas es el medio de pago.
    await _vender(ac, dueno, _venta(producto, "cash"))
    segunda = await _vender(ac, dueno, _venta(producto, "transfer"))

    assert segunda["execution_status"] == "SUCCEEDED", segunda
    assert await _contar(session, SaleEntry, negocio) == 2


# ── La MISMA operación: sigue frenada ────────────────────────────────────────


async def test_el_mismo_gasto_enviado_dos_veces_al_toque_sale_duplicado(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    mensaje = "pagué 5000 de luz"
    await _chat(ac, dueno, _gasto(mensaje, "Luz", "UTILITIES"), mensaje)
    segundo = await _chat(ac, dueno, _gasto(mensaje, "Luz", "UTILITIES"), mensaje)

    assert segundo["message"] == MENSAJE_DUPLICADO_CHAT
    assert "5 minutos" in segundo["message"]
    assert await _contar(session, ExpenseEntry, negocio) == 1


async def test_el_mismo_mensaje_redactado_distinto_por_el_llm_sigue_saliendo_duplicado(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    """El reintento de un stream cortado: mismo mensaje del usuario, pero el LLM
    redacta otra descripción y otra categoría. Es la MISMA operación."""
    await _chat(ac, dueno, _gasto("Pagué 5000 de luz", "Luz", "UTILITIES"), "Pagué 5000 de luz")
    segundo = await _chat(
        ac,
        dueno,
        _gasto("  pagué 5000  de LUZ ", "Pago de luz", "OTHER"),
        "  pagué 5000  de LUZ ",
    )

    assert segundo["message"] == MENSAJE_DUPLICADO_CHAT
    assert await _contar(session, ExpenseEntry, negocio) == 1


async def test_la_misma_venta_confirmada_dos_veces_al_toque_sale_duplicada(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    """Doble clic en «Confirmar» sobre dos pendientes de la MISMA venta (el
    mensaje mandado dos veces): la segunda no se registra."""
    producto = await producto_con_compra(session, negocio.tenant_id)

    await _vender(ac, dueno, _venta(producto))
    segunda = await _vender(ac, dueno, _venta(producto))

    assert segunda["execution_status"] == "DUPLICATE", segunda
    assert await _contar(session, SaleEntry, negocio) == 1


async def test_confirmar_dos_veces_la_misma_pendiente_no_duplica_la_venta(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    producto = await producto_con_compra(session, negocio.tenant_id)
    data = await _chat(ac, dueno, _venta(producto), "vendí una vela a 1000")
    pendiente = data["pending_action_id"]

    with patch("app.application.services.stock_service.EventBus.emit"):
        primera = await ac.post(f"/api/v1/agent/confirm/{pendiente}", headers=dueno)
        segunda = await ac.post(f"/api/v1/agent/confirm/{pendiente}", headers=dueno)

    assert primera.status_code == 200
    assert segunda.status_code == 404
    assert await _contar(session, SaleEntry, negocio) == 1
    accion = (
        await session.execute(select(PendingAction).where(PendingAction.id == uuid.UUID(pendiente)))
    ).scalar_one()
    assert accion.execution_status == "SUCCEEDED"


async def test_dos_tareas_iguales_del_mismo_plan_se_registran_las_dos(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    """«Pagué 5000 de luz y 5000 de luz del depósito» puede salir del LLM como dos
    tareas con el mismo contenido: son dos operaciones, las distingue la posición."""
    user_id = (
        await session.execute(select(User.user_id).where(User.tenant_id == negocio.tenant_id))
    ).scalar_one()
    grupo = f"grupo-{uuid.uuid4().hex[:8]}"
    for posicion in (0, 1):
        session.add(
            PendingAction(
                tenant_id=negocio.tenant_id,
                user_id=user_id,
                action_type="REGISTER_EXPENSE",
                payload={
                    "amount": "5000",
                    "category": "UTILITIES",
                    "description": "Luz",
                    "transaction_date": "2026-10-20",
                    "payment_method": "cash",
                },
                risk_level="MEDIUM",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                approval_group_id=grupo,
                group_position=posicion,
            )
        )
    await session.commit()

    resp = await ac.post(f"/api/v1/agent/confirm/group/{grupo}", headers=dueno)

    assert resp.status_code == 200, resp.text
    assert resp.json()["group_execution_status"] == "SUCCEEDED", resp.json()
    assert await _contar(session, ExpenseEntry, negocio) == 2


async def test_otra_venta_igual_aclarada_en_el_mensaje_se_registra_al_toque(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, dueno: dict[str, str]
) -> None:
    """Lo que sugiere el mensaje de duplicado: «otra venta de $1000». La venta no
    trae `notes`; el mensaje crudo lo estampa el chat en la pendiente."""
    producto = await producto_con_compra(session, negocio.tenant_id)

    await _vender(ac, dueno, _venta(producto), "vendí una vela a 1000")
    segunda = await _vender(ac, dueno, _venta(producto), "otra venta de una vela a 1000")

    assert segunda["execution_status"] == "SUCCEEDED", segunda
    assert await _contar(session, SaleEntry, negocio) == 2
