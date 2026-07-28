"""Ningún camino de agentes / orquestador / API asume kiosco cuando falta el rubro.

Cierra la última tanda de fallbacks silenciosos a `kiosco_almacen`: un tenant sin
`BusinessProfile` (o con un `vertical_code` no canónico) tiene que fallar ruidoso
o pedir configuración, nunca scorearse con las heurísticas de otro rubro.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.fields import _get_vertical_code
from app.application.agents.expense.agent import AgentExpense
from app.application.agents.health.agent import AgentHealth
from app.application.agents.income.agent import AgentIncome
from app.application.agents.shared.schemas import AgentRequest, AgentResponse, RiskLevel
from app.application.agents.shared.vertical_lookup import load_tenant_vertical
from app.application.agents.stock.agent import AgentStock
from app.application.services.chat_orchestrator import ChatOrchestrator
from app.domain.verticals import UnknownVerticalError, Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.tests.conftest import add_business_profile

_ORCHESTRATOR = "app.application.services.chat_orchestrator"

#: Código corto legado, el que todavía vive en producción hasta la migración de
#: unificación. `parse_vertical` NO lo aliasea a propósito.
_VERTICAL_LEGACY = "kiosco"


async def _tenant_sin_perfil(session: AsyncSession) -> Tenant:
    """Tenant huérfano de `BusinessProfile` — el estado roto de los signups viejos."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Negocio Sin Perfil",
        display_name="Negocio Sin Perfil",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    session.add(tenant)
    await session.commit()
    return tenant


async def _tenant_con_vertical_legacy(session: AsyncSession) -> Tenant:
    """Tenant CON perfil pero con `vertical_code` no canónico.

    Es el estado que había en Neon antes de la migración de unificación:
    `business_profiles.vertical_code` guardaba el código corto `"kiosco"`.

    El CHECK `ck_business_profiles_vertical_code` (migración `20260806_0002`,
    espejado en el modelo) ahora vuelve ese estado **inescribible** — que es el
    punto de la migración. Para poder seguir probando que las capas de
    aplicación son un backstop INDEPENDIENTE del CHECK (si mañana alguien
    dropea la constraint, o si aparece un código raro por otra vía), la fila se
    fuerza suspendiendo el CHECK solo mientras dura el INSERT.

    `PRAGMA ignore_check_constraints` es de conexión y la fixture `db_session`
    comparte una sola conexión (StaticPool): se restaura en `finally` sí o sí,
    porque dejarla prendida desactivaría los CHECK del resto de la suite.

    El `rollback()` del `finally` NO es decorativo: si el `commit()` falla con un
    error real de base, la sesión queda en *pending-rollback* y el `execute` que
    apaga el PRAGMA **también** explota — el PRAGMA quedaría prendido para todos
    los tests siguientes del worker, en silencio, y la excepción del `finally`
    taparía la original. El assert final convierte esa fuga en un fallo ruidoso.
    """
    tenant = await _tenant_sin_perfil(session)
    await session.execute(text("PRAGMA ignore_check_constraints=ON"))
    try:
        session.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                vertical_code=_VERTICAL_LEGACY,
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=False,
            )
        )
        await session.commit()
    finally:
        if not session.is_active:
            await session.rollback()
        await session.execute(text("PRAGMA ignore_check_constraints=OFF"))
        prendido = (
            await session.execute(text("PRAGMA ignore_check_constraints"))
        ).scalar()
        assert not prendido, (
            "FUGA: el PRAGMA quedó prendido y los CHECK del resto de la suite "
            "están desactivados"
        )
    return tenant


# ── El CHECK de `business_profiles` (backstop a nivel DB) ─────────────────────


@pytest.mark.parametrize("codigo", [_VERTICAL_LEGACY, "otros", "ferreteria"])
async def test_business_profiles_rechaza_un_vertical_no_canonico(
    db_session: AsyncSession, codigo: str
) -> None:
    """`ck_business_profiles_vertical_code` es *la* garantía a nivel DB de que no
    vuelven los verticales desconocidos. Vive en la migración `20260806_0002` y
    espejado en el modelo — sin el espejo, ninguna base de test lo tendría
    (el conftest arma el esquema con `create_all`, nunca con Alembic) y este
    test pasaría en verde sin que la constraint existiera en ningún lado."""
    tenant = await _tenant_sin_perfil(db_session)
    db_session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            vertical_code=codigo,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()

    await db_session.rollback()


async def test_business_profiles_acepta_los_tres_canonicos(
    db_session: AsyncSession,
) -> None:
    """Control positivo: sin esto el test de arriba podría estar pasando por una
    columna NOT NULL sin llenar en vez de por el CHECK."""
    for vertical in Vertical:
        tenant = await _tenant_sin_perfil(db_session)
        await add_business_profile(db_session, tenant.tenant_id, vertical)
    await db_session.flush()


# ── Helper compartido ─────────────────────────────────────────────────────────


