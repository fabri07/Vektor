"""E6b — importar dos veces el mismo período se avisa, no se descarta.

El guard que ya existía cubre un solo escenario: la re-subida **byte a byte**
(409 por ``content_hash``, con override explícito). Medido: la misma planilla
reexportada desde Excel cambia de hash —el zip guarda timestamps—, así que ese
guard no la ve, y sin nada más el import duplica todo en silencio (verificado
contra Postgres real: 3 gastos → 6, $12.000 → $24.000, stock 10 → 20).

Lo que se agrega es un AVISO con los archivos que se parecen. La regla de negocio
es explícita en que no alcanza para más: sin una clave fuerte —ID estable de la
operación en el sistema de origen, o identidad completa de comprobante— coincidir
en fecha e importe **no prueba** que dos operaciones sean la misma. Un kiosco
puede comprar dos veces lo mismo el mismo día. Por eso estos tests afirman las
dos mitades: que avisa cuando corresponde, y que **no toca nada** — ni descarta
filas, ni bloquea el import, ni marca nada como duplicado.
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
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HEADERS = ["fecha", "articulo", "cantidad", "total", "proveedor"]
_MAPEO = [
    {"source_column": "fecha", "target_field": "expense_date"},
    {"source_column": "articulo", "target_field": "product_name"},
    {"source_column": "cantidad", "target_field": "quantity"},
    {"source_column": "total", "target_field": "amount"},
    {"source_column": "proveedor", "target_field": "supplier_name"},
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro(filas: list[list[Any]]) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Compras"
    hoja.append(_HEADERS)
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_MARZO = [
    ["2024-03-05", "Vela aromatica 200g", 5, 6000, "Distribuidora Sur"],
    ["2024-03-06", "Difusor bambu", 3, 3600, "Distribuidora Sur"],
]
_ABRIL = [
    ["2024-04-05", "Vela aromatica 200g", 5, 7000, "Distribuidora Sur"],
    ["2024-04-06", "Difusor bambu", 3, 4200, "Distribuidora Sur"],
]


async def _subir_y_confirmar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
    nombre: str,
) -> dict[str, Any]:
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
    db_session.add(record)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={"column_mappings": _MAPEO, "confirmed_fields": {"gastos": True}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    cuerpo: dict[str, Any] = resp.json()
    return cuerpo


async def _gastos_vivos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
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


async def test_el_mismo_periodo_desde_otro_archivo_se_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso real: la planilla reexportada, con otro nombre y otro hash."""
    primero = await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _MARZO, "compras-marzo.xlsx"
    )
    assert not [w for w in primero["warnings"] if "coinciden" in w], (
        f"el primer import no tiene con qué coincidir: {primero['warnings']}"
    )

    segundo = await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _MARZO, "compras-marzo (1).xlsx"
    )
    aviso = [w for w in segundo["warnings"] if "coinciden" in w]
    assert aviso, f"la segunda carga del mismo período no avisó nada: {segundo['warnings']}"
    assert "compras-marzo.xlsx" in aviso[0], (
        f"el aviso no dice con qué archivo se parece: {aviso[0]}"
    )
    assert "2 operación" in aviso[0], f"el aviso no dice cuántas: {aviso[0]}"


async def test_avisar_no_es_descartar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La mitad que protege al usuario del propio aviso.

    Dos compras iguales el mismo día son posibles y legítimas. El aviso existe
    para que las MIRE, no para decidir por él: las cuatro filas tienen que estar
    y el import tiene que haber devuelto 200.
    """
    tenant_id = sample_tenant.tenant_id
    await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _MARZO, "compras-marzo.xlsx"
    )
    await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _MARZO, "compras-marzo (1).xlsx"
    )

    vivos = await _gastos_vivos(db_session, tenant_id)
    assert len(vivos) == 4, (
        f"el aviso descartó filas en vez de sólo informar: {[str(g.amount) for g in vivos]}"
    )


async def test_un_periodo_distinto_no_dispara_el_aviso(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La contracara que hace útil al aviso: si saltara siempre, sería ruido y el
    usuario lo dejaría de leer. Abril no se parece a marzo — ni en fecha ni en
    importe— y no tiene por qué avisar nada."""
    await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _MARZO, "compras-marzo.xlsx"
    )
    abril = await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, _ABRIL, "compras-abril.xlsx"
    )

    assert not [w for w in abril["warnings"] if "coinciden" in w], (
        f"avisó de un solapamiento que no existe: {abril['warnings']}"
    )


async def test_el_aviso_no_mira_las_filas_del_propio_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un archivo con dos compras iguales adentro no se solapa consigo mismo.

    Es el falso positivo más fácil de cometer: comparar todo contra todo sin
    excluir el propio archivo haría que CUALQUIER planilla con dos líneas iguales
    —dos cajas del mismo producto al mismo precio, algo perfectamente normal—
    avisara de un duplicado inexistente en su primera carga.
    """
    repetidas = [
        ["2024-03-05", "Vela aromatica 200g", 5, 6000, "Distribuidora Sur"],
        ["2024-03-05", "Vela aromatica 200g", 5, 6000, "Distribuidora Sur"],
    ]
    solo = await _subir_y_confirmar(
        client, auth_headers, db_session, sample_tenant, repetidas, "compras.xlsx"
    )
    assert not [w for w in solo["warnings"] if "coinciden" in w], (
        f"el archivo se solapó consigo mismo: {solo['warnings']}"
    )
