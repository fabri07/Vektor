"""I3-2 — el ejecutor de importaciones tiene límites de tiempo propios, y cortarse
por tiempo no deja nada trabado.

Antes ``jobs.execute_import`` heredaba los globales de Celery (240 s soft / 300 s
hard). Un import grande no terminaba nunca: a los 300 s el pool mataba el proceso
y el archivo quedaba ``IMPORTING``, el intento ``EJECUTANDO`` y el cupo reservado.

El soft limit puede caer en dos lugares, y cada uno tiene su prueba:

- **Adentro del código del import**: la excepción viaja por la corrutina, la
  compensa ``confirm_file`` y la atrapa el ``except Exception`` del ejecutor.
- **En la espera de I/O** (lo normal contra Neon): la señal interrumpe el
  ``select`` del loop, la excepción sale por ``asyncio.run`` y la corrutina sólo
  ve un ``CancelledError``. Se simula cancelando la tarea —que es lo que hace
  ``asyncio.run`` al desarmarse— y después se corre el cierre que hace
  ``execute_import`` afuera del loop.

Mismo arnés que ``test_importacion_asincronica_e2e``: el ejecutor usa la sesión del
test (ver el encabezado de ese módulo).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.import_attempt import ImportAttempt
from app.persistence.models.tenant import SubscriptionQuotaReservation, Tenant
from app.tests.api.v1.test_importacion_asincronica_e2e import (
    _archivo,
    _cuerpo,
    _gastos,
)


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


@pytest.fixture
def habilitado(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.v1.ingestion as api

    monkeypatch.setattr(api, "async_import_enabled_for", lambda _t: True)


@pytest.fixture
def ejecutor_con_la_sesion_del_test(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Como en el e2e, pero cerrando la sesión como lo hace una real.

    Una ``AsyncSession`` de verdad hace rollback al salir de su ``async with`` con
    una excepción. Sin imitarlo, lo que dejó a medio escribir la tarea cancelada
    seguiría en la transacción del test y el cierre posterior lo commitearía: se
    probaría un mundo que no existe en producción.
    """
    import app.jobs.import_executor_worker as worker

    @contextlib.asynccontextmanager
    async def _factory() -> Any:
        try:
            yield db_session
        except BaseException:
            await db_session.rollback()
            raise

    monkeypatch.setattr(worker, "_sesion_de_worker", lambda: (_factory, None))


async def _registrar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    clave: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Registra un intento por HTTP. Devuelve (attempt_id, file_id, tenant_id)."""
    # Los ids ANTES de cualquier rollback: después los objetos quedan detached.
    tenant_id = tenant.tenant_id
    record = await _archivo(db, tenant)
    file_id = record.id
    registro = await client.post(
        f"/api/v1/ingestion/files/{file_id}/imports",
        json=_cuerpo(clave),
        headers=auth_headers,
    )
    assert registro.status_code == 202, registro.text
    return uuid.UUID(registro.json()["attempt_id"]), file_id, tenant_id


async def _estado(
    db: AsyncSession, attempt_id: uuid.UUID, file_id: uuid.UUID
) -> tuple[ImportAttempt, UploadedFile, SubscriptionQuotaReservation]:
    from app.domain.subscription import import_attempt_operation_id

    db.expunge_all()
    intento = await db.get(ImportAttempt, attempt_id)
    archivo = await db.get(UploadedFile, file_id)
    reserva = (
        await db.execute(
            select(SubscriptionQuotaReservation).where(
                SubscriptionQuotaReservation.operation_id
                == import_attempt_operation_id(attempt_id)
            )
        )
    ).scalar_one()
    assert intento is not None and archivo is not None
    return intento, archivo, reserva


def _afirmar_nada_trabado(
    intento: ImportAttempt, archivo: UploadedFile, reserva: SubscriptionQuotaReservation
) -> None:
    assert archivo.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION, (
        f"el archivo quedó en {archivo.processing_status}: trabado en 'importando'"
    )
    assert archivo.import_attempt_id is None
    assert intento.status == "FALLADO", f"el intento quedó en {intento.status}"
    assert intento.error_code == "tiempo_excedido", intento.error_code
    assert intento.error_detail and "tiempo máximo" in intento.error_detail
    assert reserva.state == "RELEASED", (
        f"la reserva de cupo quedó {reserva.state}: un import cortado no consume"
    )


# ── Configuración ────────────────────────────────────────────────────────────
def test_execute_import_tiene_limites_propios_debajo_de_los_leases() -> None:
    """Los límites no son los globales, y el hard queda debajo de los dos leases.

    Un hard limit por encima del lease deja que la recuperación reencole el intento
    —o que un `/confirm` tome el archivo— mientras el primer ejecutor sigue
    escribiendo: un timeout convertido en dos imports a la vez.
    """
    from app.application.services.import_attempt_service import LEASE_TTL_SEGUNDOS
    from app.application.services.ingestion_lease_service import (
        DEFAULT_IMPORT_LEASE_TTL_SECONDS,
    )
    from app.config.settings import Settings
    from app.jobs.celery_app import celery_app
    from app.jobs.import_executor_worker import execute_import

    soft = execute_import.soft_time_limit
    hard = execute_import.time_limit
    assert soft is not None and hard is not None, "sin límites propios: hereda 240/300 s"
    assert (soft, hard) != (
        celery_app.conf.task_soft_time_limit,
        celery_app.conf.task_time_limit,
    )
    assert soft < hard
    # La ventana soft → hard es la que tiene el cierre para revertir y avisar.
    assert hard - soft >= 2 * 60
    assert hard < LEASE_TTL_SEGUNDOS
    assert hard < DEFAULT_IMPORT_LEASE_TTL_SECONDS
    assert hard < Settings.model_fields["INGESTION_IMPORT_LEASE_TTL_SECONDS"].default


# ── El soft limit adentro del import ─────────────────────────────────────────
async def test_un_soft_limit_adentro_del_import_cierra_el_intento_y_libera_el_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La señal cae con el lease tomado y las filas ya escritas, al publicar."""
    import app.api.v1.ingestion as api
    from app.jobs.import_executor_worker import _ejecutar

    attempt_id, file_id, tenant_id = await _registrar(
        client, auth_headers, db_session, sample_tenant, "tiempo-adentro-1"
    )

    async def _cortar(*_a: Any, **_kw: Any) -> None:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(api, "finalize_import_lease", _cortar)

    assert await _ejecutar(attempt_id) == "fallado"

    _afirmar_nada_trabado(*await _estado(db_session, attempt_id, file_id))
    assert not await _gastos(db_session, tenant_id), "un import cortado dejó filas"


