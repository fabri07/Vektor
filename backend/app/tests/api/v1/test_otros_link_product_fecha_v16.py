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
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    CONVENIOS_DE_FECHA_KEY,
    MONTO_CANONICO_KEY,
)
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


@pytest.mark.parametrize(
    ("monto", "canonico", "esperado"),
    [
        ("1.234,50", False, Decimal("1234.50")),
        # La forma canónica que escribe Véktor (B·5e/B·5g: `str(Decimal)`), con su marca.
        ("1200.00", True, Decimal("1200.00")),
        ("1500.5", True, Decimal("1500.5")),
    ],
)
async def test_el_monto_se_lee_como_en_el_lote(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant, monto: str, canonico: bool, esperado: Decimal,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    marca = {MONTO_CANONICO_KEY: "1"} if canonico else {}
    rec = await _fila(db_session, sample_tenant, "13/04/2024", monto=monto, **marca)

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 200, r.text
    assert (await _venta(db_session, sample_tenant)).amount == esperado


@pytest.mark.parametrize("monto", ["NaN", "12.500", "1.500", "12,50", ""])
async def test_monto_ilegible_o_ambiguo_no_se_adivina(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant, monto: str,
) -> None:
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "13/04/2024", monto=monto)

    r = await _vincular(client, auth_headers, rec, coca.id)

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "MONTO_REQUERIDO"
    assert "«Editar»" in r.json()["detail"]["message"]
    await _sin_efectos(db_session, sample_tenant, rec)


@pytest.mark.parametrize("campo", ["amount", "transaction_date"])
async def test_reclassify_con_dato_invalido_da_422_legible_y_no_500(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant, campo: str,
) -> None:
    """Un `ValueError` de un validador (fecha futura) viaja en `ctx`: sin sacarlo,
    el 422 no se podía pasar a JSON y era un 500. El monto «NaN» da 422 igual."""
    coca = await _coca(db_session, sample_tenant)
    rec = await _fila(db_session, sample_tenant, "13/04/2024")
    campos: dict[str, Any] = {
        "amount": "3000",
        "transaction_date": "2024-04-13T00:00:00",
        "product_id": str(coca.id),
        "quantity": 1,
    }
    campos[campo] = (
        "NaN" if campo == "amount" else (today_ar() + timedelta(days=3)).isoformat() + "T10:00:00"
    )

    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={"entity_type": "sale", "fields": campos},
        headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    errores = r.json()["detail"]
    assert isinstance(errores, list) and errores[0]["loc"] == [campo]
    assert all("ctx" not in e for e in errores)
    await _sin_efectos(db_session, sample_tenant, rec)


async def _lote(client: AsyncClient, h: dict[str, Any]) -> dict[str, int]:
    r = await client.post("/api/v1/others/bulk-import", json={}, headers=h)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_el_lote_lee_el_monto_canonico_de_una_venta_de_b5e(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La fila que deja B·5e/B·5g («1200.00») entra por el lote, como por link-product."""
    vela = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Vela aromatica")
    db_session.add(
        UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            source="ingestion",
            context_label="Venta sin compra previa",
            row_data={
                "fecha": "2024-03-05", "monto": "1200.00", "cantidad": "1",
                "producto": vela.name, "medio_de_pago": "cash",
                MONTO_CANONICO_KEY: "1",
            },
            suggested_entity="sale",
            status="PENDING",
        )
    )
    await db_session.commit()

    cuerpo = await _lote(client, auth_headers)

    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (1, 0), cuerpo
    assert (await _venta(db_session, sample_tenant)).amount == Decimal("1200.00")


@pytest.mark.parametrize("monto", ["12.500", "1.500", "12,50"])
async def test_el_lote_no_adivina_un_monto_ambiguo(
    client: AsyncClient, auth_headers: dict[str, Any], db_session: AsyncSession,
    sample_tenant: Tenant, monto: str,
) -> None:
    db_session.add(
        UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            source="ingestion",
            context_label="Monto ilegible",
            row_data={"fecha": "2024-05-01", "detalle": "Pago luz", "monto": monto,
                      "categoria": "Luz"},
            suggested_entity="expense",
            status="PENDING",
        )
    )
    await db_session.commit()

    cuerpo = await _lote(client, auth_headers)

    assert (cuerpo["imported_expenses"], cuerpo["needs_manual"]) == (0, 1), cuerpo