async def test_load_tenant_vertical_devuelve_el_rubro_real(db_session: AsyncSession) -> None:
    tenant = await _tenant_sin_perfil(db_session)
    await add_business_profile(db_session, tenant.tenant_id, Vertical.LIMPIEZA)
    await db_session.commit()

    assert await load_tenant_vertical(db_session, tenant.tenant_id) == Vertical.LIMPIEZA


async def test_load_tenant_vertical_sin_perfil_levanta(db_session: AsyncSession) -> None:
    tenant = await _tenant_sin_perfil(db_session)

    with pytest.raises(UnknownVerticalError):
        await load_tenant_vertical(db_session, tenant.tenant_id)


async def test_load_tenant_vertical_codigo_no_canonico_levanta(db_session: AsyncSession) -> None:
    """El código corto legado NO se aliasea: se rechaza igual que uno inventado."""
    tenant = await _tenant_con_vertical_legacy(db_session)

    with pytest.raises(UnknownVerticalError, match=_VERTICAL_LEGACY):
        await load_tenant_vertical(db_session, tenant.tenant_id)


# ── API /fields ───────────────────────────────────────────────────────────────


async def test_fields_sin_perfil_es_404(db_session: AsyncSession) -> None:
    """Servirle el set de campos de kiosco a un tenant sin perfil enmascaraba el
    estado roto: ahora es 404 explícito."""
    tenant = await _tenant_sin_perfil(db_session)

    with pytest.raises(HTTPException) as exc:
        await _get_vertical_code(tenant.tenant_id, db_session)

    assert exc.value.status_code == 404
    assert exc.value.detail == "business_profile_not_found"


async def test_fields_vertical_no_canonico_es_409_no_500(db_session: AsyncSession) -> None:
    """Con perfil pero `vertical_code` legado, `/fields` traduce el fallo a una
    respuesta de dominio en vez de caer al handler global (500 genérico)."""
    tenant = await _tenant_con_vertical_legacy(db_session)

    with pytest.raises(HTTPException) as exc:
        await _get_vertical_code(tenant.tenant_id, db_session)

    assert exc.value.status_code == 409
    assert exc.value.detail == "vertical_no_canonico"


async def test_fields_con_perfil_devuelve_el_vertical(db_session: AsyncSession) -> None:
    tenant = await _tenant_sin_perfil(db_session)
    await add_business_profile(db_session, tenant.tenant_id, Vertical.DECORACION_HOGAR)
    await db_session.commit()

    assert await _get_vertical_code(tenant.tenant_id, db_session) == Vertical.DECORACION_HOGAR


# ── Agentes: sin DB y sin default inyectado, levantan ─────────────────────────


@pytest.mark.parametrize(
    ("agente", "nombre"),
    [
        (AgentExpense(), "AgentExpense"),
        (AgentStock(), "AgentStock"),
        (AgentIncome(), "AgentIncome"),
    ],
)
async def test_agente_sin_db_ni_default_levanta(agente, nombre: str) -> None:
    with pytest.raises(RuntimeError, match=nombre):
        await agente._business_vertical(uuid.uuid4())


@pytest.mark.parametrize(
    "agente",
    [
        AgentExpense(default_vertical=Vertical.LIMPIEZA),
        AgentStock(default_vertical=Vertical.LIMPIEZA),
        AgentIncome(default_vertical=Vertical.LIMPIEZA),
    ],
)
async def test_agente_usa_el_default_inyectado(agente) -> None:
    assert await agente._business_vertical(uuid.uuid4()) == Vertical.LIMPIEZA


async def test_agente_con_db_ignora_el_default(db_session: AsyncSession) -> None:
    """El default inyectado es SOLO para cuando no hay DB: con sesión manda el
    perfil del tenant."""
    tenant = await _tenant_sin_perfil(db_session)
    await add_business_profile(db_session, tenant.tenant_id, Vertical.DECORACION_HOGAR)
    await db_session.commit()

    agente = AgentStock(db=db_session, default_vertical=Vertical.LIMPIEZA)
    assert await agente._business_vertical(tenant.tenant_id) == Vertical.DECORACION_HOGAR


# ── AgentHealth ───────────────────────────────────────────────────────────────


