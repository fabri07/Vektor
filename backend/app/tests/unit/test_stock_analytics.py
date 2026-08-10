"""Tests de los handlers analíticos de AgentStock (Sprint 17, Stages 2-3)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.application.agents.stock.agent import AgentStock
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry


def _task(intent: str) -> AgentTask:
    action = (
        ActionType.ANALYZE_PRICES
        if intent
        in (
            "analizar_margenes_productos",
            "sugerir_precios_venta",
            "identificar_productos_sensibles",
            "simular_actualizacion_precios",
        )
        else ActionType.ANALYZE_STOCK_DATA
    )
    return AgentTask(agent="agent_stock", action_type=action, entities={"_intent": intent})


@pytest_asyncio.fixture
async def seeded_tenant(db_session: AsyncSession) -> uuid.UUID:
    from app.persistence.models.tenant import Tenant

    tid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    products = [
        Product(
            tenant_id=tid,
            name="Coca 500",
            sale_price_ars=Decimal("1000"),
            unit_cost_ars=Decimal("600"),
            stock_units=10,
            is_active=True,
        ),
        Product(
            tenant_id=tid,
            name="Pan lactal",
            sale_price_ars=Decimal("800"),
            unit_cost_ars=Decimal("620"),
            stock_units=0,
            is_active=True,
        ),
        Product(
            tenant_id=tid,
            name="Sin costo",
            sale_price_ars=Decimal("500"),
            unit_cost_ars=None,
            stock_units=100,
            is_active=True,
        ),
    ]
    db_session.add_all(products)
    await db_session.commit()
    # ventas recientes de Coca para velocidad
    coca = products[0]
    for i in range(10):
        db_session.add(
            SaleEntry(
                tenant_id=tid,
                product_id=coca.id,
                amount=Decimal("1000"),
                quantity=1,
                transaction_date=date.today() - timedelta(days=i),
                payment_method="cash",
            )
        )
    await db_session.commit()
    return tid


def _req(tenant_id: uuid.UUID, msg: str = "") -> AgentRequest:
    return AgentRequest(user_id=str(uuid.uuid4()), business_id=str(tenant_id), message=msg)


async def test_analizar_margenes(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(
        _req(seeded_tenant, "qué margen tengo"), task=_task("analizar_margenes_productos")
    )
    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_margenes_productos"
    assert resp.result["structured_data"]["without_cost_count"] == 1
    assert "Coca" in resp.message


async def test_sugerir_precios(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(_req(seeded_tenant), task=_task("sugerir_precios_venta"))
    assert resp.status == "success"
    assert resp.result["structured_data"]["suggestions"]


async def test_simular_sin_pct_pide_aclaracion(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(
        _req(seeded_tenant, "simulá un aumento"), task=_task("simular_actualizacion_precios")
    )
    assert resp.status == "requires_clarification"


async def test_simular_con_pct(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(
        _req(seeded_tenant, "simulá un aumento del 12%"),
        task=_task("simular_actualizacion_precios"),
    )
    assert resp.status == "success"
    assert resp.result["structured_data"]["simulation"]["pct"] == 12.0


async def test_detectar_quiebres(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(_req(seeded_tenant), task=_task("detectar_quiebres_stock"))
    assert resp.status == "success"
    # Pan lactal tiene stock 0 → quiebre
    assert "Pan lactal" in resp.message


async def test_estimar_dias_stock(db_session, seeded_tenant):
    agent = AgentStock(db=db_session)
    resp = await agent.process(_req(seeded_tenant), task=_task("estimar_dias_stock"))
    assert resp.status == "success"
    # Coca: 10 u / 1 u por día ≈ 10 días
    assert "Coca" in resp.message


@pytest_asyncio.fixture
async def tenant_with_price_file(db_session: AsyncSession):
    """Tenant con catálogo (Coca costo 600/venta 1000) + archivo de proveedor adjunto."""
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
    from app.persistence.models.tenant import Tenant

    tid = uuid.uuid4()
    uid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    db_session.add(
        Product(
            tenant_id=tid,
            name="Coca 500",
            sale_price_ars=Decimal("1000"),
            unit_cost_ars=Decimal("600"),
            stock_units=10,
            is_active=True,
        )
    )
    # archivo del proveedor: Coca a costo 720 (aumento 20%)
    f = UploadedFile(
        tenant_id=tid,
        uploaded_by=uid,
        original_filename="prov.xlsx",
        s3_key="k",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": "stock",
            "confidence": "HIGH",
            "stock_detectado": [{"nombre": "Coca 500", "costo": 720}],
            "ventas_detectadas": [],
            "gastos_detectados": [],
        },
    )
    db_session.add(f)
    await db_session.commit()
    return tid, str(f.id)


def _req_att(tenant_id, file_id, msg=""):
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tenant_id),
        message=msg,
        attachments=[{"file_id": file_id, "filename": "prov.xlsx"}],
    )


async def test_detectar_aumentos_proveedor(db_session, tenant_with_price_file):
    tid, fid = tenant_with_price_file
    agent = AgentStock(db=db_session)
    task = AgentTask(
        agent="agent_stock",
        action_type=ActionType.ANALYZE_PRICES,
        entities={"_intent": "detectar_aumentos_proveedor"},
    )
    resp = await agent.process(_req_att(tid, fid, "cuánto me aumentó"), task=task)
    assert resp.status == "success"
    assert resp.result["summary"] == "detectar_aumentos_proveedor"
    incs = resp.result["structured_data"]["increases"]
    assert incs and incs[0]["delta_pct"] == 20.0  # 600 → 720


async def test_analizar_lista_precios_con_margen(db_session, tenant_with_price_file):
    tid, fid = tenant_with_price_file
    agent = AgentStock(db=db_session)
    task = AgentTask(
        agent="agent_stock",
        action_type=ActionType.ANALYZE_PRICES,
        entities={"_intent": "analizar_lista_precios"},
    )
    # reusar el adjunto: lista con Coca; el handler cruza fuzzy y calcula margen
    resp = await agent.process(_req_att(tid, fid, "analizá esta lista"), task=task)
    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_lista_precios"
    assert resp.result["structured_data"]["matched"] >= 1


async def test_importar_archivo_productos_routed_to_income():
    # fix: importar_archivo_productos debe rutear a agent_income (stock no maneja IMPORT)
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_AGENT, build_plan

    assert INTENT_TO_AGENT["importar_archivo_productos"] == "agent_income"
    plan = build_plan("importar_archivo_productos", {})
    assert plan.tasks[0].agent == "agent_income"
    assert plan.tasks[0].action_type == ActionType.IMPORT_TABULAR_FILE


async def test_empty_catalog(db_session):
    from app.persistence.models.tenant import Tenant

    tid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="E",
            display_name="E",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await db_session.commit()
    agent = AgentStock(db=db_session)
    resp = await agent.process(_req(tid), task=_task("analizar_margenes_productos"))
    assert resp.result["summary"] == "SIN_PRODUCTOS"
