"""E8 — un documento que sólo produce pendientes no es un import vacío.

Foto/PDF/texto NO extraen fecha (F6-A4): cada línea con monto se captura en
«Otros» para que el usuario complete la fecha antes de importar. Ese es el
resultado CORRECTO de ese camino, no un fallback que tape una pérdida.

Pero ``check_nonempty_import`` contaba como insertado sólo ventas, gastos,
productos y maestros, así que el confirm de un documento terminaba en 422 — y el
rollback del savepoint se llevaba puestas las propias capturas. El archivo se
procesaba, el usuario leía "no se importó ninguna fila: mapeá las columnas"
(un documento no tiene columnas) y en la bandeja no quedaba nada.

Lo segundo que cubre: la **identidad de una captura**. Dos renglones idénticos
son dos operaciones legítimas, así que el ancla no puede ser el texto; es la
ocurrencia dentro de la versión del archivo, ``(archivo, contexto, ordinal)``.
Sin ancla, cada vuelta por el importador —la relectura, una re-entrega— dejaba
otra copia del mismo pendiente para clasificar.
"""

from __future__ import annotations

import contextlib
import io
import unittest.mock
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import (
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

#: Dos renglones IDÉNTICOS a propósito: son dos ventas legítimas, y tienen que
#: quedar como dos pendientes distintos. El cuarto no tiene monto — no
#: materializa nada.
_DOCUMENTO = (
    b"Venta Coca $1500\n"
    b"Venta Coca $1500\n"
    b"Venta Agua $800\n"
    b"Gracias por su compra\n"
)
_VACIO = b"Gracias por su compra\nVuelva pronto\n"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _planilla_sin_montos() -> bytes:
    """Una planilla de gastos cuyas filas NO traen monto utilizable.

    E7a-lite las captura en «Otros» en vez de perderlas, así que es el caso donde
    el crédito del documento cambiaría el resultado si no estuviera acotado.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    ws.append(["fecha", "detalle", "monto"])
    ws.append(["2026-03-05", "Compra sin monto", ""])
    ws.append(["2026-03-06", "Otra sin monto", ""])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


async def _documento(
    db: AsyncSession, tenant: Tenant, contenido: bytes = _DOCUMENTO
) -> UploadedFile:
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
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="remito.txt",
        s3_key=f"uploads/test/{uuid.uuid4()}/remito.txt",
        content_type="text/plain",
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(
            contenido, "text/plain", "remito.txt"
        ),
    )
    db.add(record)
    await db.commit()
    return record


async def _pendientes(db: AsyncSession, tenant: Tenant) -> list[UnclassifiedRecord]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )


async def _cuantos(db: AsyncSession, modelo: Any, tenant: Tenant) -> int:
    db.expunge_all()
    total = await db.execute(
        select(func.count())
        .select_from(modelo)
        .where(modelo.tenant_id == tenant.tenant_id)
    )
    return int(total.scalar_one())


async def _confirmar(
    client: AsyncClient, auth_headers: dict[str, str], file_id: Any
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{file_id}/confirm",
        json={"column_mappings": [], "confirmed_fields": {"ventas": True}},
        headers=auth_headers,
    )


@pytest_asyncio.fixture
async def documento(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _documento(db_session, sample_tenant)


class TestElConfirmConservaLoQueCapturo:
    async def test_termina_bien_y_los_pendientes_quedan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
    ) -> None:
        response = await _confirmar(client, auth_headers, documento.id)

        assert response.status_code == 200, response.text
        assert len(await _pendientes(db_session, sample_tenant)) == 3
        # No se importó nada: es lo que el documento NO puede producir todavía.
        assert await _cuantos(db_session, SaleEntry, sample_tenant) == 0
        assert await _cuantos(db_session, ExpenseEntry, sample_tenant) == 0

    async def test_dos_renglones_identicos_son_dos_pendientes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
    ) -> None:
        """El texto no es la identidad: dos veces lo mismo son dos operaciones.

        Anclar en el contenido haría que Véktor descartara la segunda — plata
        real perdida por parecerse a la primera.
        """
        await _confirmar(client, auth_headers, documento.id)

        pendientes = await _pendientes(db_session, sample_tenant)
        lineas = sorted(str((p.row_data or {}).get("linea")) for p in pendientes)
        assert lineas == ["Venta Agua $800", "Venta Coca $1500", "Venta Coca $1500"]
        # Y cada uno tiene su propia referencia de fila: es lo que le permite a la
        # relectura reconocer cuál ya se clasificó a mano.
        refs = {str((p.row_data or {}).get("__row_ref__") or "") for p in pendientes}
        assert len(refs) == 3 and "" not in refs

    async def test_un_documento_sin_un_solo_monto_sigue_siendo_vacio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El rechazo del archivo realmente vacío se conserva.

        Lo que cuenta es lo CAPTURADO, no lo intentado: un documento del que no se
        pudo leer un monto no dejó nada para revisar y no hay por qué decir que se
        procesó.
        """
        vacio = await _documento(db_session, sample_tenant, _VACIO)

        response = await _confirmar(client, auth_headers, vacio.id)

        assert response.status_code == 422, response.text
        assert await _pendientes(db_session, sample_tenant) == []

    async def test_una_planilla_que_no_importo_nada_sigue_rechazandose(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """La excepción es del DOCUMENTO, no de «Otros» en general.

        Esta planilla SÍ captura sus dos filas en «Otros» (no traen monto
        utilizable, E7a-lite), así que es exactamente el caso donde el crédito
        cambiaría el resultado. Ahí «Otros» puede estar tapando un mapeo que
        falló —el motivo por el que `counts["otros"]` nunca contó— y el 422 es lo
        que le permite al usuario reintentar con el mapeo corregido.
        """
        contenido = _planilla_sin_montos()
        planilla = UploadedFile(
            tenant_id=sample_tenant.tenant_id,
            uploaded_by=None,
            original_filename="gastos.xlsx",
            s3_key=f"uploads/test/{uuid.uuid4()}/gastos.xlsx",
            content_type=_XLSX,
            size_bytes=len(contenido),
            purpose="ingestion",
            status="uploaded",
            processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
            parsed_summary_json=parse_uploaded_content(
                contenido, _XLSX, "gastos.xlsx"
            ),
        )
        db_session.add(planilla)
        await db_session.commit()

        response = await client.post(
            f"/api/v1/ingestion/files/{planilla.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "fecha", "target_field": "expense_date"},
                    {"source_column": "detalle", "target_field": "notes"},
                    {"source_column": "monto", "target_field": "amount"},
                ],
                "confirmed_fields": {"gastos": True},
                "context_entity": {"table": "expense"},
            },
            headers=auth_headers,
        )

        assert response.status_code == 422, response.text
        assert await _cuantos(db_session, ExpenseEntry, sample_tenant) == 0


