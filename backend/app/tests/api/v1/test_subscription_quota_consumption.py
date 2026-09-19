"""Bloque C — cada operación consume UNA vez, junto con lo que produce.

Importaciones (síncrona y en segundo plano), lecturas de foto/PDF y la
conciliación de reservas colgadas. El arnés de archivos es el de
``test_importacion_asincronica_e2e`` (ver ahí por qué el ejecutor usa la sesión
del test).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import subscription_reconciliation as rec
from app.application.services import subscription_service as svc
from app.domain.subscription import QuotaResource, import_attempt_operation_id
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.import_attempt import ImportAttempt
from app.persistence.models.tenant import (
    Subscription,
    SubscriptionQuotaReservation,
    SubscriptionQuotaUsage,
    Tenant,
)
from app.tests.api.v1.test_importacion_asincronica_e2e import (
    _MAPEO,
    _archivo,
    _cuerpo,
    _ejecutar,
    _gastos,
)

P = "/api/v1"
_PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu."""


@pytest.fixture
def habilitado(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.v1.ingestion as api

    monkeypatch.setattr(api, "async_import_enabled_for", lambda _t: True)


@pytest.fixture
def ejecutor_con_la_sesion_del_test(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    import app.jobs.import_executor_worker as worker

    @contextlib.asynccontextmanager
    async def _factory() -> Any:
        yield db_session

    monkeypatch.setattr(worker, "_sesion_de_worker", lambda: (_factory, None))


async def _plan(db: AsyncSession, tenant: Tenant, **valores: Any) -> Subscription:
    """El FREE del fixture pasa a un plan pago con cupos chicos y medibles."""
    ahora = datetime.now(UTC)
    base: dict[str, Any] = {
        "plan_code": "control",
        "status": "ACTIVE",
        "seats_included": 3,
        "granted_ia_queries_per_month": 5,
        "granted_imports_per_month": 2,
        "granted_photo_pdf_reads_per_month": 2,
        "current_period_start": ahora - timedelta(days=1),
        "current_period_end": ahora + timedelta(days=29),
    }
    await db.execute(
        update(Subscription)
        .where(Subscription.tenant_id == tenant.tenant_id)
        .values(**base | valores)
    )
    await db.commit()
    db.expunge_all()
    return (
        await db.execute(select(Subscription).where(Subscription.tenant_id == tenant.tenant_id))
    ).scalar_one()


async def _uso(db: AsyncSession, tenant: Tenant, recurso: QuotaResource) -> tuple[int, int]:
    db.expunge_all()
    fila = (
        await db.execute(
            select(SubscriptionQuotaUsage.used, SubscriptionQuotaUsage.reserved).where(
                SubscriptionQuotaUsage.tenant_id == tenant.tenant_id,
                SubscriptionQuotaUsage.resource == recurso.value,
            )
        )
    ).first()
    return (0, 0) if fila is None else (fila.used, fila.reserved)


async def _estado_del_archivo(db: AsyncSession, file_id: uuid.UUID) -> str:
    db.expunge_all()
    return (
        await db.execute(
            select(UploadedFile.processing_status).where(UploadedFile.id == file_id)
        )
    ).scalar_one()


def _confirm() -> dict[str, Any]:
    return {"column_mappings": _MAPEO, "confirmed_fields": {"gastos": True}}


# ── Importación síncrona ──────────────────────────────────────────────────────


async def test_confirm_consume_una_importacion_junto_con_los_datos(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)

    resp = await client.post(
        f"{P}/ingestion/files/{archivo.id}/confirm", json=_confirm(), headers=auth_headers
    )

    assert resp.status_code == 200, resp.text
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 3
    # Consumida y sin nada colgado: no hay reserva que conciliar en este camino.
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (1, 0)


async def test_confirm_sin_cupo_responde_429_antes_de_tomar_el_lease(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant, granted_imports_per_month=0)
    archivo = await _archivo(db_session, sample_tenant)

    resp = await client.post(
        f"{P}/ingestion/files/{archivo.id}/confirm", json=_confirm(), headers=auth_headers
    )

    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["resource"] == "import"
    assert await _gastos(db_session, sample_tenant.tenant_id) == []
    assert await _estado_del_archivo(db_session, archivo.id) == (
        PROCESSING_STATUS_NEEDS_CONFIRMATION
    )


async def test_perder_la_ultima_unidad_al_final_revierte_todo_y_suelta_el_archivo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El chequeo temprano no es vinculante: otra importación puede llevarse la
    última unidad mientras esta trabaja. El consumo real falla al final, y tiene
    que irse TODO — datos, consumo y el IMPORTING del archivo — con un 429, no
    con un 500 (la excepción llega cruda al `except` que compensa el lease)."""
    await _plan(db_session, sample_tenant, granted_imports_per_month=0)
    archivo = await _archivo(db_session, sample_tenant)

    async def _todavia_habia(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(svc, "assert_quota_available", _todavia_habia)

    resp = await client.post(
        f"{P}/ingestion/files/{archivo.id}/confirm", json=_confirm(), headers=auth_headers
    )

    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["code"] == "QUOTA_EXCEEDED"
    assert await _gastos(db_session, sample_tenant.tenant_id) == []
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (0, 0)
    assert await _estado_del_archivo(db_session, archivo.id) == (
        PROCESSING_STATUS_NEEDS_CONFIRMATION
    )


async def test_un_cliente_no_puede_pasar_la_clave_de_una_importacion_ya_cobrada(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """`quota_operation_id` no es un query param: si lo fuera, mandar la clave
    de una importación ya confirmada haría gratis todas las siguientes."""
    await _plan(db_session, sample_tenant)
    primero = await _archivo(db_session, sample_tenant)
    await client.post(
        f"{P}/ingestion/files/{primero.id}/confirm", json=_confirm(), headers=auth_headers
    )
    db_session.expunge_all()
    clave_cobrada = (
        await db_session.execute(
            select(SubscriptionQuotaReservation.operation_id).where(
                SubscriptionQuotaReservation.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalar_one()

    segundo = await _archivo(db_session, sample_tenant)
    resp = await client.post(
        f"{P}/ingestion/files/{segundo.id}/confirm",
        params={"quota_operation_id": clave_cobrada},
        json=_confirm(),
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (2, 0)


# ── Importación en segundo plano ──────────────────────────────────────────────


@pytest.mark.usefixtures("habilitado", "ejecutor_con_la_sesion_del_test")
async def test_registrar_reserva_y_ejecutar_confirma_esa_misma_unidad(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)

    registro = await client.post(
        f"{P}/ingestion/files/{archivo.id}/imports", json=_cuerpo(), headers=auth_headers
    )
    assert registro.status_code == 202, registro.text
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (0, 1)

    # La misma petición otra vez devuelve el mismo intento: no reserva de nuevo.
    repetida = await client.post(
        f"{P}/ingestion/files/{archivo.id}/imports", json=_cuerpo(), headers=auth_headers
    )
    assert repetida.json()["attempt_id"] == registro.json()["attempt_id"]
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (0, 1)

    assert await _ejecutar(registro.json()["attempt_id"]) == "completado"
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 3
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (1, 0)


@pytest.mark.usefixtures("habilitado")
async def test_registrar_sin_cupo_responde_429_y_no_deja_intento(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant, granted_imports_per_month=0)
    archivo = await _archivo(db_session, sample_tenant)

    resp = await client.post(
        f"{P}/ingestion/files/{archivo.id}/imports", json=_cuerpo(), headers=auth_headers
    )

    assert resp.status_code == 429, resp.text
    db_session.expunge_all()
    assert (await db_session.execute(select(ImportAttempt))).scalars().all() == []


@pytest.mark.usefixtures("habilitado", "ejecutor_con_la_sesion_del_test")
async def test_lo_ya_autorizado_termina_aunque_la_suscripcion_venza_antes_de_correr(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"{P}/ingestion/files/{archivo.id}/imports", json=_cuerpo(), headers=auth_headers
    )
    await _plan(db_session, sample_tenant, status="READ_ONLY")

    assert await _ejecutar(registro.json()["attempt_id"]) == "completado"
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 3
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (1, 0)


@pytest.mark.usefixtures("habilitado", "ejecutor_con_la_sesion_del_test")
async def test_un_intento_que_fracasa_libera_su_reserva(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant)
    archivo = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"{P}/ingestion/files/{archivo.id}/imports", json=_cuerpo(), headers=auth_headers
    )
    # El archivo desaparece antes de ejecutar: fracaso definitivo.
    await db_session.execute(
        update(UploadedFile)
        .where(UploadedFile.id == archivo.id)
        .values(deleted_at=datetime.now(UTC))
    )
    await db_session.commit()

    assert await _ejecutar(registro.json()["attempt_id"]) == "fallado"
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (0, 0)


# ── Lecturas de foto/PDF ──────────────────────────────────────────────────────


def _lectura_falsa(
    monkeypatch: pytest.MonkeyPatch, *, campos: dict[str, Any], usage: Any
) -> list[int]:
    from app.application.services.customer_extraction_service import CustomerExtraction

    llamadas: list[int] = []

    async def _extract(*_a: Any, **_k: Any) -> Any:
        llamadas.append(1)
        return CustomerExtraction(fields=campos), usage

    monkeypatch.setattr("app.api.v1.customers.extract_customer", _extract)
    return llamadas


async def _leer(client: AsyncClient, headers: dict[str, str], nombre: str, contenido: bytes) -> Any:
    return await client.post(
        f"{P}/customers/extract",
        files={"file": (nombre, contenido, "application/octet-stream")},
        headers=headers,
    )


async def test_una_foto_leida_con_ia_consume_una_lectura(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _plan(db_session, sample_tenant)
    _lectura_falsa(
        monkeypatch, campos={"name": "Ana"}, usage={"input_tokens": 900, "output_tokens": 80}
    )
    resp = await _leer(client, auth_headers, "dni.png", _PNG)
    assert resp.status_code == 200, resp.text
    assert await _uso(db_session, sample_tenant, QuotaResource.PHOTO_PDF_READ) == (1, 0)


async def test_una_lectura_que_no_devuelve_nada_no_consume(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El servicio es fail-soft: si el modelo falla devuelve vacío. Cuesta
    tokens, pero un error técnico no consume unidad del cliente."""
    await _plan(db_session, sample_tenant)
    _lectura_falsa(monkeypatch, campos={}, usage=None)
    resp = await _leer(client, auth_headers, "dni.png", _PNG)
    assert resp.status_code == 200, resp.text
    assert await _uso(db_session, sample_tenant, QuotaResource.PHOTO_PDF_READ) == (0, 0)


async def test_una_planilla_no_toca_el_cupo_de_lecturas(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _plan(db_session, sample_tenant, granted_photo_pdf_reads_per_month=0)
    csv = b"nombre,dni\nAna,30111222\n"
    resp = await _leer(client, auth_headers, "cliente.csv", csv)
    assert resp.status_code == 200, resp.text  # sin cupo de lecturas, y pasa igual
    assert await _uso(db_session, sample_tenant, QuotaResource.PHOTO_PDF_READ) == (0, 0)


async def test_sin_cupo_de_lecturas_no_se_llama_al_modelo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _plan(db_session, sample_tenant, granted_photo_pdf_reads_per_month=0)
    llamadas = _lectura_falsa(monkeypatch, campos={"name": "Ana"}, usage={"input_tokens": 1})
    resp = await _leer(client, auth_headers, "dni.png", _PNG)
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["resource"] == "photo_pdf_read"
    assert llamadas == []


# ── Conciliación ──────────────────────────────────────────────────────────────


async def _reserva_colgada(
    db: AsyncSession, sub: Subscription, recurso: QuotaResource, op: str, *, hace: timedelta
) -> None:
    await svc.reserve(db, subscription=sub, resource=recurso, operation_id=op)
    await db.execute(
        update(SubscriptionQuotaReservation)
        .where(SubscriptionQuotaReservation.operation_id == op)
        .values(created_at=datetime.now(UTC) - hace)
    )
    await db.commit()


async def _veredictos(db: AsyncSession, *, apply: bool) -> dict[str, str]:
    resultado = await rec.reconcile(db, apply=apply)
    await db.commit()
    return {v.operation_id: v.accion for v in resultado.veredictos}


async def test_un_chat_viejo_sin_rastro_se_libera_y_uno_auditado_se_confirma(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _plan(db_session, sample_tenant)
    viejo = timedelta(hours=3)
    await _reserva_colgada(db_session, sub, QuotaResource.IA_QUERY, "chat:sin-rastro", hace=viejo)
    await _reserva_colgada(db_session, sub, QuotaResource.IA_QUERY, "chat:auditado", hace=viejo)
    db_session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            decision_type="CHAT_TURN",
            decision_data={},
            triggered_by="test",
            trace_id="auditado",
            tokens_total=1200,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    assert await _veredictos(db_session, apply=True) == {
        "chat:sin-rastro": rec.LIBERAR,
        "chat:auditado": rec.CONFIRMAR,
    }
    assert await _uso(db_session, sample_tenant, QuotaResource.IA_QUERY) == (1, 0)
    # Idempotente: ya no queda nada en RESERVED.
    assert await _veredictos(db_session, apply=True) == {}


async def test_nunca_se_libera_solo_por_antiguedad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un intento VIVO conserva su reserva tenga la edad que tenga; una clave
    que nadie reconoce se conserva y se reporta; un request reciente puede
    seguir corriendo."""
    sub = await _plan(db_session, sample_tenant, granted_imports_per_month=5)
    archivo = await _archivo(db_session, sample_tenant)
    intento = ImportAttempt(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        file_id=archivo.id,
        request_key="vivo",
        payload_hash="x" * 64,
        payload_json={},
        status="EJECUTANDO",
    )
    db_session.add(intento)
    await db_session.commit()
    un_mes = timedelta(days=30)
    await _reserva_colgada(
        db_session, sub, QuotaResource.IMPORT, import_attempt_operation_id(intento.id), hace=un_mes
    )
    await _reserva_colgada(db_session, sub, QuotaResource.IMPORT, "algo:raro", hace=un_mes)
    await _reserva_colgada(
        db_session, sub, QuotaResource.IA_QUERY, "chat:reciente", hace=timedelta(minutes=5)
    )

    assert await _veredictos(db_session, apply=True) == {
        import_attempt_operation_id(intento.id): rec.NO_TOCAR,
        "algo:raro": rec.AMBIGUA,
        "chat:reciente": rec.NO_TOCAR,
    }
    assert await _uso(db_session, sample_tenant, QuotaResource.IMPORT) == (0, 2)


async def test_el_dry_run_no_escribe(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    sub = await _plan(db_session, sample_tenant)
    await _reserva_colgada(
        db_session, sub, QuotaResource.PHOTO_PDF_READ, "read:x", hace=timedelta(hours=3)
    )
    assert await _veredictos(db_session, apply=False) == {"read:x": rec.LIBERAR}
    assert await _uso(db_session, sample_tenant, QuotaResource.PHOTO_PDF_READ) == (0, 1)
