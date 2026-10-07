"""B·3 (paso 3 del cierre del #48) — fusionar duplicados no pierde el código externo.

Antes, ``product_dedup_service`` desactivaba al duplicado con su ``external_code``
adentro y el canónico seguía sin código: si el sistema que lo emitió (el ERP, la
planilla del proveedor) lo volvía a mandar, ya no resolvía contra ningún producto
activo. Ahora:

- canónico sin código → recibe código Y fuente del duplicado (liberado antes);
- los dos con código → gana el del canónico y el del duplicado queda como
  conflicto en el ``after_json`` del ``MERGE_PRODUCT``;
- nadie con código → no pasa nada.

La reversa devuelve el código al duplicado, y no lo hace si alguien lo cambió
después del apply. Stock y precio no se tocan. Mismo patrón de siembra que
``test_product_dedup_apply.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.services import product_dedup_service as svc
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Session de COMMIT REAL sobre engine aislado: el apply y la reversa commitean
    por grupo (mismo motivo que en ``test_product_dedup_apply.py``)."""
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


async def _add_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    sku: str | None = None,
    barcode: str | None = None,
    external_code: str | None = None,
    external_source: str | None = None,
    sale_price: Decimal = Decimal("100.00"),
    stock_units: int = 0,
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        barcode=barcode,
        external_code=external_code,
        external_source=external_source,
        sale_price_ars=sale_price,
        stock_units=stock_units,
        custom_fields={},
        is_active=True,
    )
    session.add(p)
    await session.flush()
    return p


