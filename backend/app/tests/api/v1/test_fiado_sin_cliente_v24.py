"""V-24 — el fiado importado sin cliente va a «Otros», no a «Local».

Decisión del dueño (2026-10-28). El import le ponía el centinela «Local» a toda
venta sin un cliente identificado, también a la fiada (``account``): una deuda a
cobrar de nadie, que sale de la caja y de la proyección y nunca se cobra. La
regla del dominio es que un fiado sin cliente real no se acepta (POST /sales da
400; el lote de «Otros» lo deja en ``needs_manual``). Ahora la venta fiada NUEVA
sin cliente real se saca (con su stock) y su fila va a «Otros» con el motivo,
para resolverla eligiendo el cliente.

Todas las ventas tienen compra previa (``producto_con_compra``): B·5e no
interviene salvo donde el test lo pide.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service
from app.application.services.customer_sentinel import resolve_or_create_local_sentinel
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    MOTIVO_FIADO_SIN_CLIENTE,
    ROW_REF_KEY,
    insert_confirmed_data,
)
from app.application.services.inventory_replay_service import run_inventory_replay
from app.domain.business_time import today_ar
from app.domain.inventory_effect import HISTORICAL_REPLAY
from app.integrations.s3 import S3Client
from app.persistence.models.customer import Customer
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


HACE_3 = (today_ar() - timedelta(days=3)).isoformat()


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    await db.commit()
    return list(
        (
            await db.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None)
                )
            )
        ).scalars()
    )


async def _otros(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        ).scalars()
    )


async def _stock(db: AsyncSession, product_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(select(Product.stock_units).where(Product.id == product_id))
        ).scalar_one()
        or 0
    )


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


def _fila(monto: str, pago: str, **extra: Any) -> dict[str, Any]:
    return {"fecha": HACE_3, "venta": monto, "producto": "Yerba", "cantidad": "2",
            "medio_pago": pago, "__context__": "table", **extra}


async def _confirmar(client: AsyncClient, headers: dict[str, str], f: UploadedFile) -> Any:
    r = await client.post(
        f"/api/v1/ingestion/files/{f.id}/confirm",
        json={"confirmed_fields": {"ventas": True}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── El import ─────────────────────────────────────────────────────────────────


async def test_el_fiado_sin_cliente_va_a_otros_y_el_contado_queda_en_local(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await producto_con_compra(db_session, tid, nombre="Yerba", stock=100)
    f = await _archivo(db_session, tid, [_fila("1000", "Efectivo"), _fila("2000", "Fiado")])

    data = await _confirmar(client, auth_headers, f)

    assert any("1 venta(s) fiadas fueron a «Otros»" in w for w in data["warnings"])
    (venta,) = await _ventas(db_session, tid)
    # El contado sin cliente sigue yendo a «Local», como siempre.
    assert (str(venta.amount), venta.payment_method) == ("1000.00", "cash")
    assert venta.customer_id == await resolve_or_create_local_sentinel(db_session, tid)
    (rec,) = await _otros(db_session, tid)
    assert rec.context_label == MOTIVO_FIADO_SIN_CLIENTE
    assert rec.suggested_entity == "sale" and rec.status == "PENDING"
    assert rec.uploaded_file_id == f.id
    assert rec.row_data["monto"] == "2000.00"
    assert rec.row_data["producto"] == "Yerba"
    assert rec.row_data["medio_de_pago"] == "account"
    assert rec.row_data.get(ROW_REF_KEY)
    # Sin efecto de inventario elegido, el confirm no mueve stock.
    assert await _stock(db_session, yerba.id) == 100


async def test_el_fiado_con_un_cliente_que_no_existe_va_a_otros_con_el_nombre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El archivo nombra un cliente que no está cargado: el import lo deja en
    «Local» por fila (F7c). Para un fiado eso es la misma deuda de nadie; la
    fila de «Otros» dice qué nombre traía, para elegirlo o darlo de alta."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    f = await _archivo(db_session, tid, [_fila("2000", "Libreta", cliente="Pedro Gómez")])
    counts = await insert_confirmed_data(
        db_session, tid, f.parsed_summary_json or {}, {"ventas": True},
        uploaded_file_id=f.id, column_mappings={"cliente": "customer_name"},
    )
    await db_session.commit()
    assert counts["ventas_fiado_sin_cliente"] == 1
    assert await _ventas(db_session, tid) == []
    (rec,) = await _otros(db_session, tid)
    assert rec.context_label == MOTIVO_FIADO_SIN_CLIENTE
    assert rec.row_data["cliente"] == "Pedro Gómez"


async def test_el_fiado_con_cliente_del_archivo_se_importa_con_ese_cliente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    ana = Customer(tenant_id=tid, name="Ana Pérez", doc_type="DNI", dni="30111222")
    db_session.add(ana)
    await db_session.commit()
    f = await _archivo(db_session, tid, [_fila("2000", "Fiado", dni="30111222")])
    counts = await insert_confirmed_data(
        db_session, tid, f.parsed_summary_json or {}, {"ventas": True},
        uploaded_file_id=f.id, column_mappings={"dni": "customer_dni"},
    )
    await db_session.commit()
    assert counts.get("ventas_fiado_sin_cliente", 0) == 0
    (venta,) = await _ventas(db_session, tid)
    assert (venta.payment_method, venta.customer_id) == ("account", ana.id)
    assert await _otros(db_session, tid) == []


async def test_fiado_sin_cliente_y_sin_compra_va_una_vez_con_el_motivo_del_fiado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un solo motivo por fila: el del fiado, porque sólo se resuelve de a una
    (el lote nunca importa un fiado) y el modal exige cliente Y compra."""
    tid = sample_tenant.tenant_id
    db_session.add(Product(tenant_id=tid, name="Yerba", sale_price_ars=Decimal("1000"),
                           stock_units=50))
    await db_session.commit()
    f = await _archivo(db_session, tid, [_fila("2000", "Fiado")])
    counts = await insert_confirmed_data(
        db_session, tid, f.parsed_summary_json or {}, {"ventas": True}, uploaded_file_id=f.id,
    )
    await db_session.commit()
    assert counts["ventas_fiado_sin_cliente"] == 1
    assert counts.get("ventas_sin_compra", 0) == 0
    (rec,) = await _otros(db_session, tid)
    assert rec.context_label == MOTIVO_FIADO_SIN_CLIENTE


