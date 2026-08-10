"""Tests del servicio de proveedores (lecturas tenant-scoped sobre la entidad Supplier).

Regresión del fix de review: get_supplier_by_email/get_approved_senders deben
excluir proveedores desactivados (soft-delete), no solo derivar el status.
"""


from app.application.services.supplier_service import (
    get_approved_senders,
    get_supplier_by_email,
)
from app.persistence.models.supplier import Supplier
from app.persistence.repositories.supplier_repository import SupplierRepository


async def test_get_supplier_by_email_and_approved_senders_exclude_deactivated(
    db_session, sample_tenant
) -> None:
    tenant_id = str(sample_tenant.tenant_id)
    repo = SupplierRepository(db_session)
    supplier = Supplier(
        tenant_id=sample_tenant.tenant_id, name="Proveedor", email="prov@x.com"
    )
    await repo.save(supplier)

    # Activo → se encuentra y aparece en el allowlist.
    found = await get_supplier_by_email("prov@x.com", tenant_id, db_session)
    assert found is not None
    assert found["status"] == "active"
    assert "prov@x.com" in await get_approved_senders(tenant_id, db_session)

    # Soft-delete → ya no se resuelve por email ni cuenta como approved sender.
    await repo.soft_delete(supplier)
    assert await get_supplier_by_email("prov@x.com", tenant_id, db_session) is None
    assert "prov@x.com" not in await get_approved_senders(tenant_id, db_session)
