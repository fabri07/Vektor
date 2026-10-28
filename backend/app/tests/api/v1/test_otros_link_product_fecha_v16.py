"""V-16 — `link-product` lee la fecha de la fila como el lote.

Antes pasaba la fecha CRUDA (`row_data["fecha"]`) a `CreateSaleRequest`:
«13/04/2024» (dd/mm inequívoco) terminaba en un 422 de validación ilegible.
Ahora usa `interpretar_fecha_de_fila_de_otros` (el convenio de su columna,
`CONVENIOS_DE_FECHA_KEY`), y lo que no se puede leer sin adivinar es un 422
`FECHA_REQUERIDA` que dice qué hacer; el cliente la manda en `transaction_date`.

Decisión del dueño (2026-10-27): una fecha corta ambigua sin convenio NUNCA se
adivina; lo inequívoco entra; lo futuro nunca.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import CONVENIOS_DE_FECHA_KEY
from app.domain.business_time import fecha_de_negocio, today_ar
from app.persistence.models.product import Product
from app.persistence.models.product_alias import ProductAlias
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio

_REF = "Negrita chica"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _fila(db: AsyncSession, tenant: Tenant, fecha: str, **extra: str) -> UnclassifiedRecord:
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Venta sin producto",
        row_data={
            "fecha": fecha,
            "monto": "3000",
            "cantidad": "2",
            "medio_de_pago": "cash",
            "producto": _REF,
            **extra,
        },
        suggested_entity="sale",
        status="PENDING",
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


async def _venta(db: AsyncSession, tenant: Tenant) -> SaleEntry:
    db.expunge_all()
    return (
        await db.execute(select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id))
    ).scalar_one()


async def _sin_efectos(db: AsyncSession, tenant: Tenant, rec: UnclassifiedRecord) -> None:
    db.expunge_all()
    for modelo in (SaleEntry, ProductAlias):
        n = (
            await db.execute(
                select(func.count()).select_from(modelo).where(modelo.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
        assert n == 0, modelo
    fila = await db.get(UnclassifiedRecord, rec.id)
    assert fila is not None and fila.status == "PENDING"


async def test_dd_mm_inequivoco_entra_con_su_fecha(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "13/04/2024")

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 200, r.text
    assert fecha_de_negocio((await _venta(db_session, sample_tenant)).transaction_date) == date(
        2024, 4, 13
    )


async def test_usa_el_convenio_de_la_columna(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """En una columna mm/dd, «04/03/2024» es el 3 de abril (como en el lote)."""
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(
        db_session, sample_tenant, "04/03/2024",
        **{CONVENIOS_DE_FECHA_KEY: json.dumps({"fecha": "mdy"})},
    )

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 200, r.text
    assert fecha_de_negocio((await _venta(db_session, sample_tenant)).transaction_date) == date(
        2024, 4, 3
    )


async def test_ambigua_sin_convenio_no_se_adivina_y_se_indica(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "04/03/2024")

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 422, r.text
    detalle = r.json()["detail"]
    assert detalle["code"] == "FECHA_REQUERIDA"
    assert detalle["motivo"] == "convenio_de_fecha_ambiguo"
    assert "04/03/2024" in detalle["message"] and "indicá la fecha" in detalle["message"]
    await _sin_efectos(db_session, sample_tenant, rec)

    # La persona la indica: entra con ésa.
    r = await _vincular(
        client, auth_headers, rec, coca.id, transaction_date="2024-03-04T00:00:00"
    )
    assert r.status_code == 200, r.text
    assert fecha_de_negocio((await _venta(db_session, sample_tenant)).transaction_date) == date(
        2024, 3, 4
    )


async def test_fecha_de_la_fila_futura_pide_la_fecha(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    futura = (today_ar() + timedelta(days=30)).isoformat()
    rec = await _fila(db_session, sample_tenant, futura)

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "FECHA_REQUERIDA"
    assert r.json()["detail"]["motivo"] == "fecha_futura"
    await _sin_efectos(db_session, sample_tenant, rec)


async def test_sin_fecha_legible_pide_la_fecha(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "el martes")

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "FECHA_REQUERIDA"
    await _sin_efectos(db_session, sample_tenant, rec)


async def test_la_fecha_que_manda_el_cliente_no_puede_ser_futura(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "04/03/2024")
    futura = (today_ar() + timedelta(days=2)).isoformat() + "T10:00:00"

    r = await _vincular(client, auth_headers, rec, coca.id, transaction_date=futura)

    assert r.status_code == 422, r.text
    await _sin_efectos(db_session, sample_tenant, rec)