class TestElResultadoDiceLoQuePaso:
    async def test_no_se_presenta_como_importado(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        documento: UploadedFile,
    ) -> None:
        response = await _confirmar(client, auth_headers, documento.id)
        cuerpo = response.json()

        assert "pendientes de revisión" in cuerpo["message"]
        assert "Importados" not in cuerpo["message"]
        # El aviso es el único canal que la pantalla muestra de este resultado.
        assert any(
            "No se importó ninguna venta ni gasto" in w for w in cuerpo["warnings"]
        ), cuerpo["warnings"]


class TestNoSeMultiplicanLosPendientes:
    async def test_la_relectura_no_vuelve_a_capturar_lo_mismo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
    ) -> None:
        """La relectura reprocesa el archivo con ``insert_confirmed_data``.

        Sin ancla, cada relectura dejaba otra copia de cada línea y el usuario
        tenía que clasificar el mismo renglón de nuevo. Con ancla, la segunda
        pasada reconoce las tres ocurrencias y no captura ninguna.
        """
        await _confirmar(client, auth_headers, documento.id)
        assert len(await _pendientes(db_session, sample_tenant)) == 3

        from app.application.services import reread_service
        from app.integrations.s3 import S3Client

        async def _download(_self: S3Client, _key: str) -> bytes:
            return _DOCUMENTO

        async def _head(_self: S3Client, _key: str) -> dict[str, Any]:
            return {
                "etag": '"fake"',
                "size": len(_DOCUMENTO),
                "last_modified": "2026-01-01T00:00:00Z",
            }

        with unittest.mock.patch.multiple(S3Client, download=_download, head=_head):
            await reread_service.apply_reread(
                db_session, documento.id, sample_tenant.tenant_id
            )
        await db_session.commit()

        despues = await _pendientes(db_session, sample_tenant)
        assert len(despues) == 3
        # Y siguen ESPERANDO al usuario: la relectura no los da por resueltos.
        assert {p.status for p in despues} == {"PENDING"}