async def test_health_vertical_code_no_se_traga_las_excepciones() -> None:
    """El `except Exception: return "kiosco_almacen"` convertía cualquier falla de
    DB en un score de otro rubro. Ahora propaga."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db caída"))
    agente = AgentHealth(db=db)

    with pytest.raises(RuntimeError, match="db caída"):
        await agente._vertical_code(uuid.uuid4())


async def test_health_sin_perfil_pide_configurar_el_negocio(db_session: AsyncSession) -> None:
    """`process` traduce la falta de perfil en el mismo empty state honesto que ya
    existía para `collect`, sin narrar un score."""
    tenant = await _tenant_sin_perfil(db_session)
    agente = AgentHealth(db=db_session)
    agente.client = MagicMock()

    response = await agente.process(
        AgentRequest(
            user_id=str(uuid.uuid4()),
            business_id=str(tenant.tenant_id),
            message="¿cómo está mi negocio?",
        )
    )

    assert response.status == "requires_clarification"
    assert "perfil de negocio" in response.result["summary"].lower()


# ── ChatOrchestrator ──────────────────────────────────────────────────────────


async def test_orchestrator_sin_perfil_levanta(db_session: AsyncSession) -> None:
    tenant = await _tenant_sin_perfil(db_session)

    with patch(f"{_ORCHESTRATOR}.get_anthropic_async_client"):
        orchestrator = ChatOrchestrator()

    with pytest.raises(UnknownVerticalError):
        await orchestrator._load_business_context(tenant.tenant_id, db_session)


async def test_orchestrator_sin_perfil_pide_configurar(db_session: AsyncSession) -> None:
    """`handle` no contesta con números de kiosco: pide configurar el negocio."""
    tenant = await _tenant_sin_perfil(db_session)
    with patch(f"{_ORCHESTRATOR}.get_anthropic_async_client"):
        orchestrator = ChatOrchestrator()

    response = await orchestrator.handle(
        request=AgentRequest(
            user_id=str(uuid.uuid4()),
            business_id=str(tenant.tenant_id),
            message="¿cuánto vendí este mes?",
        ),
        db=db_session,
        redis=MagicMock(),
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
    )

    assert isinstance(response, AgentResponse)
    assert response.status == "requires_clarification"
    assert response.risk_level == RiskLevel.LOW
    assert response.message is not None
    assert "todavía no está configurado" in response.message


async def test_orchestrator_vertical_no_canonico_degrada(db_session: AsyncSession) -> None:
    """Con perfil pero `vertical_code` legado el chat degrada al mismo pedido de
    configuración — no revienta ni contesta con heurísticas de otro rubro."""
    tenant = await _tenant_con_vertical_legacy(db_session)
    with patch(f"{_ORCHESTRATOR}.get_anthropic_async_client"):
        orchestrator = ChatOrchestrator()

    response = await orchestrator.handle(
        request=AgentRequest(
            user_id=str(uuid.uuid4()),
            business_id=str(tenant.tenant_id),
            message="¿cuánto vendí este mes?",
        ),
        db=db_session,
        redis=MagicMock(),
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
    )

    assert response.status == "requires_clarification"
    assert response.message is not None
    assert "todavía no está configurado" in response.message


# ── AgentChat ─────────────────────────────────────────────────────────────────


async def test_chat_sin_vertical_no_nombra_ningun_rubro() -> None:
    """Camino de saludo / sin agente: el prompt omite el rubro en vez de
    atribuirle uno inventado."""
    from app.application.agents.chat.agent import AgentChat

    agente = AgentChat()
    request = AgentRequest(user_id=str(uuid.uuid4()), business_id=str(uuid.uuid4()), message="hola")
    agent_response = AgentResponse(
        request_id=request.request_id,
        agent_name="none",
        status="success",
        risk_level=RiskLevel.LOW,
        result={},
    )

    sin_rubro = await agente._build_system_prompt(
        request=request,
        agent_response=agent_response,
        business_name="el negocio",
        business_type=None,
        heuristics=None,
        conversation_ctx={},
        agent_memory_fragment="",
        file_context="",
        tenant_id=None,
        db=None,
        redis=None,
    )
    con_rubro = await agente._build_system_prompt(
        request=request,
        agent_response=agent_response,
        business_name="el negocio",
        business_type=Vertical.LIMPIEZA,
        heuristics=None,
        conversation_ctx={},
        agent_memory_fragment="",
        file_context="",
        tenant_id=None,
        db=None,
        redis=None,
    )

    assert "kiosco" not in sin_rubro.lower()
    assert "para el negocio." in sin_rubro
    assert "para el negocio (Limpieza)." in con_rubro


# ── AgentIncome: extracción de venta con el rubro REAL ────────────────────────


async def test_extraccion_de_venta_usa_las_heuristicas_del_rubro_real(
    db_session: AsyncSession,
) -> None:
    """El camino vivo de extracción de venta tenía el rubro kiosco hardcodeado:
    cualquier tenant recibía las heurísticas de kiosco en el prompt del LLM."""
    import json

    tenant = await _tenant_sin_perfil(db_session)
    await add_business_profile(db_session, tenant.tenant_id, Vertical.DECORACION_HOGAR)
    await db_session.commit()

    entidades = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    bloque = MagicMock()
    bloque.text = json.dumps(entidades)
    respuesta = MagicMock()
    respuesta.content = [bloque]
    respuesta.usage = MagicMock(input_tokens=10, output_tokens=5)

    agente = AgentIncome(db=db_session)
    agente.client = MagicMock()
    agente.client.messages.create = AsyncMock(return_value=respuesta)

    await agente.process(
        AgentRequest(
            user_id=str(uuid.uuid4()),
            business_id=str(tenant.tenant_id),
            message="vendí 5000 pesos al contado",
        )
    )

    call = agente.client.messages.create.await_args
    assert call is not None
    system = call.kwargs["system"]
    assert "decoracion_hogar" in system
    assert "kiosco_almacen" not in system
