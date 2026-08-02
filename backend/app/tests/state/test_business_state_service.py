"""
Tests for Business State Service.

Uses:
  - SQLite in-memory DB (via conftest fixtures).
  - FakeRedis: a simple dict-backed mock that satisfies the get/set interface
    used by compute_business_state, so no real Redis connection is needed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.state.business_state_service import (
    BusinessState,
    ProductSummary,
    _cache_key,
    _deserialize_state,
    _hash_key,
    _serialize_state,
    compute_business_state,
)

# ── Fake Redis ────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal in-memory Redis stub (get / set with optional ex)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the current store (for assertion)."""
        return dict(self._store)


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def kiosco_profile(
    db_session: AsyncSession, sample_business_profile: BusinessProfile
) -> BusinessProfile:
    """El perfil de `sample_tenant` con las estimaciones de onboarding cargadas."""
    now = datetime.now(UTC)
    bp = sample_business_profile
    bp.monthly_sales_estimate_ars = Decimal("150000.00")
    bp.monthly_inventory_spend_estimate_ars = Decimal("90000.00")
    bp.monthly_fixed_expenses_estimate_ars = Decimal("20000.00")
    bp.cash_on_hand_estimate_ars = Decimal("15000.00")
    bp.supplier_count_estimate = 2
    bp.product_count_estimate = 3
    bp.onboarding_completed = True
    bp.updated_at = now
    await db_session.commit()
    return bp


# ── Test 1: onboarding-only data ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_business_state_from_onboarding_only(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    When there are no real transactions, estimates from BusinessProfile are used.

    Los seis inputs salen de la encuesta, así que cada uno vale
    `PESO_DECLARADO` (0.4) de su peso completo:
      ventas      25 × 0.4 = 10  (estimate > 0)
      mercaderia  20 × 0.4 =  8  (estimate > 0)
      gastos      15 × 0.4 =  6  (estimate > 0)
      caja        20 × 0.4 =  8  (cash_source = "onboarding" → declarado)
      productos    0            (estimate = 3, < 5)
      proveedores 10 × 0.4 =  4  (estimate = 2, >= 1)
      total       36 → LOW

    Antes daba 90 → HIGH: los `*_est` ya traían las estimaciones adentro, así
    que la métrica premiaba haber contestado el formulario. Un negocio del que
    no se registró ni un movimiento no puede habilitar análisis.
    """
    redis = FakeRedis()
    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.tenant_id == sample_tenant.tenant_id
    assert state.vertical_code == Vertical.KIOSCO_ALMACEN.value
    assert state.monthly_sales_est == Decimal("150000.00")
    assert state.monthly_inventory_cost_est == Decimal("90000.00")
    assert state.monthly_fixed_expenses_est == Decimal("20000.00")
    assert state.cash_on_hand_est == Decimal("15000.00")
    assert state.supplier_count == 2
    assert state.product_count == 3  # estimate, < 5

    # completeness: (25+20+15+20+10) × 0.4 = 36
    assert state.data_completeness_score == 36.0
    assert state.confidence_level == "LOW"
    # Y lo dice con todas las letras, en vez de callarse (antes: `None`).
    assert state.main_concern == "Datos insuficientes para un análisis confiable"

    # Redis should be populated
    store = redis.snapshot()
    # Las keys llevan el prefijo de versión v4: `data_completeness_score` cambió
    # de significado (pondera por procedencia), y un blob v3 deserializa sin
    # error — serviría la confianza vieja durante las 24 h del TTL.
    assert _cache_key(sample_tenant.tenant_id) in store
    assert _hash_key(sample_tenant.tenant_id) in store
    assert "business_state:v4:" in _cache_key(sample_tenant.tenant_id)
    assert "last_inputs_hash:v4:" in _hash_key(sample_tenant.tenant_id)


# ── Montos del onboarding sin contestar ──────────────────────────────────────


@pytest.mark.asyncio
async def test_montos_sin_contestar_no_se_leen_como_cero(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Con la caja sin contestar, la fuente no puede ser "onboarding".

    El tiering de caja ya distinguía `is not None` desde antes, así que este
    test **no protege un fix**: documenta un camino que hasta ahora era
    inalcanzable. El formulario mandaba `parseFloat(campo) || 0`, de modo que
    `cash_on_hand_estimate_ars` nunca quedaba NULL después de un onboarding y
    esta rama no se ejercitaba nunca en la práctica.

    Ahora sí se llega, y lo que hace es lo correcto: sin saldo declarado el
    score cae al tier siguiente en vez de operar sobre un $0 inventado.
    """
    bp = kiosco_profile
    bp.monthly_inventory_spend_estimate_ars = None
    bp.monthly_fixed_expenses_estimate_ars = None
    bp.cash_on_hand_estimate_ars = None
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.cash_source != "onboarding"
    assert state.cash_source in ("flujo", "desconocido")


