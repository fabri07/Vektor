"""Fase 2 (ASTERIA, 2026-10-05): un archivo que repite EN SU MAYORÍA lo ya
cargado no se guarda sin que la persona lo reconozca.

El aviso de ``import_overlap_service`` llega con los datos ya adentro. Alcanza
para dos compras iguales el mismo día, pero no para un archivo que reorganiza los
Libros Diarios que el negocio ya importó: ahí se duplicaban ~2.500 operaciones y
el aviso aparecía después. Ahora, si la coincidencia es masiva, el confirm se
revierte entero y responde 409 ``POSIBLE_REPETICION``; con
``acepto_posible_repeticion`` entra igual. Sin clave fuerte, coincidir no prueba
duplicado: la decisión es de la persona, nunca se descarta nada solo.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HEADERS = ["fecha", "concepto", "proveedor", "monto"]
_MAPEO = [
    {"source_column": "fecha", "target_field": "expense_date"},
    {"source_column": "concepto", "target_field": "description"},
    {"source_column": "proveedor", "target_field": "supplier_name"},
    {"source_column": "monto", "target_field": "amount"},
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _filas(desde: int, cuantas: int) -> list[list[Any]]:
    return [
        [f"2024-03-{(i % 27) + 1:02d}", f"servicio {i}", "Proveedor Sur", 1000 + i]
        for i in range(desde, desde + cuantas)
    ]


def _libro(filas: list[list[Any]]) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Gastos"
    hoja.append(_HEADERS)
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _subir(
    db: AsyncSession, tenant: Tenant, filas: list[list[Any]], nombre: str
) -> uuid.UUID:
    contenido = _libro(filas)
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=_XLSX_MIME,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, nombre),
    )
    db.add(record)
    await db.commit()
    return record.id


async def _confirmar(
    client: AsyncClient, headers: dict[str, str], file_id: uuid.UUID, **extra: Any
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{file_id}/confirm",
        json={"column_mappings": _MAPEO, "confirmed_fields": {"gastos": True}, **extra},
        headers=headers,
    )


async def _cuantos_gastos(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ExpenseEntry)
                .where(ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None))
            )
        ).scalar_one()
    )


async def test_una_carga_repetida_se_frena_y_no_guarda_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    primero = await _subir(db_session, sample_tenant, _filas(0, 12), "marzo.xlsx")
    assert (await _confirmar(client, auth_headers, primero)).status_code == 200
    assert await _cuantos_gastos(db_session, sample_tenant.tenant_id) == 12

    repetido = await _subir(db_session, sample_tenant, _filas(0, 12), "marzo (copia).xlsx")
    resp = await _confirmar(client, auth_headers, repetido)

    assert resp.status_code == 409, resp.text
    detalle = resp.json()["detail"]
    assert detalle["code"] == "POSIBLE_REPETICION"
    assert detalle["coincidencias"] == 12 and detalle["operaciones"] == 12
    assert "marzo.xlsx" in detalle["message"]
    assert await _cuantos_gastos(db_session, sample_tenant.tenant_id) == 12, (
        "un confirm frenado no puede dejar nada guardado"
    )
    # Y el archivo no quedó trabado: se puede volver a confirmar.
    archivo = await db_session.get(UploadedFile, repetido)
    assert archivo is not None
    await db_session.refresh(archivo)
    assert archivo.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION


async def test_con_la_confirmacion_explicita_entra_y_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    primero = await _subir(db_session, sample_tenant, _filas(0, 12), "marzo.xlsx")
    assert (await _confirmar(client, auth_headers, primero)).status_code == 200
    repetido = await _subir(db_session, sample_tenant, _filas(0, 12), "marzo (copia).xlsx")

    resp = await _confirmar(client, auth_headers, repetido, acepto_posible_repeticion=True)

    assert resp.status_code == 200, resp.text
    assert await _cuantos_gastos(db_session, sample_tenant.tenant_id) == 24
    assert any("coinciden" in w for w in resp.json()["warnings"])


async def test_una_coincidencia_parcial_solo_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Menos de la mitad: puede ser un archivo que se superpone en un borde. Se
    importa y se avisa, como antes."""
    primero = await _subir(db_session, sample_tenant, _filas(0, 12), "marzo.xlsx")
    assert (await _confirmar(client, auth_headers, primero)).status_code == 200
    mezclado = await _subir(
        db_session, sample_tenant, _filas(0, 10) + _filas(100, 20), "marzo-y-mas.xlsx"
    )

    resp = await _confirmar(client, auth_headers, mezclado)

    assert resp.status_code == 200, resp.text
    assert any("coinciden" in w for w in resp.json()["warnings"])
