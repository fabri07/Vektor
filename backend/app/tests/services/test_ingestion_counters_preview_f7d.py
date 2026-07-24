"""F7d — contadores reconciliados de maestro + reread de maestros.

Extiende F7c (`test_ingestion_reference_resolution_f7c.py` cubre la taxonomía de
resolución por fila ventas_cliente_*/compras_proveedor_*) SIN rehacerla. Este
módulo cubre:

  - desglose de maestro (clientes_creados/actualizados/needs_review/invalidos,
    espejo en proveedores) — F7d extendió `ImportResult` (F7b) con
    `needs_review`/`invalid` para poder distinguirlos;
  - backward-compat: un summary viejo sin los buckets/counts nuevos no rompe
    `insert_confirmed_data`/`check_nonempty_import`;
  - reread de maestros (`reread_service._reread_master_entities`): upsert
    idempotente que preserva ediciones manuales no provistas por el archivo,
    needs_review/conflicto nunca mergea, y sin `master_column_mappings`
    guardado no reaplica nada (no se adivina el shape).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services import reread_service
from app.config.settings import get_settings
from app.integrations.s3 import S3Client
from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_VALID_DNI = "30111222"
_OTHER_DNI = "40987654"


# ── 1. Desglose de maestro (creados/actualizados/needs_review/invalidos) ───────


def _clientes_mixed_summary() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {"context_id": "table", "entity_type": "customer", "headers": ["nombre", "documento"]}
        ],
        "clientes_detectados": [
            {"nombre": "Juan Perez", "documento": _VALID_DNI},  # create
            {"nombre": "Sin Documento"},  # needs_review: sin ninguna clave fuerte
            {"nombre": "Doc Invalido", "documento": "abc"},  # invalid: DNI no válido
        ],
    }


@pytest.mark.asyncio
async def test_import_maestro_clientes_desglosa_creado_needs_review_invalido(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _clientes_mixed_summary(),
        {"clientes": True},
        column_mappings={"nombre": "name", "documento": "dni"},
    )
    assert counts["clientes"] == 1
    assert counts["clientes_creados"] == 1
    assert counts["clientes_actualizados"] == 0
    assert counts["clientes_needs_review"] == 1
    assert counts["clientes_invalidos"] == 1

    # needs_review/invalido NUNCA se persisten: solo existe el creado.
    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1
    assert customers[0].name == "Juan Perez"


@pytest.mark.asyncio
async def test_import_maestro_clientes_actualiza_existente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    existing = Customer(
        tenant_id=sample_tenant.tenant_id, name="Juan P.", dni=_VALID_DNI
    )
    db_session.add(existing)
    await db_session.flush()

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "clientes",
            "mapping_contexts": [
                {
                    "context_id": "table",
                    "entity_type": "customer",
                    "headers": ["nombre", "documento"],
                }
            ],
            "clientes_detectados": [{"nombre": "Juan Perez", "documento": _VALID_DNI}],
        },
        {"clientes": True},
        column_mappings={"nombre": "name", "documento": "dni"},
    )
    assert counts["clientes"] == 1
    assert counts["clientes_creados"] == 0
    assert counts["clientes_actualizados"] == 1
    assert counts["clientes_needs_review"] == 0
    assert counts["clientes_invalidos"] == 0

    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1
    assert customers[0].name == "Juan Perez"  # actualizado


@pytest.mark.asyncio
async def test_import_maestro_proveedores_desglosa_creado_needs_review_invalido(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "proveedores",
        "mapping_contexts": [
            {"context_id": "table", "entity_type": "supplier", "headers": ["nombre", "cuil"]}
        ],
        "proveedores_detectados": [
            {"nombre": "Distribuidora Sur", "cuil": "20-12345678-6"},  # create
            {"nombre": "Sin Cuil"},  # needs_review
            {"nombre": "Cuil Invalido", "cuil": "abc"},  # invalid
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"proveedores": True},
        column_mappings={"nombre": "name", "cuil": "cuil"},
    )
    assert counts["proveedores"] == 1
    assert counts["proveedores_creados"] == 1
    assert counts["proveedores_needs_review"] == 1
    assert counts["proveedores_invalidos"] == 1

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1
    assert suppliers[0].name == "Distribuidora Sur"


# ── 2. Backward-compat: summary viejo sin los buckets/counts nuevos ────────────


@pytest.mark.asyncio
async def test_summary_legacy_sin_buckets_de_maestro_no_rompe(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un summary de ANTES de F7a/F7c/F7d (sin clientes_detectados/mapping_contexts
    ni ningún contador nuevo) sigue importando ventas/gastos normalmente — la
    taxonomía nueva vive en un dict con `.get()`/defaults, nunca KeyError."""
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "3000"}],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )
    assert counts["ventas"] == 1
    # Sin ninguna columna customer_* mapeada, la venta es "anonymous" (venta de
    # mostrador, el caso normal) — la única clave nueva que legítimamente no es 0.
    assert counts["ventas_cliente_anonimo"] == 1
    # El resto de las claves nuevas están presentes con default 0 — nada las pisó
    # con un KeyError ni quedaron ausentes.
    for key in (
        "clientes",
        "proveedores",
        "clientes_creados",
        "clientes_actualizados",
        "clientes_needs_review",
        "clientes_invalidos",
        "proveedores_creados",
        "proveedores_actualizados",
        "proveedores_needs_review",
        "proveedores_invalidos",
        "ventas_cliente_identificado",
        "ventas_cliente_no_resuelto",
        "compras_proveedor_identificado",
        "compras_proveedor_anonimo",
        "compras_proveedor_no_resuelto",
    ):
        assert counts[key] == 0

    importer.check_nonempty_import(counts, summary, {"ventas": True}, None)


