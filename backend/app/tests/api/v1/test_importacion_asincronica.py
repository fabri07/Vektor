"""E6c-3 — el contrato de la importación asíncrona, y la compuerta que la gatea.

Qué se afirma acá
-----------------
* **202 con un id consultable**, y la consulta devuelve estado, progreso, y el
  resultado o un error accionable.
* **Repetir la misma petición devuelve el mismo intento** — es lo único que hace
  seguro reintentar después de un timeout.
* **Misma clave con otro contenido es un conflicto**, no una importación nueva ni
  la vieja disfrazada.
* **La consulta verifica pertenencia**: un id de intento no es un secreto.
* **La compuerta gatea sólo el registro**: apagarla no puede abandonar lo que ya
  entró.

La concurrencia entre las dos rutas se prueba aparte, contra Postgres real
(``test_importacion_asincronica_pg``): en SQLite en memoria no hay dos
transacciones que puedan pisarse.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox
from app.persistence.models.tenant import Tenant

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAPEO = [
    {"source_column": "fecha", "target_field": "expense_date"},
    {"source_column": "detalle", "target_field": "notes"},
    {"source_column": "monto", "target_field": "amount"},
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada operación paga reintentos de kombu (fail-safe)."""


@pytest.fixture
def habilitado(monkeypatch: pytest.MonkeyPatch, sample_tenant: Tenant) -> None:
    """Prende la compuerta para ESTE tenant."""
    import app.config.async_import_rollout as rollout

    monkeypatch.setattr(
        rollout, "async_import_enabled_for", lambda _t: True
    )
    import app.api.v1.ingestion as api

    monkeypatch.setattr(api, "async_import_enabled_for", lambda _t: True)


def _libro() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    ws.append(["fecha", "detalle", "monto"])
    ws.append(["2024-03-05", "Vela", "6000"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _archivo(db: AsyncSession, tenant: Tenant) -> UploadedFile:
    contenido = _libro()
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="gastos.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/gastos.xlsx",
        content_type=_XLSX,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX, "gastos.xlsx"),
    )
    db.add(record)
    await db.commit()
    return record


def _cuerpo(clave: str = "clave-de-prueba-1", **extra: Any) -> dict[str, Any]:
    return {
        "request_key": clave,
        "column_mappings": _MAPEO,
        "confirmed_fields": {"gastos": True},
        **extra,
    }


