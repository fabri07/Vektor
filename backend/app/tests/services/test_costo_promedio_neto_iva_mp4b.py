"""M·P4b — el costo promedio en la misma base que la reposición de la venta.

Cada compra entra al promedio con ``base_comparable`` (la regla de la venta). Un
Responsable Inscripto promedia netos de IVA; una compra suya sin neto confirmado
deja sus unidades sin costo comparable (``incompleto``), nunca mezcla un costo
con IVA. Monotributo e informal no cambian: su base es el costo crudo.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.application.services.stock_service import increment_stock
from app.config.settings import get_settings
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.costo_promedio import (
    MOTIVO_SIN_COSTO,
    MOTIVO_SIN_COSTO_RI,
    ContratoDelMovimiento,
    Evento,
    costo_comparable,
    proyectar,
)
from app.jobs import recalcular_costo_promedio as job
from app.persistence import costo_promedio_pendiente as pend
from app.persistence.models.business import BusinessProfile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.services.test_costeo_de_ventas import _a_las, _producto, _regimen

RI = "responsable_inscripto"
CENT = Decimal("0.01")

# ── Dominio ──────────────────────────────────────────────────────────────────

_NETO = ContratoDelMovimiento("incluido", "incluido", None, Decimal("21"), True)
_SIN_CONFIRMAR = ContratoDelMovimiento("incluido", "incluido", None, Decimal("21"), None)


@pytest.mark.parametrize(
    ("clase", "contrato", "regimen", "esperado"),
    [
        # RI con IVA recuperable confirmado: neto, como la reposición.
        ("compra", _NETO, RI, "100"),
        # RI sin confirmar si se recupera: no se sabe el neto.
        ("compra", _SIN_CONFIRMAR, RI, None),
        # RI: compra anterior al contrato (todo NULL).
        ("compra", ContratoDelMovimiento(), RI, None),
        # RI: la apertura y una entrada declarada nunca confirman.
        ("apertura", _NETO, RI, None),
        ("entrada", _NETO, RI, None),
        # Monotributo: el IVA integra el costo (incluido: ya está).
        ("compra", ContratoDelMovimiento("incluido", "incluido", None, None, False),
         "monotributo", "121"),
        # Fuera del RI, una estimación sigue con el crudo (igual que su reposición).
        ("compra", ContratoDelMovimiento(), "monotributo", "121"),
        ("apertura", ContratoDelMovimiento(), None, "121"),
        ("compra", ContratoDelMovimiento(), "registered", "121"),
    ],
)
def test_costo_comparable(
    clase: Any, contrato: ContratoDelMovimiento, regimen: str | None, esperado: str | None
) -> None:
    got = costo_comparable(Decimal("121"), clase, contrato, regimen)
    assert got == (None if esperado is None else Decimal(esperado))


def test_sin_costo_sigue_sin_costo() -> None:
    assert costo_comparable(None, "compra", _NETO, RI) is None
    assert costo_comparable(Decimal(0), "compra", _NETO, "monotributo") == 0


def test_el_motivo_del_ri_dice_por_que() -> None:
    from datetime import datetime  # noqa: PLC0415

    ev = Evento(datetime(2026, 3, 1, 12), "compra", 10, None, datetime(2026, 3, 1), uuid.uuid4())
    (_, generico), = proyectar([ev])
    (_, ri), = proyectar([ev], motivo_sin_costo=MOTIVO_SIN_COSTO_RI)
    assert generico.motivo == MOTIVO_SIN_COSTO
    assert (ri.estado, ri.motivo) == (generico.estado, MOTIVO_SIN_COSTO_RI)


# ── Con el ledger real ───────────────────────────────────────────────────────

HOY = today_ar()


def _contrato(costo: str, iva: str | None, recuperable: bool | None) -> ContratoDeCosto:
    return ContratoDeCosto(
        unit_cost_final=Decimal(costo), shipping_treatment="incluido", tax_treatment="incluido",
        tax_amount=None if iva is None else Decimal(iva), tax_recoverable=recuperable,
    )


async def _comprar(
    db: AsyncSession, tenant: Tenant, p: Product, qty: int, contrato: ContratoDeCosto, dias: int
) -> InventoryMovement:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        return await increment_stock(
            p.id, tenant.tenant_id, qty, contrato.unit_cost_final, None, db,
            occurred_at=_a_las(HOY - timedelta(days=dias)), contrato=contrato,
        )


async def _vender(
    db: AsyncSession, tenant: Tenant, p: Product, fecha: Any, qty: int
) -> SaleEntry:
    """Venta que descuenta stock (el promedio lee el ledger)."""
    venta = SaleEntry(
        tenant_id=tenant.tenant_id, product_id=p.id, amount=Decimal("400") * qty,
        quantity=qty, transaction_date=fecha, payment_method="cash",
    )
    db.add(venta)
    await db.flush()
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await stock_service.decrement_stock(
            p.id, tenant.tenant_id, qty, stock_service.sale_source_event_id(venta.id), db,
            occurred_at=fecha,
        )
    await db.commit()
    return venta


async def _historial(
    db: AsyncSession, tenant: Tenant, regimen: str, recuperable: bool | None
) -> Product:
    """10 a $121 (IVA $21), se venden 5, 10 a $242 (IVA $42)."""
    await _regimen(db, tenant, regimen)
    p = await _producto(db, tenant)
    await _comprar(db, tenant, p, 10, _contrato("121", "21", recuperable), 5)
    await _vender(db, tenant, p, _a_las(HOY - timedelta(days=4)), qty=5)
    await _comprar(db, tenant, p, 10, _contrato("242", "42", recuperable), 3)
    return p


async def test_ri_promedia_netos_y_la_venta_compara_en_la_misma_base(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _historial(db_session, sample_tenant, RI, True)
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY - timedelta(days=1)), qty=1)
    r = await svc.reconstruir(db_session, tid, medir_brecha=True)

    vigente = await svc.costo_a(db_session, tid, p.id, HOY)
    assert vigente is not None and vigente.estado == "medido"
    # (5 × 100 + 10 × 200) / 15, netos. Con el crudo daría 201,67.
    assert vigente.costo_promedio is not None
    assert vigente.costo_promedio.quantize(CENT) == Decimal("166.67")
    await db_session.refresh(venta)
    assert (venta.cost_unit_ars, venta.cost_confirmed) == (Decimal("200.00"), True)
    assert venta.cost_avg_unit_ars == Decimal("166.67")
    # La brecha del backfill compara contra la reposición NETA: 1 − 166,67/200.
    assert [b.quantize(CENT) for b in r.brechas] == [Decimal("0.17")]


async def test_ri_con_una_compra_sin_neto_confirmado_queda_incompleto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _regimen(db_session, sample_tenant, RI)
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, 10, _contrato("242", "42", True), 5)
    await _comprar(db_session, sample_tenant, p, 10, _contrato("121", "21", None), 3)
    await svc.reconstruir(db_session, tid)

    vigente = await svc.costo_a(db_session, tid, p.id, HOY)
    assert vigente is not None
    assert (vigente.estado, vigente.motivo) == ("incompleto", MOTIVO_SIN_COSTO_RI)
    # El parcial es el de las unidades netas: nunca mezcla el costo con IVA.
    assert vigente.costo_promedio == Decimal("200")


async def test_monotributo_promedia_el_crudo_como_antes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _historial(db_session, sample_tenant, "monotributo", False)
    await svc.reconstruir(db_session, tid)

    vigente = await svc.costo_a(db_session, tid, p.id, HOY)
    assert vigente is not None and vigente.estado == "medido"
    assert vigente.costo_promedio is not None
    assert vigente.costo_promedio.quantize(CENT) == Decimal("201.67")


# ── Qué hace recalcular ──────────────────────────────────────────────────────


@pytest.fixture
def sin_encolar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pend, "ENCOLAR_EN_HILO", False)
    monkeypatch.setattr(job.recalcular_costo_promedio, "apply_async", lambda *a, **k: None)


async def _estado(db: AsyncSession, p: Product) -> ProductCostState:
    st = (
        await db.execute(
            select(ProductCostState)
            .where(ProductCostState.product_id == p.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return st


async def _publicado(db: AsyncSession, tenant: Tenant) -> None:
    await db.commit()
    await svc.reconstruir(db, tenant.tenant_id)
    await db.commit()


async def test_cambiar_el_regimen_recalcula_todo_el_tenant(
    db_session: AsyncSession, sample_tenant: Tenant, sin_encolar: None
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    a = await _producto(db_session, sample_tenant, name="A")
    b = await _producto(db_session, sample_tenant, name="B")
    await _comprar(db_session, sample_tenant, a, 10, _contrato("121", "21", True), 3)
    await _publicado(db_session, sample_tenant)
    assert (await _estado(db_session, a)).pendiente_desde is None

    perfil = (
        await db_session.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()
    perfil.fiscal_condition = RI
    await db_session.commit()

    # Hasta el producto sin historial: el régimen es de todo el tenant.
    assert (await _estado(db_session, a)).pendiente_desde == date.min
    assert (await _estado(db_session, b)).pendiente_desde == date.min


async def test_el_mismo_regimen_no_marca(
    db_session: AsyncSession, sample_tenant: Tenant, sin_encolar: None
) -> None:
    await _regimen(db_session, sample_tenant, RI)
    a = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, a, 10, _contrato("121", "21", True), 3)
    await _publicado(db_session, sample_tenant)

    perfil = (
        await db_session.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()
    perfil.fiscal_condition = RI
    await db_session.commit()

    assert (await _estado(db_session, a)).pendiente_desde is None


async def test_cambiar_el_contrato_de_una_compra_marca_desde_su_dia(
    db_session: AsyncSession, sample_tenant: Tenant, sin_encolar: None
) -> None:
    await _regimen(db_session, sample_tenant, RI)
    a = await _producto(db_session, sample_tenant)
    mov = await _comprar(db_session, sample_tenant, a, 10, _contrato("121", "21", None), 3)
    await _publicado(db_session, sample_tenant)

    mov.tax_recoverable = True
    await db_session.commit()

    assert (await _estado(db_session, a)).pendiente_desde == HOY - timedelta(days=3)
    await svc.reconstruir(db_session, sample_tenant.tenant_id)
    vigente = await svc.costo_a(db_session, sample_tenant.tenant_id, a.id, HOY)
    assert vigente is not None and vigente.estado == "medido"
    assert vigente.costo_promedio == Decimal("100")


# ── GET /products ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("regimen", "sin_iva"), [(RI, True), ("monotributo", False)])
async def test_productos_rotula_el_promedio_sin_iva_del_ri(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    regimen: str,
    sin_iva: bool,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _historial(db_session, sample_tenant, regimen, regimen == RI)
    await svc.reconstruir(db_session, tid)
    monkeypatch.setattr(get_settings(), "AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS", [str(tid)])

    resp = await client.get("/api/v1/products", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    fila = next(f for f in resp.json() if f["id"] == str(p.id))
    assert fila["avg_cost_estado"] == "medido"
    assert fila["avg_cost_sin_iva"] is sin_iva
