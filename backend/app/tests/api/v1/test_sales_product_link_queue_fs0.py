"""F-S.0 mecanismo 4: cola de ventas sin producto vinculado, agrupada por
nombre crudo. Cubre los 5 bloqueantes y los puntos importantes de la revisión:
orden de rutas, has_user_edits, auditoría agrupada, recálculo .delay(),
guardas de mantenimiento/PIN, paginación real y candidatos livianos.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.pin_service import PinService
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


def _window_key(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return PinService._window_key(tenant_id, user_id)


async def _unlinked_sale(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    raw_name: str,
    *,
    amount: str = "1500",
    voided: bool = False,
    with_source_upload: bool = True,
    other_custom_field: bool = False,
) -> SaleEntry:
    cf: dict[str, Any] = {"_unlinked_product_name_raw": raw_name}
    if other_custom_field:
        cf["_customer_resolution"] = "anonymous"
    entry = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(amount),
        quantity=1,
        transaction_date=datetime.now(UTC),
        payment_method="cash",
        notes="test",
        provenance="REAL",
        source_upload_id=uuid.uuid4() if with_source_upload else None,
        custom_fields=cf,
        voided_at=datetime.now(UTC) if voided else None,
        void_reason="MANUAL_ADMIN_VOID" if voided else None,
    )
    db_session.add(entry)
    await db_session.flush()
    return entry


class TestProductLinkQueue:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: unittest.mock.MagicMock) -> None:
        pass

    # ── Bloqueante #1: orden de rutas ────────────────────────────────────────

    async def test_get_no_cae_en_get_sale_id_devuelve_200(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Si `/{sale_id}` matcheara primero, esto daría 422 (UUID inválido),
        no 200 — regresión directa del bloqueante #1."""
        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        assert resp.status_code == 200

    # ── GET: agrupación, orden, aislamiento, exclusiones ─────────────────────

    async def test_queue_agrupa_por_nombre_y_ordena_por_cantidad_desc(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Agua sin gas")
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
        await db_session.commit()

        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        assert [(g["raw_name"], g["count"]) for g in groups] == [
            ("Gaseosa cola grande", 2),
            ("Agua sin gas", 1),
        ]

    async def test_queue_sugiere_candidatos_por_nombre(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        product = Product(
            tenant_id=sample_tenant.tenant_id,
            name="Coca Cola 2 Litros",
            sale_price_ars=Decimal("2000"),
            stock_units=5,
        )
        db_session.add(product)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Coca 2lts sin frio")
        await db_session.commit()

        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        group = resp.json()["groups"][0]
        assert any(c["id"] == str(product.id) for c in group["candidates"])

    async def test_queue_aislada_por_tenant(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        second_tenant: Tenant, auth_headers: dict[str, str],
    ) -> None:
        await _unlinked_sale(db_session, second_tenant.tenant_id, "Del otro negocio")
        await db_session.commit()

        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        assert resp.json()["groups"] == []

    async def test_queue_excluye_anuladas(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Venta anulada", voided=True)
        await db_session.commit()

        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        assert resp.json()["groups"] == []

    # ── POST: vinculación, has_user_edits, alias, auditoría, recálculo ───────

    async def test_vincular_setea_product_id_marca_has_user_edits_y_deja_alias(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str], sample_user: Any,
        mock_score_trigger: unittest.mock.MagicMock,
    ) -> None:
        from app.domain.product_alias import product_aliases

        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Coca Cola 2L",
            sale_price_ars=Decimal("2000"), stock_units=5,
        )
        db_session.add(product)
        s1 = await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
        s2 = await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
        await db_session.commit()

        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Gaseosa cola grande", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["linked"] == 2

        await db_session.refresh(s1)
        await db_session.refresh(s2)
        assert s1.product_id == product.id
        assert s2.product_id == product.id
        assert s1.has_user_edits is True, (
            "sin esto una relectura del archivo original podría pisar el vínculo manual"
        )
        assert s1.custom_fields.get("_unlinked_product_name_raw") is None, (
            "el flag queda obsoleto una vez vinculado, no tiene que seguir en la cola"
        )

        await db_session.refresh(product)
        assert "Gaseosa cola grande" in product_aliases(product.custom_fields)

        mock_score_trigger.assert_called_once_with(
            str(sample_tenant.tenant_id), "sales_product_bulk_linked"
        )

        audit = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "SALES_PRODUCT_BULK_LINKED"
                )
            )
        ).scalar_one()
        assert audit.decision_data["linked_count"] == 2
        assert audit.decision_data["raw_name"] == "Gaseosa cola grande"
        assert audit.actor_user_id == sample_user.user_id

    async def test_vincular_preserva_otros_custom_fields(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Producto X",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(product)
        sale = await _unlinked_sale(
            db_session, sample_tenant.tenant_id, "Nombre crudo",
            other_custom_field=True,
        )
        await db_session.commit()

        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Nombre crudo", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        await db_session.refresh(sale)
        assert sale.custom_fields.get("_customer_resolution") == "anonymous"

    async def test_vincular_segunda_vez_es_idempotente(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        from app.domain.product_alias import product_aliases

        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Producto Y",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(product)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Nombre repetido")
        await db_session.commit()

        body = {"raw_name": "Nombre repetido", "target_product_id": str(product.id)}
        first = await client.post(
            "/api/v1/sales/product-link-queue/link", json=body, headers=auth_headers
        )
        assert first.json()["linked"] == 1

        second = await client.post(
            "/api/v1/sales/product-link-queue/link", json=body, headers=auth_headers
        )
        assert second.json()["linked"] == 0, "ya no queda ninguna venta con ese flag"

        await db_session.refresh(product)
        assert product_aliases(product.custom_fields).count("Nombre repetido") == 1

    async def test_vincular_rechaza_producto_de_otro_tenant(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        second_tenant: Tenant, auth_headers: dict[str, str],
    ) -> None:
        ajeno = Product(
            tenant_id=second_tenant.tenant_id, name="Ajeno",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(ajeno)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Cualquiera")
        await db_session.commit()

        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Cualquiera", "target_product_id": str(ajeno.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

    async def test_vincular_rechaza_producto_inactivo(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        inactivo = Product(
            tenant_id=sample_tenant.tenant_id, name="Inactivo",
            sale_price_ars=Decimal("100"), stock_units=1, is_active=False,
        )
        db_session.add(inactivo)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Cualquiera")
        await db_session.commit()

        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Cualquiera", "target_product_id": str(inactivo.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

    async def test_vincular_con_producto_inexistente_rechaza(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Lo que sea", "target_product_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

    async def test_vincular_rechaza_raw_name_solo_espacios(
        self, client: AsyncClient, sample_tenant: Tenant, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "   ", "target_product_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    # ── Guardas: PIN y mantenimiento ──────────────────────────────────────────

    async def test_vincular_sin_ventana_de_pin_devuelve_428(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str], sample_user: Any, fake_redis: Any,
    ) -> None:
        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Producto Z",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(product)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Cualquiera")
        await db_session.commit()

        await fake_redis.delete(_window_key(sample_tenant.tenant_id, sample_user.user_id))
        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Cualquiera", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 428
        assert resp.json()["detail"] == "PIN_REQUIRED"

    async def test_vincular_bloqueado_durante_mantenimiento(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.application.services import maintenance_lock_service

        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Producto W",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(product)
        await _unlinked_sale(db_session, sample_tenant.tenant_id, "Cualquiera")
        await db_session.commit()

        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Cualquiera", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()

    # ── Paginación ────────────────────────────────────────────────────────────

    async def test_paginacion_trunca_y_lo_reporta(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Con topes chicos (monkeypatcheados), la cola corta y avisa
        `truncated=True` en vez de devolver silenciosamente una lista parcial
        como si fuera completa."""
        import app.api.v1.sales as sales_module

        monkeypatch.setattr(sales_module, "_QUEUE_PAGE_SIZE", 3)
        monkeypatch.setattr(sales_module, "_QUEUE_MAX_MATCHES", 2)

        for i in range(4):
            await _unlinked_sale(db_session, sample_tenant.tenant_id, f"Producto {i}")
        await db_session.commit()

        resp = await client.get("/api/v1/sales/product-link-queue", headers=auth_headers)
        body = resp.json()
        assert body["truncated"] is True
        assert sum(g["count"] for g in body["groups"]) == 2

    async def test_vincular_reporta_truncado_cuando_el_grupo_excede_el_tope(
        self, client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant,
        auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regresión (code review): antes el POST descartaba el `truncated`
        que ya calculaba el escaneo — un grupo más grande que el tope se
        vinculaba parcialmente y respondía éxito sin avisar que quedó resto."""
        import app.api.v1.sales as sales_module

        monkeypatch.setattr(sales_module, "_QUEUE_PAGE_SIZE", 3)
        monkeypatch.setattr(sales_module, "_QUEUE_MAX_MATCHES", 2)

        product = Product(
            tenant_id=sample_tenant.tenant_id, name="Producto Truncado",
            sale_price_ars=Decimal("100"), stock_units=1,
        )
        db_session.add(product)
        for _ in range(4):
            await _unlinked_sale(db_session, sample_tenant.tenant_id, "Mismo nombre")
        await db_session.commit()

        resp = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Mismo nombre", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] == 2
        assert body["truncated"] is True

        # Repetir la llamada vincula el resto — idempotente, no re-vincula lo ya hecho.
        second = await client.post(
            "/api/v1/sales/product-link-queue/link",
            json={"raw_name": "Mismo nombre", "target_product_id": str(product.id)},
            headers=auth_headers,
        )
        assert second.json() == {"linked": 2, "truncated": False}
