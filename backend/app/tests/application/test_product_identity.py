"""Identidad fuerte de producto (Fase 5) — helpers de get-or-create y guard.

Los índices únicos parciales los crea la migración ``20260802_0001`` (PR B); acá se
crean dentro de la transacción del test (que hace rollback al terminar) para poder
ejercitar el comportamiento REAL de la colisión en vez de mockear ``flush``. Cuando
PR B los declare en el ORM, este fixture desaparece.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.product_identity import (
    ProductIdentityConflictError,
    _violated_identity_index,
    add_product_or_reuse,
    find_active_by_identity,
    product_identity_guard,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_DDL = (
    "CREATE UNIQUE INDEX uq_products_tenant_barcode_norm ON products "
    "(tenant_id, barcode_normalized) WHERE is_active "
    "AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''",
    "CREATE UNIQUE INDEX uq_products_tenant_sku_norm ON products "
    "(tenant_id, sku_normalized) WHERE is_active "
    "AND sku_normalized IS NOT NULL AND sku_normalized <> ''",
)


@pytest_asyncio.fixture
async def f5_indexes(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    """Crea los uniques parciales de F5 en la DB del test (SQLite los impone)."""
    for stmt in _DDL:
        await db_session.execute(text(stmt))
    await db_session.flush()
    yield
    for name in ("uq_products_tenant_barcode_norm", "uq_products_tenant_sku_norm"):
        await db_session.execute(text(f"DROP INDEX IF EXISTS {name}"))


def _product(tenant_id: uuid.UUID, **kw: object) -> Product:
    base: dict[str, object] = {
        "tenant_id": tenant_id,
        "name": "Producto",
        "sale_price_ars": Decimal("100"),
    }
    base.update(kw)
    return Product(**base)


# ── find_active_by_identity ──────────────────────────────────────────────────


async def test_find_prioriza_barcode_sobre_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El barcode identifica el artículo físico; el SKU es el código del comercio."""
    tid = sample_tenant.tenant_id
    por_barcode = _product(tid, name="Coca", barcode="7790011110001")
    por_sku = _product(tid, name="Otra", sku="COCA500")
    db_session.add_all([por_barcode, por_sku])
    await db_session.flush()

    found, matched = await find_active_by_identity(
        db_session, tid, barcode="7790011110001", sku="COCA500"
    )
    assert matched == "barcode"
    assert found is not None and found.id == por_barcode.id


