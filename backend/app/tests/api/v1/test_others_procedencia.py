"""Clasificar una fila de "Otros" a mano deja rastro de su archivo.

Antes, el registro que nacía de clasificar manualmente una fila NO recibía
`source_upload_id`, y `Product`/`Customer`/`Supplier` ni siquiera tienen esa
columna. Resultado: el dato quedaba huérfano y sobrevivía al borrado del archivo
que lo trajo, sin manera de saber de dónde había salido.

Ahora: venta y gasto llevan `source_upload_id` + `source_row_ref`; los maestros y
los productos —que no tienen columna de origen— dejan su procedencia en el ledger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

pytestmark = pytest.mark.asyncio


async def _archivo(db_session: AsyncSession, tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="mixto.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/mixto.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=512,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
    )
    db_session.add(record)
    await db_session.flush()
    return record


async def _fila(
    db_session: AsyncSession, tenant: Tenant, archivo: UploadedFile
) -> UnclassifiedRecord:
    record = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        uploaded_file_id=archivo.id,
        source="ingestion",
        context_label="Hoja 1",
        headers=["detalle", "monto"],
        row_data={"detalle": "algo", "monto": "500"},
        status=UNCLASSIFIED_STATUS_PENDING,
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _items(db_session: AsyncSession, file_id: uuid.UUID) -> list[DataRepairItem]:
    res = await db_session.execute(
        select(DataRepairItem).where(DataRepairItem.source_file_id == file_id)
    )
    return list(res.scalars().all())


class TestProcedenciaDesdeOtros:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        pass

    async def test_la_venta_lleva_el_archivo_de_origen(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        archivo = await _archivo(db_session, sample_tenant)
        fila = await _fila(db_session, sample_tenant, archivo)

        resp = await client.post(
            f"/api/v1/others/{fila.id}/reclassify",
            json={
                "entity_type": "sale",
                "fields": {
                    "amount": "500.00",
                    "transaction_date": "2026-07-15T10:00:00",
                    "payment_method": "cash",
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        venta = (await db_session.execute(select(SaleEntry))).scalars().one()
        assert venta.source_upload_id == archivo.id
        # Derivado del id de la fila, no de sus valores: estable aunque el
        # usuario corrija un campo antes de clasificar.
        assert venta.source_row_ref == f"unclassified:{fila.id}"

    async def test_el_cliente_creado_deja_su_procedencia_en_el_ledger(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        """`Customer` no tiene columna de origen: el vínculo sólo puede vivir acá."""
        archivo = await _archivo(db_session, sample_tenant)
        fila = await _fila(db_session, sample_tenant, archivo)

        resp = await client.post(
            f"/api/v1/others/{fila.id}/reclassify",
            json={"entity_type": "customer", "fields": {"name": "Marta Ruiz"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        items = await _items(db_session, archivo.id)
        assert [i.action for i in items] == ["CREATE_CUSTOMER"]
        assert (items[0].after_json or {}).get("name") == "Marta Ruiz"

    async def test_vincular_a_un_producto_existente_guarda_su_valor_anterior(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        """Esta rama pisa precio/costo de un producto PREEXISTENTE. Sin el `before`
        guardado, borrar el archivo no puede devolvérselo."""
        archivo = await _archivo(db_session, sample_tenant)
        fila = await _fila(db_session, sample_tenant, archivo)
        producto = Product(
            tenant_id=sample_tenant.tenant_id,
            name="Ya existía",
            sale_price_ars=Decimal("100"),
            unit_cost_ars=Decimal("60"),
            stock_units=5,
        )
        db_session.add(producto)
        await db_session.commit()

        resp = await client.post(
            f"/api/v1/others/{fila.id}/reclassify",
            json={
                "entity_type": "product",
                "target_product_id": str(producto.id),
                "fields": {"sale_price_ars": "999.00"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        items = await _items(db_session, archivo.id)
        assert [i.action for i in items] == ["UPDATE_PRODUCT"]
        # Como Decimal: SQLite no fija la escala del NUMERIC ("100") y Postgres sí
        # ("100.00"). Comparar strings ataría el test al motor.
        assert Decimal((items[0].before_json or {})["sale_price_ars"]) == Decimal("100")
        assert Decimal((items[0].after_json or {})["sale_price_ars"]) == Decimal("999")

    async def test_una_fila_sin_archivo_no_inventa_procedencia(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
    ) -> None:
        """Una fila cargada por chat no tiene archivo: no hay nada que registrar."""
        fila = UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            uploaded_file_id=None,
            source="chat",
            row_data={"detalle": "algo"},
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        db_session.add(fila)
        await db_session.commit()

        resp = await client.post(
            f"/api/v1/others/{fila.id}/reclassify",
            json={"entity_type": "customer", "fields": {"name": "Sin archivo"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

        res = await db_session.execute(select(DataRepairItem))
        assert res.scalars().all() == []