async def _seed_pair(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    canonical_code: tuple[str, str] | None,
    dup_code: tuple[str, str] | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Canónico (gana por barcode) + duplicado que comparte el SKU."""
    canonical = await _add_product(
        session,
        tenant_id,
        name="Yerba",
        sku="YERBA1",
        barcode="7790088880008",
        external_code=canonical_code[0] if canonical_code else None,
        external_source=canonical_code[1] if canonical_code else None,
        sale_price=Decimal("321.00"),
    )
    dup = await _add_product(
        session,
        tenant_id,
        name="Yerba x",
        sku="YERBA1",
        external_code=dup_code[0] if dup_code else None,
        external_source=dup_code[1] if dup_code else None,
        sale_price=Decimal("999.00"),
    )
    await session.commit()
    return canonical.id, dup.id


async def _plan_apply(session: AsyncSession, tenant_id: uuid.UUID) -> svc.ApplyResult:
    plan = await svc.plan_dedup(session, tenant_id)
    source_run_id = await svc.persist_dedup_plan(session, tenant_id, plan)
    return await svc.apply_dedup_plan(session, tenant_id, source_run_id, lease_id=None)


async def _merge_after(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    item = (
        await session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == run_id, DataRepairItem.action == "MERGE_PRODUCT"
            )
        )
    ).scalar_one()
    assert item.after_json is not None
    return item.after_json


async def _free_shared_sku(session: AsyncSession, canonical_id: uuid.UUID) -> None:
    """La reversa automática siempre rebota por la clave que formó el grupo (ver
    ``_free_identities`` en ``test_product_dedup_revert.py``): un humano la libera."""
    canonical = await session.get(Product, canonical_id)
    assert canonical is not None
    canonical.sku = None
    canonical.barcode = None
    await session.commit()


# ── Apply ─────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_canonico_sin_codigo_recibe_el_del_duplicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=None, dup_code=("A-001", "tango")
    )

    result = await _plan_apply(db_session, tid)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    assert canonical.external_code == "A-001"
    assert canonical.external_source == "tango"
    assert canonical.external_code_key is not None
    # Liberado en el duplicado: el código vive en UN solo producto.
    assert dup.external_code is None and dup.external_source is None
    assert dup.external_code_key is None
    assert dup.is_active is False
    # Ni stock ni precio.
    assert canonical.sale_price_ars == Decimal("321.00")
    assert dup.sale_price_ars == Decimal("999.00")
    assert canonical.stock_units == 0

    after = await _merge_after(db_session, result.run_id)
    assert after["external_code_transferred"] == {
        "duplicate_id": str(dup_id),
        "external_code": "A-001",
        "external_source": "tango",
    }
    assert after["external_code_conflicts"] == []


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_los_dos_con_codigo_gana_el_canonico_y_se_audita_el_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=("C-1", "tango"), dup_code=("D-9", "planilla")
    )

    result = await _plan_apply(db_session, tid)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    assert (canonical.external_code, canonical.external_source) == ("C-1", "tango")
    # El del duplicado NO se borra: queda en su producto inactivo, como evidencia.
    assert (dup.external_code, dup.external_source) == ("D-9", "planilla")

    after = await _merge_after(db_session, result.run_id)
    assert after["external_code_transferred"] is None
    assert after["external_code_conflicts"] == [
        {"duplicate_id": str(dup_id), "external_code": "D-9", "external_source": "planilla"}
    ]


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_ninguno_con_codigo_no_cambia_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, _dup_id = await _seed_pair(db_session, tid, canonical_code=None, dup_code=None)

    result = await _plan_apply(db_session, tid)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None and canonical.external_code is None
    after = await _merge_after(db_session, result.run_id)
    assert after["external_code_transferred"] is None
    assert after["external_code_conflicts"] == []


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_falla_despues_de_transferir_hace_rollback_del_grupo(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=None, dup_code=("A-001", "tango")
    )

    original = svc._move_external_code

    async def _move_then_fail(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        raise RuntimeError("falla simulada después de mover el código")

    monkeypatch.setattr(svc, "_move_external_code", _move_then_fail)

    result = await _plan_apply(db_session, tid)
    assert result.status == "FAILED"
    assert result.groups_failed == 1

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    # Nada del grupo quedó: el código sigue en el duplicado, que sigue activo.
    assert canonical.external_code is None
    assert (dup.external_code, dup.external_source) == ("A-001", "tango")
    assert dup.is_active is True


# ── Reversa ───────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_devuelve_el_codigo_al_duplicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=None, dup_code=("A-001", "tango")
    )
    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    await _free_shared_sku(db_session, canonical_id)
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    assert canonical.external_code is None and canonical.external_code_key is None
    assert (dup.external_code, dup.external_source) == ("A-001", "tango")
    assert dup.external_code_key is not None
    assert dup.is_active is True


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_no_pisa_un_codigo_editado_despues_del_apply(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=None, dup_code=("A-001", "tango")
    )
    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    await _free_shared_sku(db_session, canonical_id)
    # Alguien corrigió el código del canónico después de la fusión.
    await db_session.execute(
        update(Product).where(Product.id == canonical_id).values(external_code="A-002")
    )
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.groups_reverted == 0
    assert reverted.groups_skipped == 1
    assert reverted.group_results[0]["reason"] == "state_diverged"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    assert canonical.external_code == "A-002"  # la edición sobrevive
    assert dup.external_code is None
    assert dup.is_active is False  # el grupo entero quedó como estaba


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_rebota_si_otro_producto_tomo_el_codigo_del_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El duplicado que conservó su código (conflicto) vuelve a ser ACTIVO al
    revertir: si mientras tanto otro producto tomó ese código, reactivarlo violaría
    el índice. Se saltea el grupo en vez de romper con IntegrityError."""
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=("C-1", "tango"), dup_code=("D-9", "planilla")
    )
    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    await _free_shared_sku(db_session, canonical_id)
    await _add_product(
        db_session, tid, name="Otro", external_code="D-9", external_source="planilla"
    )
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.groups_reverted == 0
    assert reverted.group_results[0]["reason"] == "revert_identity_collision"

    db_session.expunge_all()
    dup = await db_session.get(Product, dup_id)
    assert dup is not None and dup.is_active is False
