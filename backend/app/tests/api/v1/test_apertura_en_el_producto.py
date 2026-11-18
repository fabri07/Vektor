"""El stock inicial del producto se lee y se corrige desde «Editar producto».

- ``GET /products/{id}`` trae ``apertura`` (fecha, quién la puso, si tiene costo, si se puede
  corregir): la pantalla no tiene que adivinar nada.
- ``POST /products/{id}/opening-cost`` carga el costo de un stock inicial que se cargó SIN
  costo (no es fuente con ninguna fecha). Un costo que ya existe no se corrige acá.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import (
    FECHA_DE_ARCHIVO,
    SOURCE_CATALOG_INITIAL_STOCK,
)
from app.application.services.respaldo_service import primeros_dias_de_fuente
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

pytestmark = pytest.mark.asyncio

HOY = today_ar()
HACE_30 = HOY - timedelta(days=30)
HACE_10 = HOY - timedelta(days=10)


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _archivo(db: AsyncSession, tenant_id: uuid.UUID) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tenant_id, uploaded_by=None,
        original_filename="catalogo.xlsx", s3_key=f"{uuid.uuid4()}.xlsx",
        content_type="application/vnd.ms-excel", size_bytes=100, purpose="ingestion",
    )
    db.add(f)
    await db.commit()
    return f


async def _producto(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    nombre: str = "Yerba",
    costo: str | None = "60",
    upload_id: uuid.UUID | None = None,
    sin_apertura: bool = False,
    activo: bool = True,
    fuente_de_fecha: str | None = None,
) -> Product:
    p = Product(
        tenant_id=tenant_id, name=nombre, sale_price_ars=Decimal("100"), stock_units=20,
        is_active=activo,
    )
    db.add(p)
    await db.flush()
    if not sin_apertura:
        db.add(
            InventoryMovement(
                tenant_id=tenant_id, product_id=p.id, movement_type="adjustment", qty=20,
                unit_cost=Decimal(costo) if costo is not None else None,
                source_type=SOURCE_CATALOG_INITIAL_STOCK, source_event_id="import",
                source_upload_id=upload_id, opening_date_source=fuente_de_fecha,
                occurred_at=datetime.combine(HOY, datetime.min.time(), UTC),
            )
        )
    await db.commit()
    await db.refresh(p)
    return p


async def _apertura(db: AsyncSession, pid: uuid.UUID) -> InventoryMovement:
    await db.commit()
    return (
        await db.execute(
            select(InventoryMovement)
            .where(
                InventoryMovement.product_id == pid,
                InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
            )
            .execution_options(populate_existing=True)
        )
    ).scalars().one()


# ── Lectura: GET /products/{id} ─────────────────────────────────────────────

async def test_el_detalle_trae_el_stock_inicial_cargado_sin_confirmar(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id)
    r = await client.get(f"/api/v1/products/{p.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    ap = r.json()["apertura"]
    assert (ap["origen"], ap["confirmada"], ap["con_costo"], ap["editable"]) == (
        "carga", False, True, True
    )
    assert Decimal(ap["costo"]) == Decimal("60")
    assert ap["fecha"] == HOY.isoformat()
    assert r.json()["primer_dia_de_fuente"] == HOY.isoformat()


async def test_el_detalle_distingue_la_fecha_que_trajo_el_archivo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    f = await _archivo(db_session, sample_tenant.tenant_id)
    p = await _producto(
        db_session, sample_tenant.tenant_id, upload_id=f.id, fuente_de_fecha=FECHA_DE_ARCHIVO
    )
    ap = (await client.get(f"/api/v1/products/{p.id}", headers=auth_headers)).json()["apertura"]
    assert ap["origen"] == "archivo"


async def test_un_stock_inicial_sin_costo_lo_dice_y_no_es_fuente(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id, costo=None)
    r = (await client.get(f"/api/v1/products/{p.id}", headers=auth_headers)).json()
    assert (r["apertura"]["con_costo"], r["apertura"]["costo"]) == (False, None)
    assert r["primer_dia_de_fuente"] is None


async def test_dos_cargas_de_archivos_distintos_no_se_pueden_corregir_acá(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    f1, f2 = await _archivo(db_session, tid), await _archivo(db_session, tid)
    p = await _producto(db_session, tid, upload_id=f1.id)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=5,
            unit_cost=Decimal("60"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", source_upload_id=f2.id,
        )
    )
    await db_session.commit()
    ap = (await client.get(f"/api/v1/products/{p.id}", headers=auth_headers)).json()["apertura"]
    assert ap["editable"] is False
    assert "2 cargas" in ap["motivo_no_editable"]


async def test_sin_stock_inicial_o_dado_de_baja_no_hay_bloque(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    sin = await _producto(db_session, tid, nombre="Sin", sin_apertura=True)
    baja = await _producto(db_session, tid, nombre="Baja", activo=False)
    for p in (sin, baja):
        r = await client.get(f"/api/v1/products/{p.id}", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["apertura"] is None


async def test_el_detalle_no_ve_el_stock_inicial_de_otro_negocio(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, second_tenant: Tenant,
) -> None:
    ajeno = await _producto(db_session, second_tenant.tenant_id)
    r = await client.get(f"/api/v1/products/{ajeno.id}", headers=auth_headers)
    assert r.status_code == 404, r.text


# ── Escritura: POST /products/{id}/opening-cost ─────────────────────────────

async def test_cargar_el_costo_hace_del_stock_inicial_una_fuente_y_audita(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, costo=None)
    assert await primeros_dias_de_fuente(db_session, tid, [p.id]) == {}

    r = await client.post(
        f"/api/v1/products/{p.id}/opening-cost", json={"unit_cost_ars": "75.50"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    mov = await _apertura(db_session, p.id)
    assert (mov.unit_cost, mov.unit_cost_final) == (Decimal("75.50"), Decimal("75.50"))
    assert mov.shipping_treatment == "desconocido"
    assert (await primeros_dias_de_fuente(db_session, tid, [p.id])) == {p.id: HOY}
    log = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == tid,
                DecisionAuditLog.decision_type == "OPENING_COST_SET",
            )
        )
    ).scalars().one()
    assert log.decision_data["unit_cost_ars"] == "75.50"


async def test_cargar_el_costo_costea_las_ventas_que_estaban_sin_costo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, costo=None)
    venta = SaleEntry(
        tenant_id=tid, product_id=p.id, amount=Decimal("400"), quantity=2,
        transaction_date=datetime.combine(HOY, datetime.min.time()).replace(hour=12),
        payment_method="cash",
    )
    db_session.add(venta)
    await db_session.commit()
    assert (await db_session.get(SaleEntry, venta.id, populate_existing=True)).cost_origin == (  # type: ignore[union-attr]
        "sin_costo"
    )

    r = await client.post(
        f"/api/v1/products/{p.id}/opening-cost", json={"unit_cost_ars": "70"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["ventas_sin_costo_desde_la_fecha"] == 1
    await db_session.commit()
    v = await db_session.get(SaleEntry, venta.id, populate_existing=True)
    assert v is not None
    assert (v.cost_origin, v.cost_ars) == ("stock_inicial", Decimal("140.00"))


async def test_un_costo_que_ya_existe_no_se_corrige_acá(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id, costo="60")
    r = await client.post(
        f"/api/v1/products/{p.id}/opening-cost", json={"unit_cost_ars": "99"},
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "OPENING_HAS_COST"
    assert (await _apertura(db_session, p.id)).unit_cost == Decimal("60")


async def test_cargar_el_costo_con_dos_cargas_candidatas_es_409(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    f1, f2 = await _archivo(db_session, tid), await _archivo(db_session, tid)
    p = await _producto(db_session, tid, costo=None, upload_id=f1.id)
    db_session.add(
        InventoryMovement(
            tenant_id=tid, product_id=p.id, movement_type="adjustment", qty=5,
            source_type=SOURCE_CATALOG_INITIAL_STOCK, source_event_id="import",
            source_upload_id=f2.id,
        )
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/products/{p.id}/opening-cost", json={"unit_cost_ars": "70"},
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "OPENING_AMBIGUOUS"


async def test_cargar_el_costo_sin_stock_inicial_baja_o_ajeno_es_404_y_costo_cero_422(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, second_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    sin = await _producto(db_session, tid, nombre="Sin", sin_apertura=True)
    baja = await _producto(db_session, tid, nombre="Baja", costo=None, activo=False)
    ajeno = await _producto(db_session, second_tenant.tenant_id, costo=None)
    for p in (sin, baja, ajeno):
        r = await client.post(
            f"/api/v1/products/{p.id}/opening-cost", json={"unit_cost_ars": "70"},
            headers=auth_headers,
        )
        assert r.status_code == 404, (p.name, r.text)
    con = await _producto(db_session, tid, nombre="Con", costo=None)
    r = await client.post(
        f"/api/v1/products/{con.id}/opening-cost", json={"unit_cost_ars": "0"},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert (await _apertura(db_session, con.id)).unit_cost is None