async def test_find_ignora_inactivos(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    """El índice es parcial sobre is_active — el lookup tiene que espejarlo."""
    tid = sample_tenant.tenant_id
    db_session.add(_product(tid, sku="BAJA", is_active=False))
    await db_session.flush()

    found, matched = await find_active_by_identity(db_session, tid, sku="BAJA")
    assert found is None and matched is None


async def test_find_respeta_exclude_id_y_tenant(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    propio = _product(tid, sku="SKU1")
    ajeno = _product(second_tenant.tenant_id, sku="SKU1")
    db_session.add_all([propio, ajeno])
    await db_session.flush()

    # No se reporta a sí mismo (caso PATCH).
    found, _ = await find_active_by_identity(db_session, tid, sku="SKU1", exclude_id=propio.id)
    assert found is None
    # El del otro tenant no interfiere.
    found, _ = await find_active_by_identity(db_session, tid, sku="SKU1")
    assert found is not None and found.id == propio.id


# ── add_product_or_reuse ─────────────────────────────────────────────────────


async def test_add_camino_feliz(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    product, creado = await add_product_or_reuse(
        db_session, _product(sample_tenant.tenant_id, sku="NUEVO")
    )
    assert creado is True
    assert product.id is not None


async def test_add_reusa_ante_colision_de_sku(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """Colisión por clave fuerte = match exacto, no ambigüedad → se reusa."""
    tid = sample_tenant.tenant_id
    existente = _product(tid, name="Coca 500", sku="COCA500")
    db_session.add(existente)
    await db_session.flush()

    product, creado = await add_product_or_reuse(
        db_session, _product(tid, name="Coca", sku="COCA500")
    )
    assert creado is False
    assert product.id == existente.id
    # No se creó un tercer producto.
    rows = await db_session.execute(select(Product.id).where(Product.tenant_id == tid))
    assert len(rows.scalars().all()) == 1


async def test_add_raise_levanta_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    tid = sample_tenant.tenant_id
    existente = _product(tid, barcode="7790011110001")
    db_session.add(existente)
    await db_session.flush()

    with pytest.raises(ProductIdentityConflictError) as caught:
        await add_product_or_reuse(
            db_session, _product(tid, barcode="7790011110001"), on_conflict="raise"
        )
    assert caught.value.matched_by == "barcode"
    assert caught.value.existing.id == existente.id


async def test_add_con_barcode_libre_resuelve_al_dueño_del_sku(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """Colisión sólo de sku, con barcode propio libre → reusa al dueño del sku.

    Nota: este caso NO discrimina la corrección del relookup (con el barcode libre,
    consultar ambas claves también cae al sku). Es cobertura del camino feliz; el
    caso que sí discrimina es el de ambigüedad, abajo.
    """
    tid = sample_tenant.tenant_id
    a = _product(tid, name="A", barcode="7790011110001")
    b = _product(tid, name="B", sku="SKU-B")
    db_session.add_all([a, b])
    await db_session.flush()

    product, creado = await add_product_or_reuse(
        db_session, _product(tid, name="Candidato", barcode="7790099990009", sku="SKU-B")
    )
    assert creado is False
    assert product.id == b.id


async def test_add_no_reusa_cuando_barcode_y_sku_son_de_productos_distintos(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """Regresión del relookup: el candidato "es" dos productos a la vez.

    Barcode de A, sku de B. El código anterior reconsultaba con AMBAS claves y la
    prioridad de barcode devolvía A, así que el import reusaba A aunque el índice
    violado pudiera ser el de B — fusionando identidades distintas en silencio.
    Ahora es ambigüedad explícita: no se reusa ninguno ni siquiera en modo ``reuse``,
    para que el import lo mande a "Otros" en vez de decidir por su cuenta.
    """
    tid = sample_tenant.tenant_id
    dueño_barcode = _product(tid, name="A", barcode="7790011110001")
    dueño_sku = _product(tid, name="B", sku="SKU-B")
    db_session.add_all([dueño_barcode, dueño_sku])
    await db_session.flush()

    with pytest.raises(ProductIdentityConflictError) as caught:
        await add_product_or_reuse(
            db_session, _product(tid, name="Candidato", barcode="7790011110001", sku="SKU-B")
        )
    assert caught.value.ambiguous is True


async def test_add_exige_producto_transient(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Es EL error fácil de cometer: si el caller agrega antes, el INSERT sale
    fuera del savepoint y la colisión aborta la transacción entera."""
    candidato = _product(sample_tenant.tenant_id, sku="YA-AGREGADO")
    db_session.add(candidato)

    with pytest.raises(AssertionError, match="TRANSIENT"):
        await add_product_or_reuse(db_session, candidato)


async def test_add_deja_la_sesion_usable_y_no_pierde_pendientes(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """Tras la colisión la transacción sigue viva y lo pendiente ajeno sobrevive."""
    tid = sample_tenant.tenant_id
    db_session.add(_product(tid, name="Ocupante", sku="OCUPADO"))
    await db_session.flush()

    ajeno = _product(tid, name="Ajeno", sku="AJENO")
    db_session.add(ajeno)  # pendiente, sin relación con el helper

    product, creado = await add_product_or_reuse(db_session, _product(tid, sku="OCUPADO"))
    assert creado is False

    # El ajeno se drenó ANTES del savepoint → no lo expungó _restore_snapshot.
    rows = await db_session.execute(
        select(Product.name).where(Product.tenant_id == tid).order_by(Product.name)
    )
    assert rows.scalars().all() == ["Ajeno", "Ocupante"]
    # Y la sesión sigue escribiendo.
    otro, creado_otro = await add_product_or_reuse(db_session, _product(tid, sku="POSTERIOR"))
    assert creado_otro is True and otro.id is not None


async def test_add_no_traga_violaciones_ajenas(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """Un NOT NULL roto no puede convertirse en 'ya existía, lo reuso'."""
    invalido = _product(sample_tenant.tenant_id, sku="X")
    invalido.name = None  # type: ignore[assignment]  # NOT NULL a propósito

    with pytest.raises(IntegrityError):
        await add_product_or_reuse(db_session, invalido)


# ── product_identity_guard ───────────────────────────────────────────────────


async def test_guard_traduce_colision_en_patch(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    """PATCH que mueve el SKU a uno ocupado → conflicto, con la mutación DENTRO."""
    tid = sample_tenant.tenant_id
    ocupante = _product(tid, name="Ocupante", sku="TOMADO")
    editado = _product(tid, name="Editado", sku="LIBRE")
    db_session.add_all([ocupante, editado])
    await db_session.flush()

    with pytest.raises(ProductIdentityConflictError) as caught:
        async with product_identity_guard(
            db_session, tenant_id=tid, sku="TOMADO", exclude_id=editado.id
        ):
            editado.sku = "TOMADO"

    assert caught.value.matched_by == "sku"
    assert caught.value.existing.id == ocupante.id


async def test_guard_camino_feliz_persiste_la_mutacion(
    db_session: AsyncSession, sample_tenant: Tenant, f5_indexes: None
) -> None:
    tid = sample_tenant.tenant_id
    producto = _product(tid, name="Original", sku="ANTES")
    db_session.add(producto)
    await db_session.flush()

    async with product_identity_guard(
        db_session, tenant_id=tid, sku="DESPUES", exclude_id=producto.id
    ):
        producto.sku = "DESPUES"

    refreshed = await db_session.get(Product, producto.id)
    assert refreshed is not None
    assert refreshed.sku == "DESPUES"
    assert refreshed.sku_normalized == "despues"  # normalize_sku baja a minúsculas


# ── clasificador ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mensaje", "esperado"),
    [
        # Forma PostgreSQL (por nombre embebido en el texto).
        ('duplicate key value violates unique constraint "uq_products_tenant_sku_norm"', "sku"),
        (
            'duplicate key value violates unique constraint "uq_products_tenant_barcode_norm"',
            "barcode",
        ),
        # Forma SQLite (por columnas — NO menciona el nombre del índice).
        ("UNIQUE constraint failed: products.tenant_id, products.sku_normalized", "sku"),
        ("UNIQUE constraint failed: products.tenant_id, products.barcode_normalized", "barcode"),
        # Violaciones ajenas → None (se re-propagan).
        ("NOT NULL constraint failed: products.name", None),
        ("FOREIGN KEY constraint failed", None),
        ('insert violates foreign key constraint "products_tenant_id_fkey"', None),
        ("UNIQUE constraint failed: products.tenant_id, products.name_normalized", None),
    ],
)
def test_clasificador_de_constraint(mensaje: str, esperado: str | None) -> None:
    assert _violated_identity_index(IntegrityError(mensaje, None, Exception(mensaje))) == esperado
