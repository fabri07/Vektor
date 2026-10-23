"""B·8a — correcciones de la revisión del PR #162.

1. El candado E6b vive en el helper de la venta desde «Otros»: `link-product` y
   `reclassify` dan 409 `IMPORT_IDENTITY_TAKEN` y no crean venta ni alias.
2. Un homónimo INACTIVO del alias no lo inutiliza: el import vincula al dueño.
3. Un alias cuyo producto se dio de baja se REASIGNA al producto elegido.
4. La venta desde «Otros» descuenta stock por el camino de POST /sales (y sin
   stock suficiente no se crea nada); borrar el archivo lo devuelve.
5. Cada camino real que consulta el alias, de punta a punta: venta plana y
   multihoja, «Importar sugeridos», preview de la relectura y el vínculo fuerte
   de un gasto plano. La referencia no comparte palabras con el producto, así que
   sacar el alias de cualquiera de esos caminos hace fallar su test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services import reread_service
from app.application.services.file_deletion_service import revert_file_data
from app.application.services.ingestion_import_service import IDENTITY_REF_KEY
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.text_norm import normalize_product_name
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.operation_identity import OperationIdentity
from app.persistence.models.product import Product
from app.persistence.models.product_alias import ProductAlias
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio

_REF = "Negrita chica"
_FECHA = "2024-03-15"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _archivo(db: AsyncSession, tenant: Tenant) -> UploadedFile:
    f = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
        content_type="text/csv",
        size_bytes=10,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    db.add(f)
    await db.flush()
    return f


async def _fila(
    db: AsyncSession,
    tenant: Tenant,
    *,
    archivo: UploadedFile | None = None,
    cantidad: str = "2",
    extra: dict[str, Any] | None = None,
) -> UnclassifiedRecord:
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Venta sin producto",
        row_data={
            "fecha": _FECHA, "monto": "3000", "cantidad": cantidad,
            "medio_de_pago": "cash", "producto": _REF, **(extra or {}),
        },
        suggested_entity="sale",
        status="PENDING",
        uploaded_file_id=archivo.id if archivo else None,
    )
    db.add(rec)
    await db.commit()
    return rec


async def _alias(db: AsyncSession, tenant: Tenant, pid: uuid.UUID, texto: str = _REF) -> None:
    db.add(
        ProductAlias(
            tenant_id=tenant.tenant_id, product_id=pid, alias=texto,
            alias_normalizado=normalize_product_name(texto),
        )
    )
    await db.commit()


async def _contar(db: AsyncSession, modelo: Any, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(modelo).where(modelo.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
    )


async def _link(
    client: AsyncClient, h: dict[str, Any], rec: UnclassifiedRecord, pid: uuid.UUID
) -> Any:
    return await client.post(
        f"/api/v1/others/{rec.id}/link-product", json={"product_id": str(pid)}, headers=h
    )


async def _reclassify(
    client: AsyncClient, h: dict[str, Any], rec: UnclassifiedRecord, pid: uuid.UUID, qty: int = 2
) -> Any:
    return await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={
            "entity_type": "sale",
            "fields": {
                "amount": "3000", "quantity": qty, "transaction_date": f"{_FECHA}T00:00:00",
                "payment_method": "cash", "product_id": str(pid),
            },
        },
        headers=h,
    )


# ── 1. Candado E6b ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("camino", ["link", "reclassify"])
async def test_identidad_tomada_es_409_en_los_dos_caminos(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    camino: str,
) -> None:
    coca = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca Cola 500ml")
    db_session.add(
        OperationIdentity(
            tenant_id=sample_tenant.tenant_id, identity_key="k-1", content_hash="h",
            entity_type="sale",
        )
    )
    rec = await _fila(db_session, sample_tenant, extra={IDENTITY_REF_KEY: "k-1"})

    r = await (_link if camino == "link" else _reclassify)(client, auth_headers, rec, coca.id)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IMPORT_IDENTITY_TAKEN"
    db_session.expunge_all()
    assert await _contar(db_session, SaleEntry, sample_tenant) == 0
    assert await _contar(db_session, ProductAlias, sample_tenant) == 0
    fila = await db_session.get(UnclassifiedRecord, rec.id)
    assert fila is not None and fila.status == "PENDING"


# ── 2. Homónimo inactivo ──────────────────────────────────────────────────────


async def test_un_homonimo_inactivo_no_inutiliza_el_alias(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    a = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
    b = await producto_con_compra(db_session, tid, nombre=_REF)
    b.is_active = False
    await db_session.commit()
    await _alias(db_session, sample_tenant, a.id)

    counts = await importer.insert_confirmed_data(
        db_session,
        tid,
        {
            "file_type": "spreadsheet", "inferred_type": "ventas", "has_venta": True,
            "ventas_detectadas": [{"fecha": _FECHA, "monto": "100", "producto": _REF}],
        },
        {"ventas": True},
        column_mappings={"producto": "product_name"},
    )
    await db_session.flush()

    assert counts["ventas"] == 1
    venta = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert venta.product_id == a.id


# ── 3. Alias de un producto dado de baja ──────────────────────────────────────


async def test_el_alias_de_un_producto_dado_de_baja_se_reasigna(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    viejo = await producto_con_compra(db_session, tid, nombre="Coca vieja")
    nuevo = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
    await _alias(db_session, sample_tenant, viejo.id)
    viejo.is_active = False
    await db_session.commit()
    rec = await _fila(db_session, sample_tenant)

    r = await _link(client, auth_headers, rec, nuevo.id)

    assert r.status_code == 200, r.text
    db_session.expunge_all()
    alias = (await db_session.execute(select(ProductAlias))).scalar_one()
    assert alias.product_id == nuevo.id
    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == "OTHERS_SALE_LINKED_TO_PRODUCT"
            )
        )
    ).scalar_one()
    assert audit.decision_data["alias_reasignado_de"] == str(viejo.id)


# ── 4. Stock ──────────────────────────────────────────────────────────────────


async def _queso(db: AsyncSession, tenant: Tenant, stock: int = 10000) -> Product:
    """Un producto por peso: se vende en kg, el stock está en gramos."""
    p = await producto_con_compra(db, tenant.tenant_id, nombre="Queso cremoso", stock=stock)
    p.base_units_per_sale_unit = 1000
    await db.commit()
    return p


@pytest.mark.parametrize("camino", ["link", "reclassify"])
async def test_la_venta_desde_otros_descuenta_stock(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    camino: str,
) -> None:
    queso = await _queso(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant)  # 2 kg

    r = await (_link if camino == "link" else _reclassify)(client, auth_headers, rec, queso.id)

    assert r.status_code == 200, r.text
    db_session.expunge_all()
    actual = await db_session.get(Product, queso.id)
    # 10.000 g − 2 kg = 8.000 g.
    assert actual is not None and actual.stock_units == 8000


@pytest.mark.parametrize("camino", ["link", "reclassify"])
async def test_sin_stock_suficiente_no_se_crea_nada(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    camino: str,
) -> None:
    queso = await _queso(db_session, sample_tenant, stock=1500)
    rec = await _fila(db_session, sample_tenant)  # 2 kg > 1,5 kg

    r = await (_link if camino == "link" else _reclassify)(client, auth_headers, rec, queso.id)

    assert r.status_code in (400, 409, 422), r.text
    db_session.expunge_all()
    assert await _contar(db_session, SaleEntry, sample_tenant) == 0
    assert await _contar(db_session, ProductAlias, sample_tenant) == 0
    actual = await db_session.get(Product, queso.id)
    assert actual is not None and actual.stock_units == 1500
    fila = await db_session.get(UnclassifiedRecord, rec.id)
    assert fila is not None and fila.status == "PENDING"


async def test_borrar_el_archivo_devuelve_la_venta_y_el_stock(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    queso = await _queso(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, archivo=archivo)
    assert (await _link(client, auth_headers, rec, queso.id)).status_code == 200

    await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
    await db_session.flush()

    db_session.expunge_all()
    actual = await db_session.get(Product, queso.id)
    assert actual is not None and actual.stock_units == 10000
    assert (await db_session.execute(select(SaleEntry))).scalar_one().voided_at is not None


# ── 5. Cada camino real que consulta el alias ─────────────────────────────────


async def _coca_con_alias(db: AsyncSession, tenant: Tenant) -> Product:
    coca = await producto_con_compra(db, tenant.tenant_id, nombre="Coca Cola 500ml")
    await _alias(db, tenant, coca.id)
    return coca


async def _producto_de_las_ventas(db: AsyncSession, tenant: Tenant) -> list[uuid.UUID | None]:
    return list(
        (
            await db.execute(
                select(SaleEntry.product_id).where(SaleEntry.tenant_id == tenant.tenant_id)
            )
        ).scalars()
    )


async def test_venta_plana(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    coca = await _coca_con_alias(db_session, sample_tenant)

    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet", "inferred_type": "ventas", "has_venta": True,
            "ventas_detectadas": [{"fecha": _FECHA, "monto": "100", "producto": "NEGRITA CHICA"}],
        },
        {"ventas": True},
        column_mappings={"producto": "product_name"},
    )
    await db_session.flush()

    assert await _producto_de_las_ventas(db_session, sample_tenant) == [coca.id]


async def test_venta_multihoja(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    coca = await _coca_con_alias(db_session, sample_tenant)
    ctx = "sheet:Ventas"

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet", "inferred_type": "mixed", "multi_sheet": True,
            "mapping_contexts": [
                {"context_id": ctx, "label": "Ventas", "entity_type": "sale",
                 "headers": ["Fecha", "Monto", "Producto"], "row_count": 1}
            ],
            "ventas_detectadas": [
                {"Fecha": _FECHA, "Monto": "100", "Producto": "negrita chica", "__context__": ctx}
            ],
        },
        {"ventas": True},
        context_mappings={
            ctx: {"Fecha": "transaction_date", "Monto": "amount", "Producto": "product_name"}
        },
        context_confirmed={ctx: True},
    )
    await db_session.flush()

    assert counts["ventas"] == 1
    assert await _producto_de_las_ventas(db_session, sample_tenant) == [coca.id]


async def test_importar_sugeridos(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    coca = await _coca_con_alias(db_session, sample_tenant)
    await _fila(db_session, sample_tenant)

    resultado = await importer.bulk_import_unclassified(db_session, sample_tenant.tenant_id, "sale")
    await db_session.flush()

    assert resultado["imported_sales"] == 1
    assert await _producto_de_las_ventas(db_session, sample_tenant) == [coca.id]


async def test_preview_de_la_relectura(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    await _coca_con_alias(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)
    await db_session.commit()
    fresh = {
        "file_type": "spreadsheet", "inferred_type": "mixed", "multi_sheet": True,
        "ventas_detectadas": [{"producto": "NEGRITA CHICA", "monto": "500", "fecha": _FECHA}],
    }

    preview = await reread_service.preview_reread(
        db_session, archivo.id, sample_tenant.tenant_id, fresh_override=fresh
    )

    assert preview.unlinked_products is not None
    assert preview.unlinked_products.ventas_con_producto == 1
    assert preview.unlinked_products.ventas_sin_producto == 0


async def test_vinculo_fuerte_de_un_gasto_plano(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un gasto plano SIN cantidad, de una categoría que no es mercadería, que
    nombra un producto por su alias: es un vínculo fuerte (no un parecido de
    palabras), así que la fila queda como compra sin cantidad → «Otros» (B·5b-2).
    Sin el alias en `_vinculo_fuerte` se soltaría el producto y entraría como un
    gasto operativo."""
    await _coca_con_alias(db_session, sample_tenant)

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet", "inferred_type": "gastos", "has_gasto": True,
            "gastos_detectados": [
                {"fecha": _FECHA, "gasto": "500", "producto": "Negrita chica",
                 "categoria": "Varios"}
            ],
        },
        {"gastos": True},
        column_mappings={"producto": "product_name", "categoria": "category"},
    )
    await db_session.flush()

    gastos = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert not [g for g in gastos if g.product_id is None and g.voided_at is None], counts
