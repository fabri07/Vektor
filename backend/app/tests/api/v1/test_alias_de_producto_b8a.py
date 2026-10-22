"""B·8a — alias de producto, de punta a punta.

`POST /others/{id}/link-product` resuelve una venta de «Otros» eligiendo el
producto, y lo que decía el archivo queda como alias: el próximo import lo
vincula solo. Las protecciones:

- la fila se toma con FOR UPDATE y tiene que estar PENDING;
- el alias se valida ANTES de crear la venta: si ya es de otro producto, o si es
  el nombre de otro producto activo → 409, y no queda venta, alias ni fila
  importada;
- la venta usa el MISMO helper que `reclassify` (exige compra previa, B·5e);
- el alias se consulta después del código de barras y del SKU, sólo para
  productos activos, y si el nombre exacto da OTRO producto no elige;
- el escaneo del POS y `/products/lookup` nunca lo usan;
- borrar el archivo de origen no borra el alias.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.file_deletion_service import revert_file_data
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.text_norm import normalize_product_name
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.product_alias import ProductAlias
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio

_REF = "Coca 500"


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
    producto: str | None = _REF,
    archivo: UploadedFile | None = None,
    sugerida: str = "sale",
    cantidad: str = "2",
) -> UnclassifiedRecord:
    row: dict[str, Any] = {
        "fecha": "2024-03-15",
        "monto": "3000",
        "cantidad": cantidad,
        "medio_de_pago": "cash",
    }
    if producto is not None:
        row["producto"] = producto
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Venta sin producto",
        row_data=row,
        suggested_entity=sugerida,
        status="PENDING",
        uploaded_file_id=archivo.id if archivo else None,
    )
    db.add(rec)
    await db.commit()
    return rec


async def _coca(db: AsyncSession, tenant: Tenant) -> Product:
    return await producto_con_compra(db, tenant.tenant_id, nombre="Coca Cola 500ml")


async def _vincular(
    client: AsyncClient, h: dict[str, Any], rec: UnclassifiedRecord, pid: uuid.UUID, **extra: Any
) -> Any:
    return await client.post(
        f"/api/v1/others/{rec.id}/link-product",
        json={"product_id": str(pid), **extra},
        headers=h,
    )


async def _contar(db: AsyncSession, modelo: Any, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(modelo).where(modelo.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
    )


async def _sin_efectos(db: AsyncSession, tenant: Tenant, rec: UnclassifiedRecord) -> None:
    db.expunge_all()
    assert await _contar(db, SaleEntry, tenant) == 0
    assert await _contar(db, ProductAlias, tenant) == 0
    fila = await db.get(UnclassifiedRecord, rec.id)
    assert fila is not None and fila.status == "PENDING"


class TestVincular:
    async def test_crea_la_venta_el_alias_y_el_rastro(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        archivo = await _archivo(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant, archivo=archivo)

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 200, r.text
        db_session.expunge_all()
        venta = (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == sample_tenant.tenant_id)
            )
        ).scalar_one()
        assert venta.product_id == coca.id
        assert venta.quantity == 2
        assert venta.amount == Decimal("3000")
        assert venta.source_upload_id == archivo.id
        assert venta.source_row_ref == f"unclassified:{rec.id}"
        alias = (await db_session.execute(select(ProductAlias))).scalar_one()
        assert (alias.product_id, alias.alias) == (coca.id, _REF)
        assert alias.alias_normalizado == normalize_product_name(_REF)
        fila = await db_session.get(UnclassifiedRecord, rec.id)
        assert fila is not None and fila.status == "IMPORTED"
        audit = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "OTHERS_SALE_LINKED_TO_PRODUCT"
                )
            )
        ).scalar_one()
        assert audit.decision_data["alias_creado"] is True

    async def test_el_proximo_import_se_vincula_solo_en_los_dos_caminos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        rec = await _fila(db_session, sample_tenant)
        assert (await _vincular(client, auth_headers, rec, coca.id)).status_code == 200

        plano = await importer.insert_confirmed_data(
            db_session,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "ventas",
                "has_venta": True,
                "ventas_detectadas": [
                    {"fecha": "2024-03-16", "monto": "1500", "producto": "COCA 500"}
                ],
            },
            {"ventas": True},
            column_mappings={"producto": "product_name"},
        )
        ctx = "sheet:Ventas"
        multi = await importer.insert_confirmed_data(
            db_session,
            tid,
            {
                "file_type": "spreadsheet",
                "inferred_type": "mixed",
                "multi_sheet": True,
                "mapping_contexts": [
                    {"context_id": ctx, "label": "Ventas", "entity_type": "sale",
                     "headers": ["Fecha", "Monto", "Producto"], "row_count": 1}
                ],
                "ventas_detectadas": [
                    {"Fecha": "2024-03-17", "Monto": "1500", "Producto": "coca 500",
                     "__context__": ctx}
                ],
            },
            {"ventas": True},
            context_mappings={
                ctx: {"Fecha": "transaction_date", "Monto": "amount", "Producto": "product_name"}
            },
            context_confirmed={ctx: True},
        )
        await db_session.flush()

        assert plano["ventas"] == 1 and multi["ventas"] == 1
        ventas = (
            await db_session.execute(
                select(SaleEntry.product_id).where(SaleEntry.tenant_id == tid)
            )
        ).scalars().all()
        assert sorted(map(str, ventas)) == [str(coca.id)] * 3

    async def test_alias_que_ya_es_del_mismo_producto_no_se_duplica(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        for _ in range(2):
            rec = await _fila(db_session, sample_tenant)
            assert (await _vincular(client, auth_headers, rec, coca.id)).status_code == 200

        assert await _contar(db_session, ProductAlias, sample_tenant) == 1
        assert await _contar(db_session, SaleEntry, sample_tenant) == 2

    async def test_el_propio_nombre_no_se_guarda_como_alias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca 500")
        rec = await _fila(db_session, sample_tenant)

        assert (await _vincular(client, auth_headers, rec, coca.id)).status_code == 200
        assert await _contar(db_session, ProductAlias, sample_tenant) == 0


class TestRechazos:
    async def test_alias_de_otro_producto_es_409_sin_efectos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        pepsi = await producto_con_compra(db_session, tid, nombre="Pepsi 500ml")
        db_session.add(
            ProductAlias(
                tenant_id=tid, product_id=pepsi.id, alias=_REF,
                alias_normalizado=normalize_product_name(_REF),
            )
        )
        rec = await _fila(db_session, sample_tenant)

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "ALIAS_OTRO_PRODUCTO"
        db_session.expunge_all()
        assert await _contar(db_session, SaleEntry, sample_tenant) == 0
        alias = (await db_session.execute(select(ProductAlias))).scalar_one()
        assert alias.product_id == pepsi.id
        fila = await db_session.get(UnclassifiedRecord, rec.id)
        assert fila is not None and fila.status == "PENDING"

    async def test_alias_que_es_el_nombre_de_otro_producto_es_409_sin_efectos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        await producto_con_compra(db_session, tid, nombre="Coca 500")  # otro producto
        rec = await _fila(db_session, sample_tenant)

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "ALIAS_NOMBRE_DE_OTRO_PRODUCTO"
        await _sin_efectos(db_session, sample_tenant, rec)

    async def test_sin_compra_previa_es_422_sin_efectos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await producto_con_compra(
            db_session, sample_tenant.tenant_id, nombre="Coca Cola 500ml", dia=date(2025, 1, 1)
        )
        rec = await _fila(db_session, sample_tenant)  # venta de 2024: antes de la compra

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 422, r.text
        await _sin_efectos(db_session, sample_tenant, rec)

    async def test_dos_veces_la_misma_fila_es_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant)
        assert (await _vincular(client, auth_headers, rec, coca.id)).status_code == 200

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "RECORD_ALREADY_RESOLVED"
        assert await _contar(db_session, SaleEntry, sample_tenant) == 1

    async def test_producto_inactivo_es_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        coca.is_active = False
        await db_session.commit()
        rec = await _fila(db_session, sample_tenant)

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

    async def test_producto_de_otro_negocio_es_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        second_tenant: Tenant,
    ) -> None:
        ajeno = await producto_con_compra(db_session, second_tenant.tenant_id, nombre="Coca Cola")
        rec = await _fila(db_session, sample_tenant)

        r = await _vincular(client, auth_headers, rec, ajeno.id)

        assert r.status_code == 422

    async def test_fila_de_otro_negocio_es_404(
        self,
        client: AsyncClient,
        second_auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant)

        r = await _vincular(client, second_auth_headers, rec, coca.id)

        assert r.status_code == 404

    async def test_viewer_no_puede(
        self,
        client: AsyncClient,
        viewer_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant)

        assert (await _vincular(client, viewer_headers, rec, coca.id)).status_code == 403

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"producto": None}, "SIN_REFERENCIA"),
            ({"sugerida": "expense"}, "NOT_A_SALE"),
            ({"cantidad": "2,5"}, "CANTIDAD_REQUERIDA"),
        ],
    )
    async def test_fila_que_no_se_puede_vincular_es_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        kwargs: dict[str, Any],
        code: str,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant, **kwargs)

        r = await _vincular(client, auth_headers, rec, coca.id)

        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == code
        await _sin_efectos(db_session, sample_tenant, rec)

    async def test_con_cantidad_explicita_una_cantidad_decimal_no_traba(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant, cantidad="2,5")

        r = await _vincular(client, auth_headers, rec, coca.id, quantity=3)

        assert r.status_code == 200, r.text


def _indices(**kw: Any) -> importer.ProductIdentityIndexes:
    base: dict[str, Any] = {
        "by_sku": {}, "by_barcode": {}, "by_name_brand": {}, "by_name": {}, "by_id": {},
        "by_alias": {},
    }
    base.update(kw)
    return importer.ProductIdentityIndexes(**base)


class TestResolucion:
    def test_resolve_product_alias_despues_de_codigos(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        alias = {normalize_product_name(_REF): [a]}

        assert importer._resolve_product({}, {}, _REF, None, by_alias=alias) == a
        # El SKU gana.
        assert importer._resolve_product({"x1": b}, {}, _REF, "X1", by_alias=alias) == b
        # El nombre exacto del catálogo es de OTRO producto: no se elige.
        por_nombre: dict[str, uuid.UUID | None] = {importer._normalize_name(_REF): b}
        assert importer._resolve_product({}, por_nombre, _REF, None, by_alias=alias) is None

    def test_resolve_identity_alias_y_conflicto_con_el_nombre(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        clave = normalize_product_name(_REF)
        info = {a: {"name": "A", "sku": None, "barcode": None},
                b: {"name": "B", "sku": None, "barcode": None}}

        solo_alias = importer._resolve_product_identity(
            _REF, None, None, indexes=_indices(by_alias={clave: [a]}, by_id=info)
        )
        assert (solo_alias.status, solo_alias.product_id) == ("resolved", a)

        choque = importer._resolve_product_identity(
            _REF, None, None,
            indexes=_indices(by_alias={clave: [a]}, by_name={clave: [b]}, by_id=info),
        )
        assert choque.status == "conflict"

    async def test_el_alias_de_un_producto_inactivo_no_se_carga(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        db_session.add(
            ProductAlias(
                tenant_id=tid, product_id=coca.id, alias=_REF,
                alias_normalizado=normalize_product_name(_REF),
            )
        )
        await db_session.flush()
        assert (await importer._load_alias_index(db_session, tid)) == {
            normalize_product_name(_REF): [coca.id]
        }
        coca.is_active = False
        await db_session.flush()

        assert (await importer._load_alias_index(db_session, tid)) == {}
        indices = await importer._load_product_identity_indexes(db_session, tid)
        assert indices.by_alias == {}


class TestFueraDelAlcance:
    async def test_el_escaneo_y_lookup_no_usan_alias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        db_session.add(
            ProductAlias(
                tenant_id=tid, product_id=coca.id, alias="7790001",
                alias_normalizado=normalize_product_name("7790001"),
            )
        )
        await db_session.commit()

        r = await client.get("/api/v1/products/lookup?code=7790001", headers=auth_headers)

        assert r.status_code in (404, 422), r.text

    async def test_borrar_el_archivo_no_borra_el_alias(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        coca = await _coca(db_session, sample_tenant)
        archivo = await _archivo(db_session, sample_tenant)
        rec = await _fila(db_session, sample_tenant, archivo=archivo)
        assert (await _vincular(client, auth_headers, rec, coca.id)).status_code == 200

        await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
        await db_session.flush()

        db_session.expunge_all()
        venta = (await db_session.execute(select(SaleEntry))).scalar_one()
        assert venta.voided_at is not None  # la venta vuelve con el archivo
        assert await _contar(db_session, ProductAlias, sample_tenant) == 1
        assert (await db_session.get(Product, coca.id)) is not None


class TestRelectura:
    async def test_el_preview_de_la_relectura_cuenta_el_alias_como_vinculado(
        self, db_session: AsyncSession, sample_tenant: Tenant
    ) -> None:
        """Sin los alias, el preview proyectaría como «sin producto» una venta que el
        reimport real (`insert_confirmed_data`) sí vincula."""
        from app.application.services import reread_service

        tid = sample_tenant.tenant_id
        coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
        db_session.add(
            ProductAlias(
                tenant_id=tid, product_id=coca.id, alias="Negrita chica",
                alias_normalizado=normalize_product_name("Negrita chica"),
            )
        )
        await db_session.flush()
        fresh = {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "ventas_detectadas": [{"producto": "NEGRITA CHICA", "monto": "500"}],
        }
        catalog = await reread_service._load_product_index(db_session, tid)

        sin_alias = await reread_service.estimate_unlinked_products(
            db_session, tid, fresh, {"ventas": True, "gastos": False}, catalog
        )
        con_alias = await reread_service.estimate_unlinked_products(
            db_session, tid, fresh, {"ventas": True, "gastos": False}, catalog,
            await importer._load_alias_index(db_session, tid),
        )

        assert sin_alias.ventas_sin_producto == 1
        assert (con_alias.ventas_con_producto, con_alias.ventas_sin_producto) == (1, 0)