# ── La compuerta ─────────────────────────────────────────────────────────────
async def test_sin_la_compuerta_prendida_la_ruta_no_existe(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Default: nadie habilitado, y el frontend sigue con `/confirm`."""
    record = await _archivo(db_session, sample_tenant)
    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert respuesta.status_code == 404


async def test_apagar_la_compuerta_no_abandona_lo_ya_registrado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    habilitado: None,
) -> None:
    """La razón por la que la compuerta gatea SÓLO el registro.

    Un intento ya registrado tiene su orden commiteada. Si apagar la compuerta
    también apagara al publicador, esa orden no se entregaría nunca y el usuario
    vería una importación "pendiente" que nadie va a ejecutar. Frenar lo nuevo no
    puede significar abandonar lo que ya entró.
    """
    record = await _archivo(db_session, sample_tenant)
    creado = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert creado.status_code == 202
    attempt_id = creado.json()["attempt_id"]

    # Se apaga la compuerta.
    import app.api.v1.ingestion as api

    monkeypatch.setattr(api, "async_import_enabled_for", lambda _t: False)

    # Registrar algo NUEVO ya no se puede...
    rechazado = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(clave="otra-clave-distinta"),
        headers=auth_headers,
    )
    assert rechazado.status_code == 404

    # ...pero lo que ya entró sigue consultable y su orden sigue viva.
    consulta = await client.get(
        f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
    )
    assert consulta.status_code == 200
    db_session.expunge_all()
    ordenes = (
        (
            await db_session.execute(
                select(ImportOutbox).where(ImportOutbox.attempt_id == uuid.UUID(attempt_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(ordenes) == 1, "la orden ya commiteada no puede desaparecer"


# ── El contrato ──────────────────────────────────────────────────────────────
async def test_registrar_devuelve_202_con_un_id_consultable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    record = await _archivo(db_session, sample_tenant)
    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert respuesta.status_code == 202, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "PENDIENTE"
    assert cuerpo["file_id"] == str(record.id)

    consulta = await client.get(
        f"/api/v1/ingestion/imports/{cuerpo['attempt_id']}", headers=auth_headers
    )
    assert consulta.status_code == 200
    assert consulta.json()["attempt_id"] == cuerpo["attempt_id"]


async def test_el_intento_y_su_orden_quedan_escritos_juntos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    record = await _archivo(db_session, sample_tenant)
    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    attempt_id = uuid.UUID(respuesta.json()["attempt_id"])

    db_session.expunge_all()
    intento = (
        await db_session.execute(select(ImportAttempt).where(ImportAttempt.id == attempt_id))
    ).scalar_one()
    orden = (
        await db_session.execute(
            select(ImportOutbox).where(ImportOutbox.attempt_id == attempt_id)
        )
    ).scalar_one()
    assert orden is not None
    # Y la solicitud quedó CONGELADA, con su versión de archivo y de formato.
    assert intento.payload_json["confirmed_fields"] == {"gastos": True}
    assert intento.payload_version == 1
    assert "request_key" not in intento.payload_json, (
        "la clave identifica la petición, no forma parte de lo que se importa: "
        "si entrara al payload, cambiarla cambiaría la huella del contenido"
    )


async def test_repetir_la_misma_peticion_devuelve_el_mismo_intento(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    """El caso del timeout, que es la razón de ser de la clave de petición.

    El cliente no sabe si su POST llegó. Reintentar con la misma clave tiene que
    ser seguro; si creara otra importación, el timeout duplicaría los datos.
    """
    record = await _archivo(db_session, sample_tenant)
    primero = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    segundo = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert primero.status_code == 202
    # 200 y no 202: la segunda no creó nada, y el cliente puede querer saberlo.
    assert segundo.status_code == 200
    assert primero.json()["attempt_id"] == segundo.json()["attempt_id"]

    db_session.expunge_all()
    intentos = (
        (
            await db_session.execute(
                select(ImportAttempt).where(ImportAttempt.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(intentos) == 1


async def test_la_misma_clave_con_otro_contenido_es_conflicto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    """Devolver el intento viejo importaría algo que el usuario no pidió; crear
    uno nuevo rompería la promesa de la clave. Se dice, y se dice cuál es el que
    ya existe."""
    record = await _archivo(db_session, sample_tenant)
    await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    conflicto = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(confirmed_fields={"ventas": True}),
        headers=auth_headers,
    )
    assert conflicto.status_code == 409, conflicto.text
    detalle = conflicto.json()["detail"]
    assert detalle["code"] == "REQUEST_KEY_CONFLICT"
    assert detalle["attempt_id"], "tiene que decir cuál es el intento que ya existe"


# ── La consulta verifica pertenencia ─────────────────────────────────────────
async def test_no_se_puede_consultar_el_intento_de_otro_tenant(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
    habilitado: None,
) -> None:
    """Un id de intento no es un secreto: no puede alcanzar para leer la
    importación de otro negocio.

    Y responde 404, no 403: decir "existe pero no es tuyo" ya filtra que existe.
    """
    record = await _archivo(db_session, second_tenant)
    ajeno = ImportAttempt(
        id=uuid.uuid4(),
        tenant_id=second_tenant.tenant_id,
        file_id=record.id,
        request_key="clave-del-otro-tenant",
        payload_hash="x" * 64,
        payload_json={},
    )
    db_session.add(ajeno)
    await db_session.commit()

    respuesta = await client.get(
        f"/api/v1/ingestion/imports/{ajeno.id}", headers=auth_headers
    )
    assert respuesta.status_code == 404


async def test_un_intento_inexistente_responde_404(
    client: AsyncClient, auth_headers: dict[str, str], habilitado: None
) -> None:
    respuesta = await client.get(
        f"/api/v1/ingestion/imports/{uuid.uuid4()}", headers=auth_headers
    )
    assert respuesta.status_code == 404


async def test_la_consulta_devuelve_el_error_accionable_cuando_fallo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    """Un intento fallado tiene que decir QUÉ pasó y qué hacer, no sólo que falló."""
    record = await _archivo(db_session, sample_tenant)
    fallado = ImportAttempt(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        file_id=record.id,
        request_key="clave-fallada",
        payload_hash="y" * 64,
        payload_json={},
        status="FALLADO",
        error_code="limite_excedido",
        error_detail="El archivo tiene 300.000 filas y el máximo es 200.000.",
    )
    db_session.add(fallado)
    await db_session.commit()

    respuesta = await client.get(
        f"/api/v1/ingestion/imports/{fallado.id}", headers=auth_headers
    )
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "FALLADO"
    assert cuerpo["error_code"] == "limite_excedido"
    assert "200.000" in cuerpo["error_detail"]