# ── 3. Reread de maestros ───────────────────────────────────────────────────────


def _patch_reread_fresh_summary(
    monkeypatch: pytest.MonkeyPatch, fresh: dict[str, Any]
) -> None:
    """Evita depender de S3 real + parsing real: `_fresh_summary` (reread_service)
    hace `s3.download(...)` y después `parse_uploaded_content(...)` — acá se
    fija directamente el summary "re-parseado" que la relectura va a usar."""

    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return b""

    monkeypatch.setattr(S3Client, "download", _fake_download)
    monkeypatch.setattr(reread_service, "parse_uploaded_content", lambda *a, **k: fresh)


async def _make_reread_file(
    session: AsyncSession, tenant: Tenant, master_column_mappings: dict[str, Any] | None
) -> UploadedFile:
    parsed: dict[str, Any] = {"confirmed_fields": {"clientes": True}}
    if master_column_mappings is not None:
        parsed["master_column_mappings"] = master_column_mappings
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="clientes.csv",
        s3_key=f"tenants/{tenant.tenant_id}/clientes.csv",
        content_type="text/csv",
        size_bytes=10,
        purpose="clientes",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json=parsed,
    )
    session.add(f)
    await session.commit()
    return f


@pytest.mark.asyncio
async def test_reread_maestro_actualiza_y_preserva_campo_no_provisto(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El reread actualiza el cliente existente (mismo DNI) con los campos que el
    archivo SÍ mapea (email), pero preserva el teléfono editado a mano que el
    archivo NO mapea — no lo pisa con vacío."""
    existing = Customer(
        tenant_id=sample_tenant.tenant_id,
        name="Juan Perez",
        dni=_VALID_DNI,
        phone="1122334455",  # edición manual posterior a la primera importación
    )
    db_session.add(existing)
    await db_session.flush()

    file = await _make_reread_file(
        db_session,
        sample_tenant,
        {"flat": {"nombre": "name", "documento": "dni", "correo": "email"}, "context": {}},
    )

    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "documento", "correo"],
            }
        ],
        "clientes_detectados": [
            {"nombre": "Juan Perez", "documento": _VALID_DNI, "correo": "nuevo@mail.com"}
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, sample_tenant.tenant_id)
    assert result.clientes == 1
    await db_session.commit()

    refreshed = (
        await db_session.execute(select(Customer).where(Customer.id == existing.id))
    ).scalar_one()
    assert refreshed.email == "nuevo@mail.com"  # actualizado (provisto)
    assert refreshed.phone == "1122334455"  # preservado (no mapeado por el archivo)


@pytest.mark.asyncio
async def test_reread_maestro_needs_review_no_mergea(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una fila sin ninguna clave fuerte (needs_review) NUNCA crea ni actualiza —
    el reread la saltea, igual que el confirm."""
    existing = Customer(tenant_id=sample_tenant.tenant_id, name="Juan Perez", dni=_VALID_DNI)
    db_session.add(existing)
    await db_session.flush()

    file = await _make_reread_file(
        db_session, sample_tenant, {"flat": {"nombre": "name"}, "context": {}}
    )
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {"context_id": "table", "entity_type": "customer", "headers": ["nombre"]}
        ],
        "clientes_detectados": [{"nombre": "Cliente Sin Documento"}],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, sample_tenant.tenant_id)
    assert result.clientes == 0
    await db_session.commit()

    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 1  # sigue habiendo solo el original, nada se creó


