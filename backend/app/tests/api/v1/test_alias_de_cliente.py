"""Alias de cliente confirmado: «Caro G.», «carolina g» y «Caro Gimenez» son la misma
persona. Un alias lo aprende una persona al resolver desde «Otros» eligiendo el cliente;
el import lo consulta antes de mandar un fiado sin cliente a «Otros». Nunca se aprende
solo y nunca se pisa el de otro cliente."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import customer_alias_service as svc
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.business_time import today_ar
from app.persistence.models.customer import Customer
from app.persistence.models.customer_alias import CustomerAlias
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio

HACE_3 = (today_ar() - timedelta(days=3)).isoformat()


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _cliente(db: AsyncSession, tid: uuid.UUID, nombre: str, apellido: str | None) -> Customer:
    c = Customer(tenant_id=tid, name=nombre, last_name=apellido)
    db.add(c)
    await db.commit()
    return c


async def _archivo(db: AsyncSession, tid: uuid.UUID, filas: list[dict[str, Any]]) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="ventas.xlsx",
        s3_key="ventas.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ventas", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH", "file_type": "spreadsheet", "inferred_type": "ventas",
            "has_venta": True, "row_count": len(filas), "ventas_detectadas": filas,
            "headers": list(filas[0]), "preview_rows": filas,
        },
    )
    db.add(f)
    await db.commit()
    return f


async def _importar_fiado(db: AsyncSession, tid: uuid.UUID, cliente: str) -> dict[str, Any]:
    fila = {"fecha": HACE_3, "venta": "2000", "producto": "Yerba", "cantidad": "2",
            "medio_pago": "Libreta", "cliente": cliente, "__context__": "table"}
    f = await _archivo(db, tid, [fila])
    counts = await insert_confirmed_data(
        db, tid, f.parsed_summary_json or {}, {"ventas": True},
        uploaded_file_id=f.id, column_mappings={"cliente": "customer_name"},
    )
    await db.commit()
    return counts


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    return list(
        (
            await db.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None))
            )
        ).scalars()
    )


# ── El servicio ──────────────────────────────────────────────────────────────


async def test_un_alias_se_aprende_y_se_encuentra_sin_tildes_ni_mayusculas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")

    r = await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=caro.id
    )

    assert r == svc.CREADO
    assert await svc.cliente_por_alias(db_session, tid, "  CARO g. ") == caro.id
    assert await svc.cliente_por_alias(db_session, tid, "otra persona") is None
    again = await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="caro g.", customer_id=caro.id
    )
    assert again == svc.YA_EXISTIA


async def test_un_alias_de_otro_cliente_no_se_pisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    carla = await _cliente(db_session, tid, "Carla", "Gómez")
    await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=caro.id
    )

    r = await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=carla.id
    )

    assert r == svc.CONFLICTO_ALIAS
    assert await svc.cliente_por_alias(db_session, tid, "caro g.") == caro.id


async def test_el_nombre_de_otra_ficha_no_puede_ser_alias(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await _cliente(db_session, tid, "Pedro", "Gómez")

    r = await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="pedro gomez", customer_id=caro.id
    )

    assert r == svc.CONFLICTO_NOMBRE
    assert await svc.cliente_por_alias(db_session, tid, "pedro gomez") is None


async def test_su_propio_nombre_el_vacio_y_el_centinela_no_son_alias(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    assert await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="carolina gimenez", customer_id=caro.id
    ) == svc.SIN_EFECTO
    assert await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="  ", customer_id=caro.id
    ) == svc.SIN_EFECTO
    assert await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Local", customer_id=uuid.uuid4()
    ) == svc.SIN_EFECTO
    n = (await db_session.execute(select(CustomerAlias))).scalars().all()
    assert n == []


async def test_el_alias_de_una_ficha_dada_de_baja_no_cuenta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from datetime import UTC, datetime

    tid = sample_tenant.tenant_id
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=caro.id
    )
    caro.deactivated_at = datetime.now(UTC)
    await db_session.commit()

    assert await svc.cliente_por_alias(db_session, tid, "caro g.") is None


# ── El import ────────────────────────────────────────────────────────────────


async def test_con_alias_el_fiado_se_importa_con_su_cliente_y_sin_alias_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")

    sin_alias = await _importar_fiado(db_session, tid, "Caro G.")
    assert sin_alias.get("ventas_fiado_sin_cliente", 0) == 1
    assert await _ventas(db_session, tid) == []

    await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=caro.id
    )
    await db_session.commit()
    con_alias = await _importar_fiado(db_session, tid, "caro g.")

    assert con_alias.get("ventas_fiado_sin_cliente", 0) == 0
    (venta,) = await _ventas(db_session, tid)
    assert (venta.payment_method, venta.customer_id) == ("account", caro.id)


async def test_si_el_nombre_es_de_otra_ficha_y_el_alias_de_otra_no_se_adivina(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Pedro Gomez", customer_id=caro.id
    )
    # El alias se aprendió ANTES de que existiera la ficha «Pedro Gómez»: ahora el
    # nombre coincide con otra ficha y el alias con Carolina → contradicción.
    await _cliente(db_session, tid, "Pedro", "Gómez")
    await db_session.commit()

    counts = await _importar_fiado(db_session, tid, "pedro gomez")

    assert counts.get("ventas_fiado_sin_cliente", 0) == 1
    assert await _ventas(db_session, tid) == []


# ── «Otros» ──────────────────────────────────────────────────────────────────


async def test_resolver_desde_otros_eligiendo_el_cliente_aprende_el_alias(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await producto_con_compra(db_session, tid, nombre="Yerba", stock=100)
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await _importar_fiado(db_session, tid, "Caro G.")
    rec = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalar_one()
    assert rec.row_data.get("cliente") == "Caro G."

    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={
            "entity_type": "sale",
            "fields": {
                "amount": 2000, "transaction_date": f"{HACE_3}T00:00:00",
                "payment_method": "account", "product_id": str(yerba.id), "quantity": 2,
                "customer_id": str(caro.id),
            },
        },
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert await svc.cliente_por_alias(db_session, tid, "caro g.") == caro.id
    # La próxima carga con ese nombre ya no pide elegir.
    siguiente = await _importar_fiado(db_session, tid, "Caro G.")
    assert siguiente.get("ventas_fiado_sin_cliente", 0) == 0


async def test_la_sugerencia_del_modal_trae_el_cliente_del_alias(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    caro = await _cliente(db_session, tid, "Carolina", "Giménez")
    await _importar_fiado(db_session, tid, "Caro G.")
    rec = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalar_one()
    antes = await client.get(f"/api/v1/others/{rec.id}/customer-suggestion", headers=auth_headers)
    assert antes.json() == {"customer_id": None, "via": None}

    await svc.aprender_alias(
        db_session, tenant_id=tid, user_id=None, texto="Caro G.", customer_id=caro.id
    )
    await db_session.commit()
    despues = await client.get(
        f"/api/v1/others/{rec.id}/customer-suggestion", headers=auth_headers
    )

    assert despues.status_code == 200, despues.text
    assert despues.json() == {"customer_id": str(caro.id), "via": "alias"}