# ── El soft limit en la espera de I/O ────────────────────────────────────────
async def test_un_soft_limit_en_la_espera_de_io_se_cierra_afuera_del_loop(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El corte cae DESPUÉS de `confirm_file`, esperando el cierre del intento.

    Es el peor lugar: `confirm_file` ya devolvió, así que su compensación no corre,
    y el `except Exception` del ejecutor no ve un `CancelledError`. Antes de I3-2
    el archivo quedaba `IMPORTING` y el intento `EJECUTANDO`.
    """
    from app.application.services import import_attempt_service as svc
    from app.jobs.import_executor_worker import _cerrar_por_tiempo, _ejecutar

    attempt_id, file_id, tenant_id = await _registrar(
        client, auth_headers, db_session, sample_tenant, "tiempo-io-1"
    )

    _real_cerrar = svc.cerrar_intento
    entro = asyncio.Event()
    _esperas = {"pendientes": 1}

    async def _colgarse(*a: Any, **kw: Any) -> bool:
        # Sólo la primera vez (la del ejecutor): el cierre por tiempo tiene que
        # cerrar de verdad.
        if _esperas["pendientes"]:
            _esperas["pendientes"] -= 1
            entro.set()
            await asyncio.Event().wait()
        return bool(await _real_cerrar(*a, **kw))

    monkeypatch.setattr(svc, "cerrar_intento", _colgarse)

    contexto: dict[str, Any] = {}
    tarea = asyncio.create_task(_ejecutar(attempt_id, contexto))
    await entro.wait()
    # Lo que hace `asyncio.run` cuando la señal sale por el `select` del loop.
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert await _cerrar_por_tiempo(attempt_id, contexto) == "fallado"

    _afirmar_nada_trabado(*await _estado(db_session, attempt_id, file_id))
    assert not await _gastos(db_session, tenant_id), "un import cortado dejó filas"


def test_execute_import_atrapa_el_soft_limit_que_sale_por_asyncio_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La tarea de Celery cierra por tiempo con lo que el ejecutor llegó a saber."""
    import app.jobs.import_executor_worker as worker

    attempt_id = uuid.uuid4()
    token = uuid.uuid4()
    vistos: list[dict[str, Any]] = []

    async def _ejecutar_cortado(_aid: uuid.UUID, contexto: dict[str, Any]) -> str:
        contexto["token"] = token
        raise SoftTimeLimitExceeded()

    async def _cierre(aid: uuid.UUID, contexto: dict[str, Any]) -> str:
        assert aid == attempt_id
        vistos.append(dict(contexto))
        return "fallado"

    monkeypatch.setattr(worker, "_ejecutar", _ejecutar_cortado)
    monkeypatch.setattr(worker, "_cerrar_por_tiempo", _cierre)

    assert worker.execute_import.run(str(attempt_id)) == {"resultado": "fallado"}
    assert vistos == [{"token": token}]


async def test_el_cierre_por_tiempo_antes_de_reclamar_no_toca_nada() -> None:
    """Sin token no hay intento nuestro: no se abre ni una sesión."""
    from app.jobs.import_executor_worker import _cerrar_por_tiempo

    assert await _cerrar_por_tiempo(uuid.uuid4(), {}) == "tiempo_sin_reclamo"
