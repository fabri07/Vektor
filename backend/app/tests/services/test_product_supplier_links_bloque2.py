"""Bloque 2 — Tienda → proveedor: vínculos Producto↔Supplier declarados en catálogo.

Diagnóstico real (Asteria, 2026-08-30): 0 proveedores existen en el tenant, y un
producto real ("ganchos para cortina de baño") se repuso desde DOS tiendas
distintas. Antes de este bloque, "Tienda" entraba a la identidad del producto
como marca (`normalize_brand`), así que dos filas del mismo nombre con distinta
Tienda colapsaban en DOS productos separados — el bug de duplicación que este
bloque cierra: con "Tienda" confirmada como proveedor, deja de ser parte de la
identidad y el mismo producto queda con dos vínculos en `product_supplier_links`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.config.settings import get_settings
from app.persistence.models.field_definitions import TenantCustomFieldDefinition
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_supplier_link import ProductSupplierLink
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": ["nombre", "tienda", "precio"],
                "row_count": len(rows),
            },
        ],
        "stock_detectado": rows,
    }


_CONTEXT_MAPPINGS = {
    "sheet:Catalogo": {
        "nombre": "name",
        "tienda": "supplier:name",
        "precio": "sale_price_ars",
    },
}


def _row(nombre: str, tienda: str, precio: str = "100") -> dict[str, Any]:
    return {
        "nombre": nombre,
        "tienda": tienda,
        "precio": precio,
        "__context__": "sheet:Catalogo",
    }


def _enable(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID) -> None:
    monkeypatch.setattr(
        get_settings(),
        "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
        [str(tenant_id)],
    )


async def _active_links(session: AsyncSession, tenant_id: uuid.UUID) -> list[ProductSupplierLink]:
    rows = (
        await session.execute(
            select(ProductSupplierLink).where(
                ProductSupplierLink.tenant_id == tenant_id,
                ProductSupplierLink.voided_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def test_un_producto_con_dos_proveedores(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mismo producto, dos filas con Tienda distinta → 1 Product, 2 links.

    Antes de este bloque, "Tienda" entraba a la identidad como marca y las dos
    filas colapsaban en DOS productos separados (bug real de Asteria).
    """
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    summary = _summary(
        [
            _row("Ganchos para cortina de baño", "El pasillo"),
            _row("Ganchos para cortina de baño", "sublink"),
        ]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    products = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalars().all()
    assert len(products) == 1, "las dos filas deben colapsar al MISMO producto"

    links = await _active_links(db_session, tid)
    assert len(links) == 2
    assert {link.product_id for link in links} == {products[0].id}
    suppliers = (
        await db_session.execute(select(Supplier).where(Supplier.tenant_id == tid))
    ).scalars().all()
    assert {s.name for s in suppliers} == {"El pasillo", "sublink"}
    assert all(link.source == "catalog_declared" for link in links)


async def test_el_link_declarado_tambien_estampa_el_movimiento_de_inventario(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hallazgo ASTERIA 2026-09-13: ``product_supplier_links`` (Bloque 2, este
    módulo) y ``inventory_movements.supplier_id`` son tablas DISTINTAS —
    ``GET /suppliers/{id}/products`` lee únicamente de la segunda. Declarar el
    vínculo sin estampar el movimiento dejaba la sección de Proveedores vacía
    aunque el link ya existiera y la vinculación estuviera habilitada.
    """
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    context_mappings = {
        "sheet:Catalogo": {
            "nombre": "name",
            "tienda": "supplier:name",
            "precio": "sale_price_ars",
            "stock": "stock_units",
        },
    }
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": ["nombre", "tienda", "precio", "stock"],
                "row_count": 1,
            },
        ],
        "stock_detectado": [
            {
                "nombre": "Ganchos para cortina de baño",
                "tienda": "El pasillo",
                "precio": "100",
                "stock": "10",
                "__context__": "sheet:Catalogo",
            }
        ],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=context_mappings,
        context_confirmed={"sheet:Catalogo": True},
    )

    supplier = (
        await db_session.execute(select(Supplier).where(Supplier.tenant_id == tid))
    ).scalar_one()
    movements = (
        await db_session.execute(
            select(InventoryMovement).where(InventoryMovement.tenant_id == tid)
        )
    ).scalars().all()
    assert len(movements) == 1
    assert movements[0].supplier_id == supplier.id


async def test_dos_filas_repetidas_no_duplican_el_vinculo(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    summary = _summary(
        [
            _row("Producto A", "El pasillo"),
            _row("Producto A", "El pasillo"),
        ]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    links = await _active_links(db_session, tid)
    assert len(links) == 1


async def test_relectura_identica_es_idempotente(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)
    upload_id = uuid.uuid4()

    summary = _summary([_row("Producto B", "Bazar mayorista")])
    for _ in range(2):
        await insert_confirmed_data(
            db_session,
            tid,
            summary,
            {"productos": True},
            context_mappings=_CONTEXT_MAPPINGS,
            context_confirmed={"sheet:Catalogo": True},
            source="reread",
            uploaded_file_id=upload_id,
        )
        await db_session.commit()

    links = await _active_links(db_session, tid)
    assert len(links) == 1
    products = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalars().all()
    assert len(products) == 1


async def test_proveedor_confirmado_no_se_guarda_como_marca(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    summary = _summary([_row("Producto C", "Easy")])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    product = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalar_one()
    assert product.custom_fields.get("marca") is None
    assert product.custom_fields.get("tienda_original") == "Easy"


async def test_exclusion_posterior_revierte_solo_el_vinculo_atribuible(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una relectura que deja de mapear Tienda→proveedor anula el link que
    ELLA había declarado — sin tocar vínculos de otro archivo ni evidencia real.
    """
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)
    upload_id = uuid.uuid4()

    summary = _summary([_row("Producto D", "Vanika")])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
        source="reread",
        uploaded_file_id=upload_id,
    )
    assert len(await _active_links(db_session, tid)) == 1

    # Relectura del MISMO archivo, pero ahora sin mapear Tienda como proveedor
    # (el usuario reasignó la columna a un custom field, por ejemplo).
    summary_sin_supplier = _summary([_row("Producto D", "Vanika")])
    await insert_confirmed_data(
        db_session,
        tid,
        summary_sin_supplier,
        {"productos": True},
        context_mappings={
            "sheet:Catalogo": {
                "nombre": "name",
                "tienda": "custom_field:tienda",
                "precio": "sale_price_ars",
            },
        },
        context_confirmed={"sheet:Catalogo": True},
        source="reread",
        uploaded_file_id=upload_id,
    )

    assert len(await _active_links(db_session, tid)) == 0
    voided = (
        await db_session.execute(
            select(ProductSupplierLink).where(ProductSupplierLink.tenant_id == tid)
        )
    ).scalars().all()
    assert len(voided) == 1
    assert voided[0].voided_at is not None