@pytest.mark.asyncio
async def test_reread_maestro_conflicto_de_clave_no_mergea(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una fila cuyas claves matchean a DOS clientes existentes distintos
    (conflict) NUNCA mergea — el reread la saltea igual que needs_review, no
    hay merge silencioso de dos identidades distintas."""
    valid_cuit = "20-12345678-6"
    existing_a = Customer(
        tenant_id=sample_tenant.tenant_id, name="Cliente Uno", cuit=valid_cuit
    )
    existing_b = Customer(
        tenant_id=sample_tenant.tenant_id, name="Cliente Dos", email="ambiguo@mail.com"
    )
    db_session.add_all([existing_a, existing_b])
    await db_session.flush()

    file = await _make_reread_file(
        db_session,
        sample_tenant,
        {"flat": {"nombre": "name", "cuit_col": "cuit", "correo": "email"}, "context": {}},
    )
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "cuit_col", "correo"],
            }
        ],
        "clientes_detectados": [
            {
                "nombre": "Cliente Confuso",
                "cuit_col": valid_cuit,  # matchea a existing_a por documento
                "correo": "ambiguo@mail.com",  # matchea a existing_b por email
            }
        ],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, sample_tenant.tenant_id)
    assert result.clientes == 0
    await db_session.commit()

    # Ningún merge silencioso: los dos clientes existentes siguen intactos y
    # separados, y no se creó un tercero.
    customers = (await db_session.execute(select(Customer))).scalars().all()
    assert len(customers) == 2
    refreshed_a = (
        await db_session.execute(select(Customer).where(Customer.id == existing_a.id))
    ).scalar_one()
    refreshed_b = (
        await db_session.execute(select(Customer).where(Customer.id == existing_b.id))
    ).scalar_one()
    assert refreshed_a.name == "Cliente Uno"
    assert refreshed_b.name == "Cliente Dos"


@pytest.mark.asyncio
async def test_reread_sin_mapeo_guardado_no_reaplica_maestros(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward-compat: un archivo confirmado ANTES de F7d (sin
    `master_column_mappings` en el summary) no reaplica maestros en la
    relectura — no rompe, simplemente no adivina el shape de la hoja."""
    file = await _make_reread_file(db_session, sample_tenant, None)
    fresh = {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "mapping_contexts": [
            {
                "context_id": "table",
                "entity_type": "customer",
                "headers": ["nombre", "documento"],
            }
        ],
        "clientes_detectados": [{"nombre": "Juan Perez", "documento": _VALID_DNI}],
    }
    _patch_reread_fresh_summary(monkeypatch, fresh)

    result = await reread_service.apply_reread(db_session, file.id, sample_tenant.tenant_id)
    assert result.clientes == 0
    await db_session.commit()

    assert (await db_session.execute(select(Customer))).first() is None


# ── 4. apply_import (F7d review) no pisa un campo existente con celda vacía ───


@pytest.mark.asyncio
async def test_customer_apply_import_no_pisa_email_existente_con_celda_vacia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review 7d (Important): una columna MAPEADA pero con la celda vacía en esta
    fila arma {campo: ""} (clave presente, valor vacío) — el guard de update
    anterior (``if fname not in record``) chequeaba presencia de clave, no
    vacuidad, así que ese valor vacío se ``setattr``-eaba igual y borraba el
    email existente. El re-import matchea por DNI con la columna email MAPEADA
    pero vacía → el email debe preservarse."""
    from app.application.services.customer_import_service import apply_import
    from app.persistence.repositories.customer_repository import CustomerRepository

    repo = CustomerRepository(db_session)
    tid = sample_tenant.tenant_id
    existing = Customer(
        tenant_id=tid, name="Juan Perez", dni=_VALID_DNI, email="juan@viejo.com"
    )
    db_session.add(existing)
    await db_session.commit()

    # La columna "email" está MAPEADA (la clave está presente) pero la celda de
    # esta fila vino vacía.
    records = [{"name": "Juan Perez", "dni": _VALID_DNI, "email": ""}]
    result = await apply_import(repo, tid, records)
    assert result.updated_ids == [existing.id]

    await db_session.refresh(existing)
    assert existing.email == "juan@viejo.com"  # preservado, no pisado con ""


# ── 5. compras_proveedor_identificado no cuenta compras que van a "Otros" ──────


@pytest.mark.asyncio
async def test_compra_matched_pero_producto_ambiguo_no_genera_conteo_fantasma(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 7d (Minor): el bump de "matched" ocurría ANTES del check de
    producto ambiguo — una compra con proveedor matcheado pero producto
    ambiguo (≥2 activos con el mismo nombre) va a "Otros" y el gasto NUNCA se
    persiste, pero el contador igual sumaba +1 (conteo fantasma). Ahora el
    bump se difiere a después de saber que la fila no fue descartada."""
    monkeypatch.setattr(get_settings(), "SUPPLIER_REFERENCE_CREATION_MODE", "link_only")

    valid_cuil = "20-12345678-6"
    supplier = Supplier(
        tenant_id=sample_tenant.tenant_id, name="Distribuidora Real", cuil=valid_cuil
    )
    # Dos productos activos con el MISMO nombre → lookup ambiguo.
    products = [
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Coca Cola",
            sale_price_ars=Decimal("1000"),
            unit_cost_ars=Decimal("600"),
            stock_units=5,
            is_active=True,
        ),
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Coca Cola",
            sale_price_ars=Decimal("1200"),
            unit_cost_ars=Decimal("700"),
            stock_units=8,
            is_active=True,
        ),
    ]
    db_session.add(supplier)
    db_session.add_all(products)
    await db_session.flush()

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "gasto": "5000",
                "producto": "Coca Cola",
                "cantidad": "10",
                "cuil_prov": valid_cuil,
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        column_mappings={"cuil_prov": "supplier_cuil"},
    )

    assert counts["otros"] == 1
    assert counts["gastos"] == 0  # el gasto NUNCA se persistió (fue a "Otros")
    assert counts["compras_proveedor_identificado"] == 0  # sin conteo fantasma

    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert expenses == []
