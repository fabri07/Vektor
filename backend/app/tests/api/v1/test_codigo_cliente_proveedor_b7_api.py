"""B·7 por HTTP: el catálogo ofrece el código de cliente/proveedor, el confirm
vincula por código y avisa cuando un código no tiene ficha o choca con otra clave.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests._respaldo import productos_con_compra

pytestmark = pytest.mark.asyncio

_CTX = "sheet:Ventas"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _summary(codigos: list[str]) -> dict[str, Any]:
    filas = [
        {
            "Fecha": f"2024-03-{15 + i}",
            "Monto": "1500",
            "Producto": "Vela",
            "Codigo cliente": cod,
            "__context__": _CTX,
        }
        for i, cod in enumerate(codigos)
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_venta": True,
        "row_count": len(filas),
        "ventas_detectadas": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["Fecha", "Monto", "Producto", "Codigo cliente"],
                "fields": None,
                "preview_rows": filas,
                "row_count": len(filas),
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    await productos_con_compra(db_session, sample_tenant.tenant_id, ["Vela"])
    db_session.add(
        Customer(tenant_id=sample_tenant.tenant_id, name="Cliente Uno", external_code="C-1")
    )
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key="uploads/test/uuid/ventas.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(["C-1", "C-404"]),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _m(source: str, target: str) -> dict[str, Any]:
    return {"source_column": source, "target_field": target, "context_id": _CTX,
            "entity_type": "sale"}


async def test_el_catalogo_ofrece_el_codigo_de_cliente_y_de_proveedor(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    r = await client.get("/api/v1/ingestion/field-catalog", headers=auth_headers)
    assert r.status_code == 200
    venta = {f["value"]: f for f in r.json()["sale"]["fields"]}
    compra = {f["value"]: f for f in r.json()["expense"]["fields"]}
    assert venta["customer_external_code"]["label"] == "Cliente — Código"
    assert venta["customer_external_code"]["single_value"] is True
    assert compra["supplier_external_code"]["label"] == "Proveedor — Código"


async def test_confirm_vincula_por_codigo_y_avisa_el_desconocido(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    r = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                _m("Fecha", "transaction_date"),
                _m("Monto", "amount"),
                _m("Producto", "product_name"),
                _m("Codigo cliente", "customer_external_code"),
            ],
            "confirmed_fields": {"ventas": True},
            "context_confirmed": {_CTX: True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    avisos = " ".join(r.json()["warnings"])
    assert "1 venta(s) traen un código de cliente que no tiene ninguna ficha" in avisos

    cliente = (
        await db_session.execute(select(Customer).where(Customer.external_code == "C-1"))
    ).scalar_one()
    vinculadas = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.tenant_id == sample_tenant.tenant_id,
                SaleEntry.customer_id == cliente.id,
            )
        )
    ).scalars().all()
    assert len(vinculadas) == 1


async def test_dos_columnas_al_codigo_del_cliente_es_422(
    client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
) -> None:
    r = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                _m("Fecha", "transaction_date"),
                _m("Monto", "amount"),
                _m("Producto", "customer_external_code"),
                _m("Codigo cliente", "customer_external_code"),
            ],
            "confirmed_fields": {"ventas": True},
            "context_confirmed": {_CTX: True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
