"""M·1c — el costo del producto deja de ser «el último que escribió».

Contra las rutas reales: compra por ``increment_stock`` (manual, «Otros», chat),
PATCH de producto, import de catálogo y su reversa por borrado de archivo.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_deletion_service import (
    record_import_ledger,
    revert_file_data,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.stock_service import increment_stock
from app.domain.costo_vigente import CATALOGO_FIELD, MANUAL_FIELD, ORIGEN_FIELD
from app.domain.ingestion_version import INGESTION_VERSION
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def _producto(db: AsyncSession, tenant: Tenant, nombre: str = "Vela") -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("200"),
        unit_cost_ars=Decimal("60"),
        stock_units=0,
    )
    db.add(p)
    await db.flush()
    return p


async def _comprar(
    db: AsyncSession, tenant: Tenant, p: Product, costo: str, fecha: datetime
) -> None:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await increment_stock(
            p.id, tenant.tenant_id, 1, Decimal(costo), "src", db, occurred_at=fecha
        )
    await db.refresh(p)


async def test_un_comprobante_atrasado_no_pisa_una_compra_mas_nueva(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", datetime(2026, 9, 10, 10))
    await _comprar(db_session, sample_tenant, p, "80", datetime(2026, 9, 1, 10))
    assert p.unit_cost_ars == Decimal("100.00")
    assert (p.custom_fields or {})[ORIGEN_FIELD] == "compra"


async def test_compra_posterior_a_la_edicion_manual_la_reemplaza(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    p.custom_fields = {MANUAL_FIELD: {"costo": "90", "fecha": "2026-09-05T15:00:00"}}
    await db_session.flush()
    await _comprar(db_session, sample_tenant, p, "100", datetime(2026, 9, 10, 10))
    assert p.unit_cost_ars == Decimal("100.00")


async def test_compra_anterior_a_la_edicion_manual_no_la_pisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    p.unit_cost_ars = Decimal("90")
    p.custom_fields = {MANUAL_FIELD: {"costo": "90", "fecha": "2026-09-20T15:00:00"}}
    await db_session.flush()
    await _comprar(db_session, sample_tenant, p, "100", datetime(2026, 9, 10, 10))
    assert p.unit_cost_ars == Decimal("90")
    assert (p.custom_fields or {})[ORIGEN_FIELD] == "manual"


class TestPatch:
    async def _crear(self, client: AsyncClient, headers: dict[str, Any]) -> str:
        r = await client.post(
            "/api/v1/products",
            json={"name": "Difusor", "sale_price_ars": "500", "unit_cost_ars": "300"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def test_el_alta_y_la_edicion_quedan_como_manual_con_hora(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await self._crear(client, auth_headers)
        p = await db_session.get(Product, uuid.UUID(pid))
        assert p is not None
        assert (p.custom_fields or {})[MANUAL_FIELD]["costo"] == "300"

        r = await client.patch(
            f"/api/v1/products/{pid}", json={"unit_cost_ars": "320"}, headers=auth_headers
        )
        assert r.status_code == 200, r.text
        await db_session.refresh(p)
        assert (p.custom_fields or {})[MANUAL_FIELD]["costo"] == "320"

    async def test_reenviar_el_mismo_costo_no_resella(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await self._crear(client, auth_headers)
        p = await db_session.get(Product, uuid.UUID(pid))
        assert p is not None
        sello = (p.custom_fields or {})[MANUAL_FIELD]
        r = await client.patch(
            f"/api/v1/products/{pid}",
            json={"name": "Difusor grande", "unit_cost_ars": "300.00"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        await db_session.refresh(p)
        assert (p.custom_fields or {})[MANUAL_FIELD] == sello

    async def test_el_cliente_no_puede_falsificar_la_edicion(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession
    ) -> None:
        pid = await self._crear(client, auth_headers)
        r = await client.patch(
            f"/api/v1/products/{pid}",
            json={
                "custom_fields": {
                    "color": "rojo",
                    MANUAL_FIELD: {"costo": "1", "fecha": "2099-01-01T00:00:00"},
                }
            },
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        p = await db_session.get(Product, uuid.UUID(pid))
        assert p is not None
        await db_session.refresh(p)
        cf = p.custom_fields or {}
        assert cf["color"] == "rojo"
        assert cf[MANUAL_FIELD]["costo"] == "300"


async def test_borrar_el_catalogo_no_deja_su_costo_para_el_proximo_recalculo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El catálogo sella su costo (decisión 3); borrar el archivo lo devuelve
    TODO. Si quedara la edición de catálogo guardada, la compra siguiente —más
    vieja que la carga— recalcularía y la resucitaría."""
    archivo = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="lista.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/lista.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    db_session.add(archivo)
    p = await _producto(db_session, sample_tenant, "Azucar 1kg")

    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "stock",
            "has_producto": True,
            "stock_detectado": [
                {"producto": "Azucar 1kg", "precio_venta": "300", "costo_unitario": "75"}
            ],
        },
        {"productos": True},
        return_details=True,
        uploaded_file_id=archivo.id,
    )
    await db_session.flush()
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("75")
    assert (p.custom_fields or {})[ORIGEN_FIELD] == "catalogo"

    await record_import_ledger(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        file_id=archivo.id,
        product_details=counts.pop("product_details", []) or [],
    )
    await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("60")
    assert CATALOGO_FIELD not in (p.custom_fields or {})

    await _comprar(db_session, sample_tenant, p, "50", datetime(2026, 1, 10, 10))
    assert p.unit_cost_ars == Decimal("50.00")
