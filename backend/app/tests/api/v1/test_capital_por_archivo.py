"""Una hoja de capital de socios («Aportes de socios») se importa como CAPITAL, no como
gasto ni compra de mercadería; lo ilegible va a «Otros» con su motivo; la misma fila no
se carga dos veces y borrar el archivo anula el movimiento (no lo deja huérfano)."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

pytestmark = pytest.mark.asyncio

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CONTEXTO = "sheet:Aportes de socios"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga reintentos de kombu."""


def _libro(filas: list[list[Any]]) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Aportes de socios"
    hoja.append(["TRAMPA: capital, no venta."])
    hoja.append(["Fecha", "Socio", "Concepto", "Forma", "Monto ARS"])
    for fila in filas:
        hoja.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _subir(db: AsyncSession, tenant: Tenant, contenido: bytes) -> UploadedFile:
    archivo = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="aportes.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/aportes.xlsx",
        content_type=_XLSX_MIME,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, "aportes.xlsx"),
    )
    db.add(archivo)
    await db.commit()
    return archivo


async def _confirmar(client: AsyncClient, h: dict[str, str], archivo: UploadedFile) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={"confirmed_fields": {}, "context_confirmed": {_CONTEXTO: True}},
        headers=h,
    )


async def _movimientos(db: AsyncSession, tenant: Tenant) -> list[OwnerCapitalMovement]:
    return list(
        (
            await db.execute(
                select(OwnerCapitalMovement)
                .where(OwnerCapitalMovement.tenant_id == tenant.tenant_id)
                # Estado actual de la base, no el de la identity map de la sesión.
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


FILA_OK = ["2026-10-20", "marco f.", "Reposición de caja", "Efectivo", 300000]


async def test_la_hoja_de_aportes_entra_como_capital_y_no_como_gasto(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _subir(db_session, sample_tenant, _libro([FILA_OK]))

    r = await _confirmar(client, auth_headers, archivo)

    assert r.status_code == 200, r.text
    (mov,) = await _movimientos(db_session, sample_tenant)
    assert (mov.kind, mov.amount_ars, mov.payment_method, mov.partner_name) == (
        "aporte", Decimal("300000.00"), "cash", "marco f."
    )
    assert (mov.source_type, mov.source_upload_id) == ("archivo", archivo.id)
    assert mov.notes == "Reposición de caja"
    gastos = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert gastos == []
    otros = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalars().all()
    assert otros == []  # ya no termina como «compra de mercadería sin artículos»


async def test_la_misma_hoja_vuelta_a_subir_no_duplica_el_aporte(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    contenido = _libro([FILA_OK])
    primero = await _subir(db_session, sample_tenant, contenido)
    assert (await _confirmar(client, auth_headers, primero)).status_code == 200
    segundo = await _subir(db_session, sample_tenant, contenido)

    r = await _confirmar(client, auth_headers, segundo)

    assert r.status_code == 200, r.text
    assert len(await _movimientos(db_session, sample_tenant)) == 1


async def test_lo_que_no_se_puede_leer_va_a_otros_con_su_motivo(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                FILA_OK,
                ["no es fecha", "marco f.", "Aporte", "Efectivo", 1000],
                ["2026-10-21", "marco f.", "Aporte", "algo raro", 2000],
            ]
        ),
    )

    r = await _confirmar(client, auth_headers, archivo)

    assert r.status_code == 200, r.text
    assert len(await _movimientos(db_session, sample_tenant)) == 1
    otros = (
        await db_session.execute(
            select(UnclassifiedRecord.context_label).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalars().all()
    motivos = [m or "" for m in otros]
    assert len(motivos) == 2
    assert any("sin fecha legible" in m for m in motivos)
    assert any("forma de pago" in m for m in motivos)


async def test_borrar_el_archivo_anula_el_aporte_y_no_lo_deja_huerfano(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _subir(db_session, sample_tenant, _libro([FILA_OK]))
    assert (await _confirmar(client, auth_headers, archivo)).status_code == 200

    preview = await client.request(
        "DELETE", f"/api/v1/ingestion/files/{archivo.id}", headers=auth_headers
    )
    assert preview.status_code in (200, 409), preview.text
    borrado = await client.request(
        "DELETE", f"/api/v1/ingestion/files/{archivo.id}?confirm=true", headers=auth_headers
    )

    assert borrado.status_code == 200, borrado.text
    (mov,) = await _movimientos(db_session, sample_tenant)
    assert mov.voided_at is not None
    assert "archivo" in (mov.void_reason or "")


async def test_la_hoja_excluida_a_proposito_no_se_importa(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    archivo = await _subir(db_session, sample_tenant, _libro([FILA_OK]))

    r = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={"confirmed_fields": {}, "context_confirmed": {_CONTEXTO: False}},
        headers=auth_headers,
    )

    assert r.status_code in (200, 422), r.text
    assert await _movimientos(db_session, sample_tenant) == []