async def test_el_chat_tambien_manda_el_fiado_sin_cliente_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba")
    counts = await insert_confirmed_data(
        db_session, tid,
        {"file_type": "spreadsheet", "inferred_type": "ventas", "has_venta": True,
         "ventas_detectadas": [_fila("2000", "Cta. cte.")]},
        {"ventas": True}, source="chat",
    )
    await db_session.commit()
    assert counts["ventas_fiado_sin_cliente"] == 1
    assert await _ventas(db_session, tid) == []
    (rec,) = await _otros(db_session, tid)
    assert (rec.context_label, rec.source) == (MOTIVO_FIADO_SIN_CLIENTE, "chat")


# ── La salida desde «Otros» ───────────────────────────────────────────────────


async def test_desde_otros_se_resuelve_con_un_cliente_real_y_el_lote_no_la_toca(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await producto_con_compra(db_session, tid, nombre="Yerba", stock=100)
    f = await _archivo(db_session, tid, [_fila("2000", "Fiado")])
    await _confirmar(client, auth_headers, f)
    (rec,) = await _otros(db_session, tid)

    # El lote no la importa (sería un fiado de «Local»): la deja para el modal.
    r = await client.post("/api/v1/others/bulk-import", json={}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert (r.json()["imported_sales"], r.json()["needs_manual"]) == (0, 1)
    assert await _ventas(db_session, tid) == []

    campos = {
        "amount": "2000.00", "quantity": 2, "transaction_date": f"{HACE_3}T12:00:00",
        "payment_method": "account", "product_id": str(yerba.id),
    }
    # Sin cliente, como POST /sales: 400 y no se escribe nada.
    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={"entity_type": "sale", "fields": campos}, headers=auth_headers,
    )
    assert r.status_code == 400, r.text
    assert await _ventas(db_session, tid) == []
    assert await _stock(db_session, yerba.id) == 100

    ana = Customer(tenant_id=tid, name="Ana Pérez")
    db_session.add(ana)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={"entity_type": "sale", "fields": {**campos, "customer_id": str(ana.id)}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    (venta,) = await _ventas(db_session, tid)
    assert (venta.payment_method, venta.customer_id, str(venta.amount)) == (
        "account", ana.id, "2000.00"
    )
    assert await _stock(db_session, yerba.id) == 98


# ── Relectura e historia ──────────────────────────────────────────────────────

_CSV = (
    b"fecha,producto,cantidad,monto,cliente,medio_pago\n"
    + f"{HACE_3},Yerba,2,1000,,Efectivo\n".encode()
    + f"{HACE_3},Yerba,2,2000,,Fiado\n".encode()
)


async def _archivo_csv(
    db: AsyncSession, tid: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> UploadedFile:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return _CSV

    monkeypatch.setattr(S3Client, "download", _fake_download)
    summary = parse_uploaded_content(_CSV, "text/csv", "ventas.csv")
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="ventas.csv",
        s3_key=f"tenants/{tid}/ventas.csv", content_type="text/csv", size_bytes=len(_CSV),
        purpose="ventas", processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": {"ventas": True},
        },
    )
    db.add(f)
    await db.commit()
    await insert_confirmed_data(
        db, tid, summary, {"ventas": True}, uploaded_file_id=f.id,
        inventory_effect={"": HISTORICAL_REPLAY},
    )
    await db.flush()
    # La segunda pasada del confirm (F-F.3): descuenta lo que quedó como venta.
    await run_inventory_replay(db, tid, f.id, context_ids=None, apply=True)
    await db.commit()
    return f


async def test_la_relectura_no_duplica_la_fila_en_otros(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.services import reread_service

    tid = sample_tenant.tenant_id
    yerba = await producto_con_compra(db_session, tid, nombre="Yerba", stock=100)
    f = await _archivo_csv(db_session, tid, monkeypatch)
    assert len(await _otros(db_session, tid)) == 1
    # Sólo el contado descontó stock: la fiada que se sacó no se llevó nada.
    assert await _stock(db_session, yerba.id) == 98

    await reread_service.apply_reread(db_session, f.id, tid)
    await db_session.commit()

    pendientes = [o for o in await _otros(db_session, tid) if o.status == "PENDING"]
    assert [o.context_label for o in pendientes] == [MOTIVO_FIADO_SIN_CLIENTE]
    assert [str(v.amount) for v in await _ventas(db_session, tid)] == ["1000.00"]
    assert await _stock(db_session, yerba.id) == 98


async def test_la_relectura_no_rejuzga_el_fiado_historico_en_local(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Límite a propósito: la fila que ya era venta (un fiado de «Local» de antes
    de V-24) no se re-juzga al releer; corregir la historia no es automático."""
    from app.application.services import reread_service

    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba", stock=100)

    async def _sin_v24(*_a: Any, **_k: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(ingestion_import_service, "_fiados_sin_cliente_a_otros", _sin_v24)
        f = await _archivo_csv(db_session, tid, monkeypatch)
    local = await resolve_or_create_local_sentinel(db_session, tid)
    assert {(str(v.amount), v.customer_id) for v in await _ventas(db_session, tid)} == {
        ("1000.00", local), ("2000.00", local)
    }

    await reread_service.apply_reread(db_session, f.id, tid)
    await db_session.commit()

    fiadas = [v for v in await _ventas(db_session, tid) if v.payment_method == "account"]
    assert [(str(v.amount), v.customer_id) for v in fiadas] == [("2000.00", local)]
    assert await _otros(db_session, tid) == []


async def test_un_fiado_de_local_que_ya_estaba_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sólo se juzgan las ventas NUEVAS de la corrida: un fiado de «Local» que ya
    estaba en la base (de otro archivo, de antes) no se toca."""
    tid = sample_tenant.tenant_id
    yerba = await producto_con_compra(db_session, tid, nombre="Yerba")
    local = await resolve_or_create_local_sentinel(db_session, tid)
    vieja = SaleEntry(
        tenant_id=tid, amount=Decimal("500"), quantity=1, payment_method="account",
        transaction_date=datetime.fromisoformat(f"{HACE_3}T10:00:00"),
        product_id=yerba.id, customer_id=local, provenance="REAL",
    )
    db_session.add(vieja)
    await db_session.commit()
    f = await _archivo(db_session, tid, [_fila("2000", "Fiado")])
    await insert_confirmed_data(
        db_session, tid, f.parsed_summary_json or {}, {"ventas": True}, uploaded_file_id=f.id,
    )
    await db_session.commit()
    assert [v.id for v in await _ventas(db_session, tid)] == [vieja.id]
