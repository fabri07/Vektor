"""E6c-3 — el recorrido completo: registrar, ejecutar, consultar el resultado.

Qué prueba este archivo y qué NO
---------------------------------
Prueba el **recorrido de datos y estados**: que registrar por la ruta nueva
termine con las filas realmente importadas, el intento en COMPLETADO, y el mismo
resultado que habría devuelto `/confirm` disponible en la consulta.

No prueba el transporte. El ejecutor se invoca en proceso, sin broker: lo que
Celery aporta —que el mensaje llegue, y que a veces llegue dos veces— ya está
cubierto por las pruebas de entrega duplicada y de caída, que corren contra
Postgres real porque son sobre transacciones concurrentes. Levantar un broker acá
agregaría infraestructura sin agregar una afirmación nueva.

Por qué el ejecutor usa la sesión del test
-------------------------------------------
En producción el worker abre su propio engine: corre en otro proceso. En la suite
el engine es SQLite en memoria, y cada conexión nueva es una base **distinta** —
un ejecutor con engine propio vería una base vacía y el test pasaría por la razón
equivocada (o fallaría por una que no existe en producción). Se le da la sesión
del test para que mire los mismos datos; lo que eso deja afuera —dos conexiones
reales compitiendo— es justamente lo que los tests de Postgres cubren.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.import_attempt import ImportAttempt, ImportOutbox
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAPEO = [
    {"source_column": "fecha", "target_field": "expense_date"},
    {"source_column": "detalle", "target_field": "notes"},
    {"source_column": "monto", "target_field": "amount"},
]


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
    """El ejecutor mira la MISMA base que el test. Ver el encabezado del módulo."""
    import contextlib

    import app.jobs.import_executor_worker as worker

    @contextlib.asynccontextmanager
    async def _factory() -> Any:
        yield db_session

    monkeypatch.setattr(worker, "_sesion_de_worker", lambda: (_factory, None))


def _libro(filas: int = 3) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    ws.append(["fecha", "detalle", "monto"])
    for i in range(filas):
        ws.append(["2024-03-05", f"Compra {i}", "1000"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _archivo(db: AsyncSession, tenant: Tenant, filas: int = 3) -> UploadedFile:
    if not (
        await db.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == tenant.tenant_id)
        )
    ).scalar_one_or_none():
        db.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                vertical_code="kiosco_almacen",
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=True,
            )
        )
    contenido = _libro(filas)
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


def _cuerpo(clave: str = "recorrido-completo-1") -> dict[str, Any]:
    return {
        "request_key": clave,
        "column_mappings": _MAPEO,
        "confirmed_fields": {"gastos": True},
    }


async def _ejecutar(attempt_id: str) -> str:
    from app.jobs.import_executor_worker import _ejecutar as ejecutar

    return str(await ejecutar(uuid.UUID(attempt_id)))


async def _consultar(
    client: AsyncClient, auth_headers: dict[str, str], db: AsyncSession, attempt_id: str
) -> dict[str, Any]:
    """El estado por HTTP, sin leer un objeto viejo de la identity map.

    La sesión del test es la MISMA que usa el endpoint y tiene
    ``expire_on_commit=False``: un ``ImportAttempt`` cargado antes del reclamo
    conserva sus atributos viejos aunque el SELECT del endpoint vuelva a emitirse,
    y la consulta devuelve el estado anterior. Es un artefacto del arnés —en
    producción el endpoint abre su propia sesión— pero hace flakear cualquier
    aserción de estado posterior a una ejecución. Mismo motivo por el que
    ``_gastos`` expunge.
    """
    db.expunge_all()
    respuesta = await client.get(
        f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
    )
    return dict(respuesta.json())


async def _gastos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


# ── El recorrido ─────────────────────────────────────────────────────────────
async def test_registrar_ejecutar_y_consultar_el_resultado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """De punta a punta: el 202, la ejecución, los datos y el resultado.

    Es la prueba que hace que E6c cierre. Las tablas y los endpoints ya existían;
    lo que faltaba era demostrar que el recorrido entero produce los datos.
    """
    record = await _archivo(db_session, sample_tenant, filas=3)

    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert registro.status_code == 202, registro.text
    attempt_id = registro.json()["attempt_id"]
    assert registro.json()["status"] == "PENDIENTE"
    assert not await _gastos(db_session, sample_tenant.tenant_id), (
        "registrar no puede importar nada: la ejecución es de otro proceso"
    )

    assert await _ejecutar(attempt_id) == "completado"

    # 1. Los datos entraron.
    gastos = await _gastos(db_session, sample_tenant.tenant_id)
    assert len(gastos) == 3, f"se importaron {len(gastos)} de 3 filas"

    # 2. El intento quedó cerrado con su resultado.
    cuerpo = await _consultar(client, auth_headers, db_session, attempt_id)
    assert cuerpo["status"] == "COMPLETADO"
    assert cuerpo["error_code"] is None
    assert cuerpo["finished_at"] is not None

    # 3. Y el resultado es el MISMO cuerpo que devuelve `/confirm`, para que la
    #    pantalla no tenga que saber por qué ruta entró la importación.
    assert cuerpo["result"] is not None
    assert cuerpo["result"]["file_id"] == str(record.id)
    assert "warnings" in cuerpo["result"]


async def test_ejecutar_dos_veces_no_importa_dos_veces(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """La entrega duplicada, extremo a extremo y con datos reales.

    Los tests de Postgres afirman que la segunda entrega no consigue el token.
    Este afirma la consecuencia que le importa al negocio: no hay seis gastos.
    """
    record = await _archivo(db_session, sample_tenant, filas=3)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    assert await _ejecutar(attempt_id) == "completado"
    assert await _ejecutar(attempt_id) == "duplicado"

    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 3


async def test_un_archivo_borrado_antes_de_ejecutar_falla_con_motivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """Entre registrar y ejecutar puede pasar cualquier cosa. Lo que no puede
    pasar es que el intento quede colgado sin decir qué ocurrió."""
    from datetime import UTC, datetime

    record = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    record.deleted_at = datetime.now(UTC)
    await db_session.commit()

    assert await _ejecutar(attempt_id) == "fallado"

    cuerpo = await _consultar(client, auth_headers, db_session, attempt_id)
    assert cuerpo["status"] == "FALLADO"
    assert cuerpo["error_code"] == "archivo_borrado"
    assert "volvé a subirlo" in (cuerpo["error_detail"] or "").lower()


async def test_un_archivo_que_cambio_desde_el_confirm_no_se_importa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """El snapshot del archivo, que es la mitad menos obvia del congelamiento.

    Si una relectura corre entre el confirm y la ejecución, el contenido
    interpretado del archivo cambia. Importar contra el nuevo sería importar algo
    que el usuario **nunca vio** en la pantalla donde dijo que sí.
    """
    record = await _archivo(db_session, sample_tenant)
    record.content_hash = "hash-del-momento-del-confirm"
    await db_session.commit()

    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    # Una relectura reemplaza el contenido del archivo.
    record.content_hash = "otro-hash-despues-de-la-relectura"
    await db_session.commit()

    assert await _ejecutar(attempt_id) == "fallado"
    assert not await _gastos(db_session, sample_tenant.tenant_id)

    _estado = await _consultar(client, auth_headers, db_session, attempt_id)
    assert _estado["error_code"] == "archivo_borrado"


async def test_el_progreso_es_consultable_mientras_corre(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    """Lo que la pantalla muestra mientras espera.

    Sin ejecutar: el intento tiene que decir cuántas filas son desde el registro,
    porque es lo que permite mostrar una barra con sentido en vez de un spinner
    indefinido.
    """
    record = await _archivo(db_session, sample_tenant, filas=7)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    cuerpo = registro.json()
    assert cuerpo["rows_total"] == 7, "el total sale del summary del archivo"
    assert cuerpo["rows_done"] == 0


# ── Coexistencia de versiones ────────────────────────────────────────────────
async def test_confirm_sigue_funcionando_con_la_compuerta_prendida(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
) -> None:
    """Un frontend VIEJO contra un backend nuevo.

    Durante el deploy hay clientes en vuelo, y Railway redespliega api y worker en
    paralelo sin orden garantizado. Habilitar la ruta nueva no puede romper a
    quien todavía usa la vieja.
    """
    record = await _archivo(db_session, sample_tenant, filas=2)
    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={"column_mappings": _MAPEO, "confirmed_fields": {"gastos": True}},
        headers=auth_headers,
    )
    assert respuesta.status_code == 200, respuesta.text
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2


async def test_con_la_compuerta_apagada_no_queda_nada_registrado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un frontend NUEVO contra un backend con la compuerta apagada.

    El 404 tiene que ser una respuesta limpia: si dejara un intento a medias, el
    frontend caería a `/confirm` —que es lo correcto ante un 404— y el archivo se
    importaría además por el intento huérfano.
    """
    record = await _archivo(db_session, sample_tenant)
    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    assert respuesta.status_code == 404

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
    ordenes = (
        (
            await db_session.execute(
                select(ImportOutbox).where(ImportOutbox.tenant_id == sample_tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert not intentos and not ordenes, (
        "un 404 dejó rastro: el frontend va a usar `/confirm` y este intento "
        "huérfano importaría el archivo una segunda vez"
    )


async def test_un_worker_viejo_no_pierde_la_orden(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """El orden de deploy que Railway no garantiza: api nueva, worker viejo.

    La api registra intentos que ningún worker sabe ejecutar todavía. La orden
    queda en la tabla —no se pierde— y en cuanto el worker nuevo aterriza, se
    drena. Es la propiedad que hace que el despliegue no tenga que ser ordenado.
    """
    record = await _archivo(db_session, sample_tenant, filas=2)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo(),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    # El "worker viejo" no consume nada: pasa el tiempo y no se importó nada.
    assert not await _gastos(db_session, sample_tenant.tenant_id)
    db_session.expunge_all()
    intento = (
        await db_session.execute(
            select(ImportAttempt).where(ImportAttempt.id == uuid.UUID(attempt_id))
        )
    ).scalar_one()
    assert intento.status == "PENDIENTE"

    # Aterriza el worker nuevo.
    assert await _ejecutar(attempt_id) == "completado"
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2


# ── Capacidades efectivas (E7a-lite) ─────────────────────────────────────────
async def test_una_compuerta_que_cambia_entre_registro_y_ejecucion_no_importa_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La mitad del congelamiento que faltaba: el ENTORNO, no sólo el archivo.

    Las compuertas de rollout se leen al importar, y el que importa no es el
    proceso que armó el preview: la api y el worker son dos servicios con
    entornos propios que Railway redespliega en paralelo. Sin esta verificación,
    alguien confirma viendo el motor de costos de compra encendido y el worker
    escribe los costos con el motor apagado — números distintos de los que mostró
    la pantalla, sin un error a la vista.

    Lo que se afirma acá es que **no se escribió nada** y que el motivo es
    accionable, no que el intento "falló".
    """
    # Se engancha la COMPUERTA REAL y no `capacidades_efectivas`: así el snapshot
    # se arma con los nombres de verdad, y el mismo hook vale para el registro y
    # para la ejecución (`capacidades_efectivas` la resuelve en cada llamada).
    import app.config.purchase_cost_rollout as rollout

    record = await _archivo(db_session, sample_tenant)

    # Se confirma con el motor de costos de compra ENCENDIDO.
    monkeypatch.setattr(rollout, "purchase_cost_enabled_for", lambda _t: True)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo("capacidades-1"),
        headers=auth_headers,
    )
    assert registro.status_code == 202
    attempt_id = registro.json()["attempt_id"]

    # Entre el registro y la ejecución, alguien la apaga (o el worker corre con
    # otro entorno, que desde acá es indistinguible y tiene la misma consecuencia).
    monkeypatch.setattr(rollout, "purchase_cost_enabled_for", lambda _t: False)

    assert await _ejecutar(attempt_id) == "fallado"
    # Lo que importa: NO se escribió nada.
    assert not await _gastos(db_session, sample_tenant.tenant_id)

    cuerpo = await _consultar(client, auth_headers, db_session, attempt_id)
    assert cuerpo["status"] == "FALLADO"
    assert cuerpo["error_code"] == "capacidades_cambiaron"
    detalle = (cuerpo["error_detail"] or "").lower()
    # El detalle nombra la compuerta (para el operador) y dice qué hacer (para el
    # usuario). Un "configuración cambiada" a secas no sirve para ninguno de los dos.
    assert "purchase_cost_rollout_tenant_ids" in detalle
    assert "volvé a confirmar" in detalle


async def test_un_intento_sin_snapshot_de_capacidades_se_ejecuta_igual(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """Los intentos registrados antes de que la columna existiera.

    Quedan con ``NULL``, que significa "no hay con qué comparar". Hacerlos fallar
    rompería en el deploy justamente los intentos en vuelo que esta ruta existe
    para no perder: la verificación tiene que ser una mejora para los nuevos, no
    una trampa para los que ya estaban.
    """
    record = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo("capacidades-sin-snapshot"),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    # Se simula el intento viejo: sin snapshot.
    intento = await db_session.get(ImportAttempt, uuid.UUID(attempt_id))
    assert intento is not None
    intento.capabilities_json = None
    await db_session.commit()

    assert await _ejecutar(attempt_id) == "completado"
    assert await _gastos(db_session, sample_tenant.tenant_id)


# ── Revisión externa (2026-09-10): los cinco agujeros del recorrido ──────────
#
# Estas cinco pruebas vienen de una revisión externa que reprodujo cada caso antes
# de que existiera el arreglo. Se adoptan tal cual el hecho que afirman —no el
# mecanismo— porque son la única compuerta de que el recorrido asíncrono no vuelva
# a tener estos agujeros, y los cinco eran invisibles para la suite que había:
# cuatro de ellos pasaban por caminos que ninguna prueba cruzaba (relectura↔import,
# broker caído durante la recuperación, caída entre efectos y cierre, dos archivos
# con la misma clave) y el quinto sólo se ve con concurrencia real.


async def test_una_relectura_posterior_al_confirm_no_se_importa_en_silencio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
) -> None:
    """Registrar $3.000 y que se persistan $27.000 era alcanzable.

    El intento congelaba `content_hash`, que es el sha256 de los **bytes**. Una
    relectura no toca los bytes: reescribe `parsed_summary_json` y sube
    `ingestion_version`. O sea que el guard que decía proteger de una relectura era
    exactamente ciego a ella, y el ejecutor leía el resumen mutable del archivo.
    """
    import copy

    record = await _archivo(db_session, sample_tenant)
    record.content_hash = "hash-que-no-cambia-con-una-relectura"
    record.latest_preview_version = 1
    await db_session.commit()

    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo("revision-1"),
        headers=auth_headers,
    )
    assert registro.status_code == 202
    attempt_id = registro.json()["attempt_id"]

    # Una relectura reinterpreta el MISMO archivo: otros montos, mismo hash.
    def _reinterpretar(valor: Any) -> Any:
        if isinstance(valor, dict):
            return {k: _reinterpretar(v) for k, v in valor.items()}
        if isinstance(valor, list):
            return [_reinterpretar(v) for v in valor]
        if valor in ("1000", 1000):
            return "9000"
        return valor

    nuevo = _reinterpretar(copy.deepcopy(record.parsed_summary_json))
    assert nuevo != record.parsed_summary_json
    record.parsed_summary_json = nuevo
    record.ingestion_version = record.ingestion_version + 1
    record.latest_preview_version = 2
    await db_session.commit()

    assert await _ejecutar(attempt_id) == "fallado"
    # Lo que importa: no entró NADA. Ni lo viejo ni lo nuevo.
    assert not await _gastos(db_session, sample_tenant.tenant_id)

    cuerpo = await _consultar(client, auth_headers, db_session, attempt_id)
    assert cuerpo["error_code"] == "revision_cambiada"
    detalle = (cuerpo["error_detail"] or "").lower()
    # El archivo está perfecto: mandarlo a re-subirlo sería mandarlo a arreglar algo
    # que no está roto.
    assert "volvé a subirlo" not in detalle
    assert "confirmá de nuevo" in detalle


async def test_no_se_puede_releer_un_archivo_que_se_esta_importando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La otra mitad: sin esto, verificar la revisión sólo angosta la ventana.

    `reread/apply` no miraba `processing_status` en absoluto, así que podía
    reescribir la interpretación del archivo mientras un import la estaba leyendo.
    """
    from datetime import UTC, datetime

    from app.persistence.models.file import PROCESSING_STATUS_IMPORTING

    record = await _archivo(db_session, sample_tenant)
    record.processing_status = PROCESSING_STATUS_IMPORTING
    record.import_started_at = datetime.now(UTC)
    await db_session.commit()

    respuesta = await client.post(
        f"/api/v1/ingestion/files/{record.id}/reread/apply",
        json={"run_id": str(uuid.uuid4()), "draft_version": 1},
        headers=auth_headers,
    )
    assert respuesta.status_code == 409, respuesta.text
    assert "se está importando" in respuesta.text


async def test_la_recuperacion_no_pierde_la_orden_si_el_broker_esta_caido(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un broker caído es un motivo HABITUAL de que haya huérfanos que recuperar.

    La recuperación commiteaba el `PENDIENTE` y después llamaba al broker. Si esa
    llamada fallaba, la orden original seguía marcada como publicada y no quedaba
    ninguna pendiente: el intento quedaba invisible para el publicador y sin
    ejecución para siempre. O sea que la recuperación reabría la ventana que el
    outbox existe para cerrar, y en el peor momento.
    """
    from datetime import UTC, datetime, timedelta

    from app.application.services import import_attempt_service as svc
    from app.jobs import import_executor_worker as worker

    _ = worker  # el recuperador se llama abajo por el módulo
    record = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo("recuperacion-broker-1"),
        headers=auth_headers,
    )
    attempt_id = uuid.UUID(registro.json()["attempt_id"])

    # La orden YA se entregó: es lo que pasa siempre que un ejecutor llegó a tomar
    # el intento. Sin esto el test es vacuo —la orden sigue pendiente por sí sola y
    # pasa con el fix y sin él—, y de hecho así sobrevivió a la primera mutación.
    await svc.marcar_publicada_del_intento(db_session, attempt_id)
    # Un ejecutor lo tomó y murió: lease vencido.
    await svc.reclamar_intento(db_session, attempt_id)
    await db_session.execute(
        update(ImportAttempt)
        .where(ImportAttempt.id == attempt_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()

    db_session.expunge_all()
    assert not [
        o for o in await svc.ordenes_pendientes(db_session) if o.attempt_id == attempt_id
    ], "la orden tenía que estar entregada antes de recuperar"

    def _broker_caido(*_a: Any, **_kw: Any) -> None:
        raise ConnectionError("broker unavailable")

    from app.jobs.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", _broker_caido)

    # La recuperación NO habla con el broker, así que ni se entera de que está caído.
    assert await worker._recuperar() == 1

    db_session.expunge_all()
    pendientes = await svc.ordenes_pendientes(db_session)
    assert any(o.attempt_id == attempt_id for o in pendientes), (
        "La recuperación dejó un PENDIENTE sin orden pendiente: nadie lo va a ejecutar."
    )


async def test_una_caida_entre_los_efectos_y_el_cierre_no_existe(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    habilitado: None,
    ejecutor_con_la_sesion_del_test: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Los efectos y el resultado commitean juntos, así que no hay "en el medio".

    Antes el cierre iba en otra sesión DESPUÉS del commit: una caída en el medio
    dejaba los tres gastos escritos y el intento en `EJECUTANDO`. La recuperación
    lo reencolaba, el confirm rechazaba el archivo —ya estaba en `DONE`— y el
    intento terminaba **FALLADO informando fracaso sobre una importación que había
    funcionado**, sin forma de recuperar su resultado.

    Se inyecta la caída en el cierre: si los efectos estuvieran en otra transacción
    sobrevivirían. La prueba es que NO sobreviven.
    """
    from app.application.services import import_attempt_service as svc

    # El id ANTES de cualquier rollback: después el Tenant queda detached y leerle
    # un atributo revienta con DetachedInstanceError.
    _tid = sample_tenant.tenant_id
    record = await _archivo(db_session, sample_tenant)
    registro = await client.post(
        f"/api/v1/ingestion/files/{record.id}/imports",
        json=_cuerpo("caida-cierre-1"),
        headers=auth_headers,
    )
    attempt_id = registro.json()["attempt_id"]

    _real_cerrar = svc.cerrar_intento
    _caidas = {"pendientes": 1}

    async def _caer(*a: Any, **kw: Any) -> bool:
        # Sólo la PRIMERA vez: después tiene que cerrar de verdad para poder afirmar
        # que la recuperación termina bien. No se usa `monkeypatch.undo()` porque
        # desharía también el patch del fixture que le da al ejecutor la sesión del
        # test, y el ejecutor abriría una conexión real.
        if _caidas["pendientes"]:
            _caidas["pendientes"] -= 1
            raise RuntimeError("caída justo antes de cerrar el intento")
        return bool(await _real_cerrar(*a, **kw))

    # Se parchea en el SERVICIO y no en el worker: `_ejecutar` lo importa adentro
    # de la función, así que el nombre se resuelve en cada llamada.
    monkeypatch.setattr(svc, "cerrar_intento", _caer)
    # El cierre falla, y el ejecutor lo trata como lo que es: el import no terminó.
    # Antes esto era imposible de tratar así, porque los efectos YA estaban
    # commiteados cuando llegaba el cierre.
    assert await _ejecutar(attempt_id) == "fallado"

    # Lo que distingue el antes del después: los efectos NO sobrevivieron. Estaban
    # en la misma transacción que el cierre, así que el rollback se los llevó. Con
    # el commit previo al cierre, acá habría tres gastos escritos y un intento que
    # informa fracaso.
    assert not await _gastos(db_session, _tid), (
        "Los efectos sobrevivieron a un cierre fallido: están en otra transacción "
        "que el resultado, y el intento informa fracaso sobre datos que entraron."
    )

    # Y un intento que SÍ quedó interrumpido (EJECUTANDO, lease vencido) se
    # recupera terminando BIEN y con su resultado — no "FALLADO" sobre un import que
    # funcionó, que es lo que pasaba antes.
    db_session.expunge_all()
    from datetime import UTC, datetime, timedelta

    await db_session.execute(
        update(ImportAttempt)
        .where(ImportAttempt.id == uuid.UUID(attempt_id))
        .values(
            status="EJECUTANDO",
            # Con token y con lease VENCIDO: es lo que deja un ejecutor que murió, y
            # las tres condiciones que `liberar_huerfanos` exige para tocarlo.
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()
    await svc.liberar_huerfanos(db_session)
    await db_session.commit()

    assert await _ejecutar(attempt_id) == "completado"
    assert len(await _gastos(db_session, _tid)) == 3
    cuerpo = await _consultar(client, auth_headers, db_session, attempt_id)
    assert cuerpo["status"] == "COMPLETADO"
    assert cuerpo["result"] is not None


async def test_la_misma_clave_para_otro_archivo_es_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos archivos, misma clave, mismos mapeos: el segundo recibía el intento del
    PRIMERO.

    El `file_id` no está en el cuerpo —es un parámetro de la ruta— así que la huella
    del payload no los distinguía. El cliente creía haber registrado su importación
    y ese archivo no se importaba nunca: no es un duplicado, es una pérdida
    silenciosa.
    """
    from app.application.services import import_attempt_service as svc

    primero = await _archivo(db_session, sample_tenant)
    segundo = await _archivo(db_session, sample_tenant)
    payload: dict[str, Any] = {"confirmed_fields": {"gastos": True}}

    await svc.registrar_intento(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        file_id=primero.id,
        request_key="la-misma-clave",
        payload=payload,
    )
    await db_session.commit()

    with pytest.raises(svc.SolicitudEnConflictoError):
        await svc.registrar_intento(
            db_session,
            tenant_id=sample_tenant.tenant_id,
            file_id=segundo.id,
            request_key="la-misma-clave",
            payload=payload,
        )
