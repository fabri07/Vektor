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
from sqlalchemy import select
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


async def _gastos(db: AsyncSession, tenant: Tenant) -> list[ExpenseEntry]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant.tenant_id,
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
    assert not await _gastos(db_session, sample_tenant), (
        "registrar no puede importar nada: la ejecución es de otro proceso"
    )

    assert await _ejecutar(attempt_id) == "completado"

    # 1. Los datos entraron.
    gastos = await _gastos(db_session, sample_tenant)
    assert len(gastos) == 3, f"se importaron {len(gastos)} de 3 filas"

    # 2. El intento quedó cerrado con su resultado.
    consulta = await client.get(
        f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
    )
    cuerpo = consulta.json()
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

    assert len(await _gastos(db_session, sample_tenant)) == 3


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

    consulta = await client.get(
        f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
    )
    cuerpo = consulta.json()
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
    assert not await _gastos(db_session, sample_tenant)

    consulta = await client.get(
        f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
    )
    assert consulta.json()["error_code"] == "archivo_borrado"


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
    assert len(await _gastos(db_session, sample_tenant)) == 2


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
    assert not await _gastos(db_session, sample_tenant)
    db_session.expunge_all()
    intento = (
        await db_session.execute(select(ImportAttempt).where(ImportAttempt.id == uuid.UUID(attempt_id)))
    ).scalar_one()
    assert intento.status == "PENDIENTE"

    # Aterriza el worker nuevo.
    assert await _ejecutar(attempt_id) == "completado"
    assert len(await _gastos(db_session, sample_tenant)) == 2