async def test_purchase_evidence_no_se_elimina_al_retirar_declaracion_de_catalogo(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un vínculo ya upgradeado a purchase_evidence sobrevive aunque una
    relectura posterior del MISMO archivo deje de declararlo por catálogo.
    """
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)
    upload_id = uuid.uuid4()

    from app.application.services.ingestion_import_service import (
        _load_supplier_index,
        _resolve_or_create_supplier,
    )
    from app.application.services.product_supplier_link_service import (
        link_product_to_declared_supplier,
    )

    summary = _summary([_row("Producto E", "Bazar once")])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
        source="reread",
        uploaded_file_id=upload_id,
    )
    product = (
        await db_session.execute(select(Product).where(Product.tenant_id == tid))
    ).scalar_one()
    supplier_index = await _load_supplier_index(db_session, tid)
    supplier_id, _ = await _resolve_or_create_supplier(
        db_session, tid, "Bazar once", supplier_index
    )
    assert supplier_id is not None
    await link_product_to_declared_supplier(
        db_session,
        tid,
        product.id,
        supplier_id,
        source="purchase_evidence",
        source_upload_id=upload_id,
        source_context_id="sheet:Catalogo",
    )
    await db_session.commit()

    # Relectura sin mapear Tienda como proveedor.
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([_row("Producto E", "Bazar once")]),
        {"productos": True},
        context_mappings={
            "sheet:Catalogo": {
                "nombre": "name",
                "tienda": "custom_field:tienda",
                "precio": "sale_price_ars",
            },
        },
        context_confirmed={"sheet:Catalogo": True},
        source="reread",
        uploaded_file_id=upload_id,
    )

    links = await _active_links(db_session, tid)
    assert len(links) == 1
    assert links[0].source == "purchase_evidence"


async def test_aislamiento_entre_tenants(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.verticals import Vertical
    from app.persistence.models.business import BusinessProfile
    from app.persistence.models.tenant import Tenant as TenantModel

    other = TenantModel(
        tenant_id=uuid.uuid4(),
        legal_name="Otro tenant SRL",
        display_name="Otro tenant",
        status="ACTIVE",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=other.tenant_id,
            vertical_code=Vertical.KIOSCO_ALMACEN.value,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
        )
    )
    await db_session.flush()

    monkeypatch.setattr(
        get_settings(),
        "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
        [str(sample_tenant.tenant_id), str(other.tenant_id)],
    )

    await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary([_row("Producto F", "Merie")]),
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )
    await insert_confirmed_data(
        db_session,
        other.tenant_id,
        _summary([_row("Producto F", "Merie")]),
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    links_a = await _active_links(db_session, sample_tenant.tenant_id)
    links_b = await _active_links(db_session, other.tenant_id)
    assert len(links_a) == 1
    assert len(links_b) == 1
    assert links_a[0].tenant_id != links_b[0].tenant_id
    suppliers_a = (
        await db_session.execute(
            select(Supplier).where(Supplier.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    suppliers_b = (
        await db_session.execute(
            select(Supplier).where(Supplier.tenant_id == other.tenant_id)
        )
    ).scalars().all()
    assert {s.id for s in suppliers_a}.isdisjoint({s.id for s in suppliers_b})


async def test_flag_encendido_no_rechaza_y_crea_el_vinculo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """`product_supplier_links_enabled_for` está graduada (siempre True,
    2026-09-13) — el mismo mapeo NO levanta `SupplierLinkNotEnabledError`,
    para cualquier tenant, sin necesitar habilitar nada."""
    tid = sample_tenant.tenant_id

    summary = _summary([_row("Producto H", "El pasillo")])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings=_CONTEXT_MAPPINGS,
        context_confirmed={"sheet:Catalogo": True},
    )

    links = await _active_links(db_session, tid)
    assert len(links) == 1
    assert len(await _active_links(db_session, tid)) == 1

    definicion = (
        await db_session.execute(
            select(TenantCustomFieldDefinition).where(
                TenantCustomFieldDefinition.tenant_id == tid,
                TenantCustomFieldDefinition.entity_type == "product",
                TenantCustomFieldDefinition.field_key == "tienda_original",
            )
        )
    ).scalar_one_or_none()
    assert definicion is not None


async def test_reread_apply_persiste_comprobante_en_el_run(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cambio 4: una relectura exitosa deja el comprobante en
    `run.details_json["receipt"]`, con el mismo contrato que el confirm —
    incluida una columna que NUNCA se mapeó (visible desde mapping_contexts,
    no desde el mapeo elegido)."""
    from app.application.services import reread_service
    from app.application.services.reread_service import REPAIR_TYPE_REREAD
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
    from app.persistence.models.repair import DataRepairRun

    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": ["nombre", "tienda", "precio", "notas_internas"],
                "row_count": 1,
            },
        ],
        "stock_detectado": [_row("Producto W", "El pasillo")],
    }
    file = UploadedFile(
        tenant_id=tid,
        uploaded_by=None,
        original_filename="catalogo.xlsx",
        s3_key=f"tenants/{tid}/catalogo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=512,
        purpose="productos",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={"confirmed_fields": {"productos": True}},
    )
    db_session.add(file)
    await db_session.commit()

    run = DataRepairRun(
        tenant_id=tid,
        repair_type=REPAIR_TYPE_REREAD,
        status="APPLYING",
        dry_run=False,
        details_json={
            "file_id": str(file.id),
            "draft": {"column_mappings": [dict(m, context_id="sheet:Catalogo")
                                           for m in [
                {"source_column": "nombre", "target_field": "name"},
                {"source_column": "tienda", "target_field": "supplier:name"},
                {"source_column": "precio", "target_field": "sale_price_ars"},
            ]]},
        },
    )
    db_session.add(run)
    await db_session.commit()

    await reread_service.apply_reread(db_session, file.id, tid, run=run, fresh_override=summary)
    await db_session.commit()

    assert run.details_json is not None
    receipt = run.details_json["receipt"]
    assert receipt["version"] == 1
    columns = {c["source_column"]: c for c in receipt["columns"]}
    assert columns["nombre"]["result"] == "guardado"
    assert columns["precio"]["result"] == "transformado"
    assert columns["notas_internas"]["result"] == "excluido"
    assert columns["notas_internas"]["reason_kind"] == "system_rule"
