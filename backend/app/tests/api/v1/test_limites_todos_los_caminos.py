"""E6c-2 — el mismo límite en los cinco caminos que parsean un archivo.

Un tope que vive en un endpoint protege un camino y deja los otros cuatro
abiertos. Los que parsean son: el upload de chat, el upload de ingestión, el
parseo del worker, el reparseo del fallback sincrónico y la relectura. **Los tres
últimos leen de S3 y nunca tocan el chequeo de bytes del endpoint**, o sea que un
límite en HTTP no protegía justo a los procesos que corren sin nadie mirando.

Por eso el control vive en ``parse_uploaded_content`` —el único punto por el que
pasan los cinco— y lo que se afirma acá es que cada camino lo TRADUCE bien: un
rechazo con causa que el usuario puede corregir tiene que verse como rechazo, no
como fallo interno, y no puede dejar el archivo importado a medias.
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

from app.domain.ingestion_limits import LimitesDeArchivo
from app.integrations.s3 import S3Client
from app.persistence.models.file import (
    PROCESSING_STATUS_REJECTED,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _libro_grande(filas: int = 40) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    ws.append(["fecha", "detalle", "monto"])
    for i in range(filas):
        ws.append(["2024-03-05", f"Item {i}", "1000"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _patch_s3(monkeypatch: pytest.MonkeyPatch, contenido: bytes) -> None:
    """S3 falso: el upload no sale a la red y el download devuelve el archivo.

    Mismo patrón que `test_reread_file._patch_s3` — se parchean los métodos de
    `S3Client`, no la clase en el módulo que la importa, porque cada camino la
    instancia por su cuenta.
    """

    async def _upload(self: S3Client, *a: Any, **k: Any) -> str:  # noqa: ARG001
        return "uploads/test/limites.xlsx"

    async def _download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return contenido

    async def _head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"x"', "size": len(contenido), "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(S3Client, "upload", _upload)
    monkeypatch.setattr(S3Client, "upload_to_key", _upload)
    monkeypatch.setattr(S3Client, "download", _download)
    monkeypatch.setattr(S3Client, "head", _head)


@pytest.fixture
def limite_chico(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baja el tope de filas a 5 para que un archivo de prueba lo exceda.

    Se parchea el DEFAULT del parser en vez de fabricar un archivo de 200.001
    filas: el comportamiento que se prueba es el mismo y el test tarda
    milisegundos en vez de minutos.
    """
    import app.application.services.file_parsing as fp

    monkeypatch.setattr(fp, "LIMITES", LimitesDeArchivo(filas=5))


async def test_upload_de_chat_rechaza_con_413_y_dice_el_motivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    limite_chico: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """413 y no 422: el archivo no está mal formado, es más grande de lo que
    Véktor lee. Y el detalle nombra la dimensión que se excedió."""
    _patch_s3(monkeypatch, _libro_grande())
    respuesta = await client.post(
        "/api/v1/files/upload?purpose=chat",
        files={"file": ("gastos.xlsx", _libro_grande(), _XLSX)},
        headers=auth_headers,
    )
    assert respuesta.status_code == 413, respuesta.text
    detalle = respuesta.json()["detail"]
    assert "filas" in detalle, detalle
    # Y dice qué hacer, no sólo que no se pudo.
    assert "Dividilo" in detalle or "dividilo" in detalle


async def test_el_fallback_sincronico_deja_el_archivo_rechazado_no_fallado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    limite_chico: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El cuarto camino. Un límite excedido es un rechazo con causa que el
    usuario puede corregir —dividir el archivo, borrar filas vacías—, no un fallo
    interno: tiene que quedar REJECTED con su motivo, y no FAILED.

    La diferencia importa porque son dos pantallas distintas: FAILED dice "algo
    salió mal" y REJECTED dice qué pasó y qué hacer.
    """
    contenido = _libro_grande()

    _patch_s3(monkeypatch, contenido)

    respuesta = await client.post(
        "/api/v1/ingestion/upload",
        files={"file": ("gastos.xlsx", contenido, _XLSX)},
        headers=auth_headers,
    )
    assert respuesta.status_code in (201, 200), respuesta.text
    file_id = uuid.UUID(respuesta.json()["file_id"])

    db_session.expunge_all()
    record = (
        await db_session.execute(select(UploadedFile).where(UploadedFile.id == file_id))
    ).scalar_one()
    assert record.processing_status == PROCESSING_STATUS_REJECTED, (
        f"quedó en {record.processing_status}: un límite excedido no es un fallo "
        "interno, es un rechazo que el usuario puede resolver"
    )
    assert record.rejection_reason and "filas" in record.rejection_reason


async def test_un_archivo_rechazado_no_deja_ni_una_operacion_cargada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    limite_chico: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La regla que gobierna todos los límites: se lee entero o no se lee.

    Importar las primeras N filas de un archivo que se pasó produce libros que no
    cuadran y que nadie sabe que están incompletos — es peor que no importar.
    """
    contenido = _libro_grande()

    _patch_s3(monkeypatch, contenido)
    await client.post(
        "/api/v1/ingestion/upload",
        files={"file": ("gastos.xlsx", contenido, _XLSX)},
        headers=auth_headers,
    )

    for modelo in (SaleEntry, ExpenseEntry):
        filas = (
            (
                await db_session.execute(
                    select(modelo).where(modelo.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert not filas, f"quedaron {len(filas)} {modelo.__name__} de un archivo rechazado"


async def test_un_archivo_dentro_de_los_limites_sigue_entrando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La compuerta no puede tocar el caso normal: sin el tope bajado, el mismo
    archivo entra como siempre."""
    _patch_s3(monkeypatch, _libro_grande())
    respuesta = await client.post(
        "/api/v1/files/upload?purpose=chat",
        files={"file": ("gastos.xlsx", _libro_grande(), _XLSX)},
        headers=auth_headers,
    )
    assert respuesta.status_code == 201, respuesta.text