@pytest.mark.asyncio
async def test_cero_declarado_si_es_el_estimado_del_onboarding(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """El espejo: un cero CONTESTADO sí es el saldo del negocio.

    Sin este test, el de arriba pasaría igual con un `or Decimal("0")` que
    tratara todo cero como ausencia. Los dos juntos fijan la distinción.
    """
    bp = kiosco_profile
    bp.cash_on_hand_estimate_ars = Decimal("0")
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.cash_source == "onboarding"
    assert state.cash_on_hand_est == Decimal("0")


# ── Test 2: completeness increases with ≥5 active products ───────────────────


@pytest.mark.asyncio
async def test_completeness_increases_with_products(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    Adding ≥5 active products should add 10 pts to completeness vs. baseline.
    Baseline (test_1): 36 pts (todo declarado, product_count_estimate = 3).
    Los 5 productos son reales, así que suman los 10 puntos COMPLETOS (no el
    0.4 de un dato declarado): 36 + 10 = 46.

    Sigue en LOW a propósito. Cargar el catálogo no es cargar el negocio: sin
    una venta ni un gasto registrado no hay con qué medir salud financiera.
    """
    # Add 5 active products
    for i in range(5):
        p = Product(
            tenant_id=sample_tenant.tenant_id,
            name=f"Producto {i}",
            sale_price_ars=Decimal("1000.00"),
            stock_units=10,
            is_active=True,
        )
        db_session.add(p)
    await db_session.commit()

    redis = FakeRedis()
    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    # product_count now = 5 (real), so +10 pts
    assert state.product_count == 5
    assert state.data_completeness_score == 46.0
    assert state.confidence_level == "LOW"
    assert len(state.products) == 5


# ── Test 3: cache is used when no new inputs ─────────────────────────────────


@pytest.mark.asyncio
async def test_cache_is_used_when_no_new_inputs(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    Calling compute_business_state twice without any DB changes should return
    the cached state on the second call (same snapshot_id).
    """
    redis = FakeRedis()

    state1 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )
    state2 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    # Same snapshot — no recomputation
    assert state1.snapshot_id == state2.snapshot_id
    assert state1.data_completeness_score == state2.data_completeness_score


# ── Test 4: cache invalidates when new sale is added ─────────────────────────


@pytest.mark.asyncio
async def test_cache_invalidates_when_new_sale_added(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    After a new SaleEntry is inserted the fingerprint changes, so the second
    call must recompute and produce a different (newer) snapshot_id.
    """
    redis = FakeRedis()

    # First call — populates cache
    state1 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    # Insert a new sale
    sale = SaleEntry(
        tenant_id=sample_tenant.tenant_id,
        amount=Decimal("5000.00"),
        quantity=1,
        transaction_date=datetime.now(UTC).date(),
        payment_method="cash",
    )
    db_session.add(sale)
    await db_session.commit()

    # Second call — fingerprint is different → full recompute
    state2 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    assert state1.snapshot_id != state2.snapshot_id
    # A tiny amount of real data blends with onboarding instead of replacing it.
    assert state2.monthly_sales_est == Decimal("150000.000")


@pytest.mark.asyncio
async def test_blend_sales_few_transactions(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    today = datetime.now(UTC).date()
    for i in range(5):
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("1000.00"),
                quantity=1,
                transaction_date=today - timedelta(days=4 - i),
                payment_method="cash",
            )
        )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    # 5 sales over 5 days projects to 30000 monthly, then blends 70/30.
    assert state.monthly_sales_est == Decimal("114000.000")


@pytest.mark.asyncio
async def test_blend_sales_many_transactions(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    today = datetime.now(UTC).date()
    for i in range(60):
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("100.00"),
                quantity=1,
                transaction_date=today - timedelta(days=i % 30),
                payment_method="cash",
            )
        )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.monthly_sales_est == Decimal("6000.00")


@pytest.mark.asyncio
async def test_product_summaries_include_30_day_rotation_units(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Yerba",
        sale_price_ars=Decimal("1200.00"),
        stock_units=12,
        low_stock_threshold_units=3,
        is_active=True,
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add(
        SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            product_id=product.id,
            amount=Decimal("2400.00"),
            quantity=2,
            transaction_date=datetime.now(UTC).date(),
            payment_method="cash",
        )
    )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert len(state.products) == 1
    assert state.products[0].units_sold_30d == 2


# ── Serialización del blob de caché ──────────────────────────────────────────


def _estado_de_ejemplo(vertical_code: str = Vertical.LIMPIEZA.value) -> BusinessState:
    return BusinessState(
        snapshot_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        vertical_code=vertical_code,
        data_completeness_score=70.0,
        confidence_level="MEDIUM",
        monthly_sales_est=Decimal("100000.00"),
        monthly_inventory_cost_est=Decimal("60000.00"),
        monthly_fixed_expenses_est=Decimal("20000.00"),
        cash_on_hand_est=Decimal("30000.00"),
        product_count=4,
        supplier_count=2,
        products=[
            ProductSummary(
                product_id=uuid.uuid4(),
                name="Detergente",
                stock_units=10,
                low_stock_threshold_units=3,
                sale_price_ars=Decimal("1500.00"),
                units_sold_30d=7,
            )
        ],
        main_concern=None,
    )


def test_el_blob_conserva_el_vertical_en_su_propia_clave() -> None:
    """El vertical viaja en `vertical_code`, no escondido dentro del ruleset.

    Antes el vertical se guardaba en `d["ruleset"]` (el ruleset no era
    serializable, así que se lo reemplazaba por su código) y `_deserialize_state`
    lo leía de ahí. Al eliminar el campo muerto había que darle su propia clave o
    el estado volvía de la caché sin saber de qué rubro era.
    """
    estado = _estado_de_ejemplo()
    blob = json.loads(_serialize_state(estado))

    assert blob["vertical_code"] == Vertical.LIMPIEZA.value
    assert "ruleset" not in blob


def test_round_trip_de_serializacion() -> None:
    estado = _estado_de_ejemplo()
    vuelta = _deserialize_state(_serialize_state(estado))

    assert vuelta.vertical_code == estado.vertical_code
    assert vuelta.tenant_id == estado.tenant_id
    assert vuelta.monthly_sales_est == estado.monthly_sales_est
    assert vuelta.cash_on_hand_est == estado.cash_on_hand_est
    assert len(vuelta.products) == 1
    assert vuelta.products[0].units_sold_30d == 7


def test_un_vertical_desconocido_en_la_cache_no_pasa_silencioso() -> None:
    """Un blob con un rubro fuera del catálogo levanta en vez de scorearse con otro."""
    blob = _serialize_state(_estado_de_ejemplo(vertical_code="rubro_inexistente"))
    with pytest.raises(ValueError, match="rubro_inexistente"):
        _deserialize_state(blob)


# ── Completeness: observado vs declarado ─────────────────────────────────────
#
# `data_completeness_score` es la compuerta de la no-invention rule: por debajo
# de 50 la UI muestra empty state y los jobs no persisten análisis. Pero se
# calculaba sobre los `*_est`, que caen a las estimaciones del onboarding
# cuando no hay datos reales — así que medía "¿contestó la encuesta?", no
# "¿tengo datos?". Contestar los 4 montos daba 80 (HIGH) con la base vacía, y
# el dashboard mostraba un score seguro de un negocio del que no se sabe nada.


@pytest.mark.asyncio
async def test_solo_encuesta_no_alcanza_la_compuerta_de_no_invencion(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Sin una sola transacción real, la confianza no puede habilitar análisis."""
    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    # El perfil tiene los cuatro montos contestados y la base no tiene nada.
    assert state.monthly_sales_est > 0
    assert state.confidence_level == "LOW"
    assert state.data_completeness_score < 50.0


async def _cargar_ventas(
    db_session: AsyncSession, tenant_id: uuid.UUID, cantidad: int
) -> None:
    hoy = datetime.now(UTC).date()
    for i in range(cantidad):
        db_session.add(
            SaleEntry(
                tenant_id=tenant_id,
                amount=Decimal("5000.00"),
                quantity=1,
                transaction_date=hoy - timedelta(days=(i % 25) + 1),
                payment_method="cash",
            )
        )
    await db_session.commit()


@pytest.mark.asyncio
async def test_ventas_reales_valen_mas_que_la_estimacion_declarada(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Con ventas suficientes para dominar el blend, ventas pasa a observado."""
    solo_encuesta = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    await _cargar_ventas(db_session, sample_tenant.tenant_id, 12)

    con_ventas = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert con_ventas.data_completeness_score > solo_encuesta.data_completeness_score


@pytest.mark.asyncio
async def test_una_venta_suelta_no_abre_la_compuerta(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Pocas ventas NO alcanzan para cobrar los 25 puntos de la dimensión.

    Con `sale_count < 10`, `monthly_sales_est` sigue siendo 70% la estimación
    declarada. Acreditar ahí la dimensión completa dejaría al dashboard
    mostrando un score armado sobre un revenue mayormente inventado por el
    formulario — con UNA venta de $500 encima de los cuatro montos declarados,
    la cuenta daba 51 y la compuerta de no-invención se abría sola.
    """
    await _cargar_ventas(db_session, sample_tenant.tenant_id, 1)

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.confidence_level == "LOW"
    assert state.data_completeness_score < 50.0


async def _cargar_compra_de_mercaderia(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category: str,
    expense_type: str,
    amount: str = "30000.00",
) -> None:
    db_session.add(
        ExpenseEntry(
            tenant_id=tenant_id,
            amount=Decimal(amount),
            description="Compra a proveedor",
            category=category,
            expense_type=expense_type,
            transaction_date=datetime.now(UTC).date() - timedelta(days=3),
            # A cuenta corriente a propósito: un método líquido movería además la
            # dimensión de CAJA (`cash_observado` mira los movimientos líquidos) y
            # el test dejaría de aislar la de mercadería.
            payment_method="account",
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_la_compra_de_mercaderia_real_le_gana_a_la_estimacion(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Una compra de mercadería registrada tiene que contar como dato OBSERVADO.

    El filtro buscaba `category == "mercaderia"`, que es un ALIAS: el catálogo
    canónico lo normaliza a `INVENTORY` (`expense_categories.py`) y ningún writer
    persiste esa forma. El predicado no podía ser verdadero nunca, así que la
    dimensión quedaba clavada en 20 × 0.4 = 8 por más compras reales que hubiera
    — 12 puntos inalcanzables contra una compuerta que corta en 50.
    """
    await _cargar_compra_de_mercaderia(
        db_session, sample_tenant.tenant_id, category="INVENTORY", expense_type="COGS"
    )

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    # El monto real le gana a los $90.000 declarados en el onboarding.
    assert state.monthly_inventory_cost_est == Decimal("30000.00")
    # Y la dimensión pasa a valer su peso completo: 36 − 8 + 20 = 48.
    assert state.data_completeness_score == pytest.approx(48.0)


@pytest.mark.asyncio
async def test_compra_de_mercaderia_legacy_sin_backfill_tambien_cuenta(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """La migración `20260710_0001` agregó `expense_type` con server_default
    'OPEX' y SIN backfill: toda compra de mercadería anterior quedó marcada OPEX
    aunque su categoría sea INVENTORY. Mirar solo `expense_type` dejaría afuera
    justo el historial que le da sustento al score.
    """
    await _cargar_compra_de_mercaderia(
        db_session, sample_tenant.tenant_id, category="INVENTORY", expense_type="OPEX"
    )

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.monthly_inventory_cost_est == Decimal("30000.00")


@pytest.mark.asyncio
async def test_un_gasto_operativo_no_cuenta_como_mercaderia(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """No-regresión: el alquiler no es costo de mercadería. Sin este límite, el
    fix se comería cualquier gasto y la dimensión se acreditaría sola."""
    await _cargar_compra_de_mercaderia(
        db_session, sample_tenant.tenant_id, category="RENT", expense_type="OPEX"
    )

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    # Sigue en la estimación declarada y en 20 × 0.4.
    assert state.monthly_inventory_cost_est == Decimal("90000.00")


@pytest.mark.asyncio
async def test_la_caja_observada_no_depende_del_calendario(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Los mismos datos no pueden valer distinto por la antigüedad del alta.

    El tiering de `cash_source` prefiere el estimado del onboarding por sobre el
    flujo durante los 7 días del alta. Si la procedencia de la caja se leyera de
    ahí, un tenant con movimientos líquidos reales valdría 8 puntos el día 1 y
    20 el día 8 sin que cambie un solo dato.
    """
    await _cargar_ventas(db_session, sample_tenant.tenant_id, 12)

    # Día 1: el perfil se acaba de tocar → el tiering elige "onboarding".
    recien_dado_de_alta = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )
    assert recien_dado_de_alta.cash_source == "onboarding"

    # Día 8: mismos datos, sólo envejeció el perfil → el tiering elige "flujo".
    kiosco_profile.updated_at = datetime.now(UTC) - timedelta(days=8)
    await db_session.commit()

    pasada_una_semana = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )
    assert pasada_una_semana.cash_source == "flujo"

    assert (
        recien_dado_de_alta.data_completeness_score
        == pasada_una_semana.data_completeness_score
    )
