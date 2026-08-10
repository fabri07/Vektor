"""Tests para reclasificar un registro de "Otros" como cliente o proveedor.

Cubre la extensión del endpoint `POST /others/{id}/reclassify` (además de
venta/gasto/producto):
  - `entity_type="customer"` → crea Customer, record IMPORTED, label "cliente".
  - `entity_type="supplier"` → crea Supplier, record IMPORTED, label "proveedor".
  - fields inválidos (sin name) → 422.
  - record de otro tenant → 404; record ya importado → 409 (guards de
    `_get_pending_record`).
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)


async def _make_pending_record(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str = UNCLASSIFIED_STATUS_PENDING,
    suggested_entity: str | None = None,
) -> UnclassifiedRecord:
    record = UnclassifiedRecord(
        tenant_id=tenant_id,
        source="ingestion",
        context_label="Hoja 1",
        headers=["nombre", "telefono"],
        row_data={"nombre": "Juan", "telefono": "+54 11 1234-5678"},
        suggested_entity=suggested_entity,
        status=status,
    )
    db_session.add(record)
    await db_session.commit()
    return record


class TestReclassifyAsContact:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_reclassify_as_customer(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={"entity_type": "customer", "fields": {"name": "Juan"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Registro importado como cliente."

        # Customer creado para el tenant.
        customers = (
            (
                await db_session.execute(
                    select(Customer).where(Customer.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(customers) == 1
        assert customers[0].name == "Juan"

        # Record marcado como IMPORTED + resolved_at.
        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_IMPORTED
        assert record.resolved_at is not None

    async def test_reclassify_as_supplier(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={"entity_type": "supplier", "fields": {"name": "Distribuidora SA"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "Registro importado como proveedor."

        suppliers = (
            (
                await db_session.execute(
                    select(Supplier).where(Supplier.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(suppliers) == 1
        assert suppliers[0].name == "Distribuidora SA"

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_IMPORTED

    async def test_reclassify_customer_invalid_fields_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={"entity_type": "customer", "fields": {"email": "x@y.com"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422

        # No se creó ningún customer y el record sigue PENDING.
        customers = (
            (
                await db_session.execute(
                    select(Customer).where(Customer.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert customers == []
        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_PENDING

    async def test_reclassify_other_tenant_record_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        second_auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        # Record del primer tenant; el segundo tenant intenta reclasificarlo.
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={"entity_type": "customer", "fields": {"name": "Juan"}},
            headers=second_auth_headers,
        )
        assert resp.status_code == 404

    async def test_reclassify_already_imported_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        record = await _make_pending_record(
            db_session,
            sample_tenant.tenant_id,
            status=UNCLASSIFIED_STATUS_IMPORTED,
        )

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={"entity_type": "supplier", "fields": {"name": "Distribuidora SA"}},
            headers=auth_headers,
        )
        assert resp.status_code == 409