class TestElRecorridoAsincronico:
    @pytest.fixture
    def habilitado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.api.v1.ingestion as api

        monkeypatch.setattr(api, "async_import_enabled_for", lambda _t: True)

    @pytest.fixture
    def ejecutor_con_la_sesion_del_test(
        self, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
    ) -> None:
        import app.jobs.import_executor_worker as worker

        @contextlib.asynccontextmanager
        async def _factory() -> Any:
            yield db_session

        monkeypatch.setattr(worker, "_sesion_de_worker", lambda: (_factory, None))

    async def test_el_intento_cierra_bien_y_su_resultado_es_consultable(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
        habilitado: None,
        ejecutor_con_la_sesion_del_test: None,
    ) -> None:
        """El mismo archivo por la otra ruta: 202, ejecución, resultado.

        Antes fallaba igual que el sincrónico, pero peor: el 422 llegaba como
        excepción al ejecutor y el intento cerraba FALLADO, así que el usuario
        veía un error sobre un archivo que se había leído entero.
        """
        registro = await client.post(
            f"/api/v1/ingestion/files/{documento.id}/imports",
            json={
                "request_key": "documento-pendientes-1",
                "column_mappings": [],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )
        assert registro.status_code == 202, registro.text
        attempt_id = registro.json()["attempt_id"]

        from app.jobs.import_executor_worker import _ejecutar

        assert str(await _ejecutar(uuid.UUID(attempt_id))) == "completado"

        assert len(await _pendientes(db_session, sample_tenant)) == 3

        db_session.expunge_all()
        estado = (
            await client.get(
                f"/api/v1/ingestion/imports/{attempt_id}", headers=auth_headers
            )
        ).json()
        assert estado["status"] == "COMPLETADO", estado
        assert "pendientes de revisión" in estado["result"]["message"]

    async def test_repetir_la_peticion_devuelve_el_mismo_intento(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
        habilitado: None,
        ejecutor_con_la_sesion_del_test: None,
    ) -> None:
        """Idempotencia de PETICIÓN: el reintento del cliente no abre otro import."""
        cuerpo = {
            "request_key": "documento-pendientes-2",
            "column_mappings": [],
            "confirmed_fields": {"ventas": True},
        }
        primero = await client.post(
            f"/api/v1/ingestion/files/{documento.id}/imports",
            json=cuerpo,
            headers=auth_headers,
        )
        segundo = await client.post(
            f"/api/v1/ingestion/files/{documento.id}/imports",
            json=cuerpo,
            headers=auth_headers,
        )

        assert primero.json()["attempt_id"] == segundo.json()["attempt_id"]

        from app.jobs.import_executor_worker import _ejecutar

        await _ejecutar(uuid.UUID(primero.json()["attempt_id"]))
        assert len(await _pendientes(db_session, sample_tenant)) == 3

    async def test_la_entrega_duplicada_no_duplica_los_pendientes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        documento: UploadedFile,
        habilitado: None,
        ejecutor_con_la_sesion_del_test: None,
    ) -> None:
        """Idempotencia de EJECUCIÓN: el mismo intento entregado dos veces."""
        registro = await client.post(
            f"/api/v1/ingestion/files/{documento.id}/imports",
            json={
                "request_key": "documento-pendientes-3",
                "column_mappings": [],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )
        attempt_id = uuid.UUID(registro.json()["attempt_id"])

        from app.jobs.import_executor_worker import _ejecutar

        assert str(await _ejecutar(attempt_id)) == "completado"
        segunda = str(await _ejecutar(attempt_id))

        assert segunda != "completado", (
            f"la segunda entrega volvió a ejecutar el intento ({segunda})"
        )
        assert len(await _pendientes(db_session, sample_tenant)) == 3
