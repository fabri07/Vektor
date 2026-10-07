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
from app.application.services.ingestion_import_service import _aplicar_categoria_de_catalogo
from app.domain.product_category_inference import CATEGORIA_INFERIDA_FIELD
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


@pytest.mark.usefixtures("legacy_pre_f5_schema")
@pytest.mark.parametrize("reactivado", [False, True], ids=["inactivo", "reactivado_a_mano"])
async def test_revert_no_pisa_un_codigo_puesto_despues_al_duplicado_fuente(
    db_session: AsyncSession, sample_tenant: Tenant, reactivado: bool
) -> None:
    """El apply le liberó el código al duplicado fuente; si después alguien le puso
    OTRO, devolverle el viejo lo pisaría sin avisar. La validación del canónico no
    lo ataja (el canónico sigue con A-001) y el chequeo de colisión mira la clave
    TRANSFERIDA, no la actual del duplicado: la única barrera es el guard de
    ``dup_id == ext_source_id`` en ``_revert_one_group``, y va antes del
    ``if dup.is_active: continue`` para cubrir también la reactivación manual."""
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_pair(
        db_session, tid, canonical_code=None, dup_code=("A-001", "tango")
    )
    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    await _free_shared_sku(db_session, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert dup is not None
    # Por el ORM y no con `update()`: el listener recalcula `external_code_key`.
    dup.external_code = "B-777"
    if reactivado:
        dup.is_active = True
        dup.deactivated_at = None
        dup.deactivation_reason = None
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.groups_reverted == 0
    assert reverted.groups_skipped == 1
    assert reverted.group_results[0]["reason"] == "state_diverged"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canonical is not None and dup is not None
    assert dup.external_code == "B-777"  # la edición sobrevive
    assert dup.is_active is reactivado
    assert (canonical.external_code, canonical.external_source) == ("A-001", "tango")


# ── La categoría inferida viaja con su marca ──────────────────────────────────
#
# El merge completa ``category`` en el canónico desde el duplicado, pero el merge
# de ``custom_fields`` saltea las claves internas (``_``): sin llevar la marca
# explícitamente, una inferida quedaba como declarada en el canónico y un
# catálogo posterior ya no la podía reemplazar.


async def _seed_par_con_categoria(
    session: AsyncSession, tenant_id: uuid.UUID, *, dup_marca: str | None, dup_editado: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    canonical = await _add_product(
        session, tenant_id, name="Lampara", sku="LAMP1", barcode="7790088880008"
    )
    dup = await _add_product(session, tenant_id, name="Lampara x", sku="LAMP1")
    dup.category = "ILUMINACION"
    dup.custom_fields = (
        {CATEGORIA_INFERIDA_FIELD: dup_marca} if dup_marca is not None else {}
    )
    dup.has_user_edits = dup_editado
    await session.commit()
    return canonical.id, dup.id


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_la_inferida_del_duplicado_llega_al_canonico_con_su_marca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, _ = await _seed_par_con_categoria(
        db_session, tid, dup_marca="ILUMINACION", dup_editado=False
    )

    result = await _plan_apply(db_session, tid)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None
    assert canonical.category == "ILUMINACION"
    assert canonical.custom_fields.get(CATEGORIA_INFERIDA_FIELD) == "ILUMINACION"

    # Y por eso un catálogo que DECLARA la suya la puede reemplazar después.
    _aplicar_categoria_de_catalogo(canonical, "DECO", declarada=True)
    assert canonical.category == "DECO"
    assert CATEGORIA_INFERIDA_FIELD not in canonical.custom_fields


@pytest.mark.usefixtures("legacy_pre_f5_schema")
@pytest.mark.parametrize(
    ("dup_marca", "dup_editado"),
    [(None, False), ("DECO", False), ("ILUMINACION", True)],
    ids=["declarada", "marca_muerta", "editado"],
)
async def test_una_categoria_que_no_es_inferida_intacta_no_lleva_marca(
    db_session: AsyncSession, sample_tenant: Tenant, dup_marca: str | None, dup_editado: bool
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, _ = await _seed_par_con_categoria(
        db_session, tid, dup_marca=dup_marca, dup_editado=dup_editado
    )
    if dup_editado:
        # ``has_user_edits`` es el primer criterio para elegir canónico: si sólo
        # el duplicado lo tuviera, pasaría a ser el canónico y la rama de la
        # marca nunca se ejercitaría. Con los dos editados decide el barcode.
        canonical = await db_session.get(Product, canonical_id)
        assert canonical is not None
        canonical.has_user_edits = True
        await db_session.commit()

    result = await _plan_apply(db_session, tid)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None
    assert canonical.category == "ILUMINACION"
    assert CATEGORIA_INFERIDA_FIELD not in (canonical.custom_fields or {})


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_saca_la_categoria_y_la_marca_que_llevo_el_merge(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical_id, _ = await _seed_par_con_categoria(
        db_session, tid, dup_marca="ILUMINACION", dup_editado=False
    )
    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    await _free_shared_sku(db_session, canonical_id)
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None
    assert canonical.category is None
    assert CATEGORIA_INFERIDA_FIELD not in (canonical.custom_fields or {})


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_devuelve_la_marca_previa_que_piso_el_merge(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un canónico sin categoría puede tener una marca muerta (alguien vació la
    categoría a mano): el merge la pisa con la del duplicado y la reversa la
    devuelve tal cual estaba, no la borra."""
    tid = sample_tenant.tenant_id
    canonical_id, _ = await _seed_par_con_categoria(
        db_session, tid, dup_marca="ILUMINACION", dup_editado=False
    )
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None
    canonical.custom_fields = {CATEGORIA_INFERIDA_FIELD: "MUEBLES", "color": "negro"}
    await db_session.commit()

    applied = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    await _free_shared_sku(db_session, canonical_id)
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results

    db_session.expunge_all()
    canonical = await db_session.get(Product, canonical_id)
    assert canonical is not None
    assert canonical.category is None
    assert canonical.custom_fields == {CATEGORIA_INFERIDA_FIELD: "MUEBLES", "color": "negro"}
