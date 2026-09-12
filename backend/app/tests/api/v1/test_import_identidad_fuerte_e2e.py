"""E6b — con identidad fuerte, el mismo comprobante no se aplica dos veces.

El aviso de solapamiento (``test_import_solapamiento_e2e``) es lo máximo que se
puede hacer SIN identidad: mira fecha e importe y avisa, porque dos compras
iguales el mismo día existen. Acá el archivo trae comprobante completo —tipo,
punto de venta, número y CUIT del emisor—, y con eso sí se puede afirmar que dos
filas son la misma operación.

Cada test de este archivo afirma una de las dos mitades del contrato:

* lo que la identidad **permite decidir** (no aplicar dos veces el mismo
  documento);
* y sobre todo lo que **no** — dos tenants, dos emisores o dos puntos de venta con
  el mismo número no se confunden; un comprobante de varios renglones conserva
  todos; y un archivo sin identidad suficiente sigue por el camino de siempre.

La mitad negativa es la que importa más: un falso positivo acá significa que
Véktor descarta plata real creyendo que ya la tenía.
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
from app.persistence.models.operation_identity import (
    OperationIdentity,
    OperationIdentityLink,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# `proveedor` está para que el clasificador lea la hoja como libro de COMPRAS:
# sin esa columna la infiere como catálogo de productos. No participa de la
# identidad —el emisor se identifica por CUIT, nunca por nombre—; está para que
# el archivo se parezca a uno real.
_HEADERS = [
    "fecha", "tipo", "pto", "nro", "cuit", "proveedor", "articulo", "cantidad", "total",
]
_MAPEO = [
    {"source_column": "fecha", "target_field": "expense_date"},
    {"source_column": "tipo", "target_field": "document_type"},
    {"source_column": "pto", "target_field": "document_series"},
    {"source_column": "nro", "target_field": "invoice_number"},
    {"source_column": "cuit", "target_field": "supplier_cuit"},
    {"source_column": "proveedor", "target_field": "supplier_name"},
    {"source_column": "articulo", "target_field": "product_name"},
    {"source_column": "cantidad", "target_field": "quantity"},
    {"source_column": "total", "target_field": "amount"},
]

_CUIT_SUR = "30-71234567-8"
_CUIT_NORTE = "30-99999999-7"

#: Una factura de dos renglones. Es el caso que obliga a distinguir documento de
#: línea: si la identidad fuera del documento a secas y se aplicara "una vez",
#: el segundo renglón se perdería.
_FACTURA_2_RENGLONES = [
    ["2024-03-05", "Factura A", "0001", "00000123", _CUIT_SUR, "Sur", "Vela aromatica", 5, 6000],
    ["2024-03-05", "Factura A", "0001", "00000123", _CUIT_SUR, "Sur", "Difusor bambu", 3, 3600],
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


async def _confirmar(
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
    cuerpo["_file_id"] = str(record.id)
    return cuerpo


async def _gastos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
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


async def _identidades(db: AsyncSession, tenant_id: uuid.UUID) -> list[OperationIdentity]:
    return list(
        (
            await db.execute(
                select(OperationIdentity).where(OperationIdentity.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )


# ── Lo que la identidad permite decidir ──────────────────────────────────────
async def test_el_mismo_comprobante_desde_otro_archivo_no_se_aplica_dos_veces(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso que motiva todo: la planilla reexportada, con otro nombre y otro
    hash. Sin identidad duplicaba los dos gastos; con identidad no aplica nada."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "marzo.xlsx"
    )
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2

    segundo = await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "marzo (1).xlsx"
    )
    gastos = await _gastos(db_session, sample_tenant.tenant_id)
    assert len(gastos) == 2, (
        f"la segunda carga del mismo comprobante duplicó los gastos: {len(gastos)}"
    )
    assert sum(g.amount for g in gastos) == 9600
    assert any("ya estaban cargadas" in w for w in segundo["warnings"]), segundo["warnings"]


async def test_un_comprobante_de_dos_renglones_conserva_los_dos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La identidad es del DOCUMENTO. Si "ya está aplicado" se resolviera por
    documento y se aplicara una sola fila, el segundo renglón se perdería."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "factura.xlsx"
    )
    gastos = await _gastos(db_session, sample_tenant.tenant_id)
    assert len(gastos) == 2
    assert sorted(g.amount for g in gastos) == [3600, 6000]
    # Una identidad (el documento), dos vínculos (los renglones). Es lo que
    # después permite liberar de forma selectiva.
    identidades = await _identidades(db_session, sample_tenant.tenant_id)
    assert len(identidades) == 1
    vinculos = (
        await db_session.execute(
            select(OperationIdentityLink).where(
                OperationIdentityLink.identity_id == identidades[0].id
            )
        )
    ).scalars().all()
    assert len(vinculos) == 2


async def test_reordenar_los_renglones_no_lo_vuelve_un_documento_distinto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Nadie reordena una planilla a propósito, pero un exportador sí. Si el
    orden cambiara la huella, el mismo comprobante se vería como CONFLICTO y le
    caería al usuario una revisión que no tiene nada que revisar."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "a.xlsx"
    )
    segundo = await _confirmar(
        client,
        auth_headers,
        db_session,
        sample_tenant,
        list(reversed(_FACTURA_2_RENGLONES)),
        "b.xlsx",
    )
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2
    assert not [w for w in segundo["warnings"] if "conflicto" in w.lower()], segundo["warnings"]


async def test_mismo_comprobante_con_datos_distintos_va_a_revision(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Puede ser una corrección (el proveedor reemitió) o un error de carga, y
    las dos se parecen. No se omite —escondería la corrección— ni se aplica
    —duplicaría el efecto—: va a «Otros» con el motivo."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "original.xlsx"
    )
    corregida = [list(f) for f in _FACTURA_2_RENGLONES]
    corregida[0][-1] = 6500  # el proveedor reemitió con otro total

    resp = await _confirmar(
        client, auth_headers, db_session, sample_tenant, corregida, "corregida.xlsx"
    )
    gastos = await _gastos(db_session, sample_tenant.tenant_id)
    assert len(gastos) == 2, "el conflicto no puede aplicarse: duplicaría el efecto"
    assert sum(g.amount for g in gastos) == 9600, "quedaron los montos originales"

    otros = (
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(otros) == 2, f"las filas en conflicto tienen que quedar visibles: {len(otros)}"
    assert any("misma identidad" in (r.context_label or "").lower() for r in otros), [
        r.context_label for r in otros
    ]
    assert any("mismo comprobante" in w and "datos distintos" in w for w in resp["warnings"]), (
        resp["warnings"]
    )


# ── Lo que la identidad NO puede confundir ───────────────────────────────────
async def test_dos_emisores_con_el_mismo_numero_no_se_confunden(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Dos proveedores numeran sus facturas por su cuenta: la 0001-00000123 de
    uno y la del otro son dos compras. Si el emisor no entrara en la clave, la
    segunda se descartaría — plata real perdida en silencio."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "sur.xlsx"
    )
    del_norte = [list(f) for f in _FACTURA_2_RENGLONES]
    for fila in del_norte:
        fila[4] = _CUIT_NORTE

    await _confirmar(client, auth_headers, db_session, sample_tenant, del_norte, "norte.xlsx")
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 4
    assert len(await _identidades(db_session, sample_tenant.tenant_id)) == 2


async def test_dos_puntos_de_venta_con_el_mismo_numero_no_se_confunden(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El número se reinicia en cada punto de venta del MISMO emisor."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "pv1.xlsx"
    )
    otro_pv = [list(f) for f in _FACTURA_2_RENGLONES]
    for fila in otro_pv:
        fila[2] = "0002"

    await _confirmar(client, auth_headers, db_session, sample_tenant, otro_pv, "pv2.xlsx")
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 4


async def test_dos_tipos_de_comprobante_con_el_mismo_numero_no_se_confunden(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La factura 0001-123 y el remito 0001-123 del mismo proveedor son dos
    documentos distintos, y el remito puede ser la entrega de esa factura."""
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "factura.xlsx"
    )
    remito = [list(f) for f in _FACTURA_2_RENGLONES]
    for fila in remito:
        fila[1] = "Remito"

    await _confirmar(client, auth_headers, db_session, sample_tenant, remito, "remito.xlsx")
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 4


async def test_sin_identidad_suficiente_no_se_descarta_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin CUIT del emisor no hay clave fuerte. El comportamiento tiene que ser
    exactamente el de antes: se importa todo y el circuito de candidatos avisa.
    Este test es la garantía de que la deduplicación no se filtra a los archivos
    que no la habilitaron."""
    sin_cuit = [list(f) for f in _FACTURA_2_RENGLONES]
    for fila in sin_cuit:
        fila[4] = ""

    await _confirmar(client, auth_headers, db_session, sample_tenant, sin_cuit, "a.xlsx")
    segundo = await _confirmar(
        client, auth_headers, db_session, sample_tenant, sin_cuit, "b.xlsx"
    )
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 4, (
        "sin clave fuerte NO se descarta: se avisa, que es lo que ya hacía"
    )
    assert not await _identidades(db_session, sample_tenant.tenant_id)
    assert any("coinciden" in w for w in segundo["warnings"]), segundo["warnings"]


async def test_la_identidad_no_bloquea_una_fila_que_no_llego_a_aplicarse(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una fila sin monto reclama su identidad y no inserta nada. Si esa
    reclamación quedara tomada, el archivo corregido no se podría reimportar y el
    usuario vería "ya está aplicada" sobre algo que nunca se aplicó."""
    # Va con una fila sana al lado: un archivo que no importa NADA corta con 422
    # ("import vacío") antes de llegar a lo que este test mide.
    rota = [
        ["2024-03-05", "Factura A", "0001", "00000900", _CUIT_SUR, "Sur", "Vela", 5, ""],
        ["2024-03-05", "Factura A", "0001", "00000901", _CUIT_SUR, "Sur", "Difusor", 1, 900],
    ]
    await _confirmar(client, auth_headers, db_session, sample_tenant, rota, "rota.xlsx")
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 1
    claves = {i.identity_key for i in await _identidades(db_session, sample_tenant.tenant_id)}
    assert not [k for k in claves if k.endswith(":900")], (
        "la reclamación de la fila que no insertó nada tiene que devolverse al "
        f"cierre del import; quedaron {claves}"
    )

    corregida = [
        ["2024-03-05", "Factura A", "0001", "00000900", _CUIT_SUR, "Sur", "Vela", 5, 6000]
    ]
    await _confirmar(client, auth_headers, db_session, sample_tenant, corregida, "ok.xlsx")
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2


# ── Borrado y relectura: la identidad se suelta con el efecto, no con el archivo ─
async def test_borrar_el_archivo_libera_la_identidad_y_permite_reimportar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin esto, un archivo mal mapeado quedaría bloqueado para siempre: se borra
    para corregirlo, se vuelve a subir, y la identidad diría "ya está aplicada"
    sobre operaciones que ya no existen."""
    primero = await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "a.xlsx"
    )
    assert len(await _identidades(db_session, sample_tenant.tenant_id)) == 1

    borrado = await client.delete(
        f"/api/v1/ingestion/files/{primero['_file_id']}?confirm=true", headers=auth_headers
    )
    assert borrado.status_code == 200, borrado.text
    # `expunge_all` y no `expire_all`: expirar deja los objetos attachados y el
    # refresh lazy dispara IO fuera del greenlet de SQLAlchemy async.
    db_session.expunge_all()
    assert not await _gastos(db_session, sample_tenant.tenant_id)
    assert not await _identidades(db_session, sample_tenant.tenant_id), (
        "revertidos los dos renglones, la identidad tiene que quedar libre"
    )

    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "b.xlsx"
    )
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2


async def test_un_conflicto_no_se_puede_importar_desde_otros_sin_decidir(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La revisión humana no puede ser la puerta de atrás del candado.

    Si la fila que el confirm mandó a «Otros» por conflicto se importara desde ahí
    sin volver a mirar, se aplicaría exactamente el efecto duplicado que el
    importador se negó a aplicar — y el usuario ni se enteraría de que fue eso lo
    que pasó.
    """
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, _FACTURA_2_RENGLONES, "original.xlsx"
    )
    corregida = [list(f) for f in _FACTURA_2_RENGLONES]
    corregida[0][-1] = 6500
    await _confirmar(
        client, auth_headers, db_session, sample_tenant, corregida, "corregida.xlsx"
    )

    pendientes = (
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert pendientes
    registro = pendientes[0]

    cuerpo = {
        "entity_type": "expense",
        "fields": {
            "amount": "6500",
            "category": "INVENTORY",
            "expense_date": "2024-03-05T00:00:00",
            "description": "Vela aromatica",
        },
    }
    rechazo = await client.post(
        f"/api/v1/others/{registro.id}/reclassify", json=cuerpo, headers=auth_headers
    )
    assert rechazo.status_code == 409, rechazo.text
    assert rechazo.json()["detail"]["code"] == "IMPORT_IDENTITY_TAKEN"
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 2

    # Con la decisión explícita del usuario, sí entra: Véktor no puede saber cuál
    # de las dos versiones vale, y la última palabra es suya.
    aceptado = await client.post(
        f"/api/v1/others/{registro.id}/reclassify",
        json={**cuerpo, "aplicar_pese_al_conflicto": True},
        headers=auth_headers,
    )
    assert aceptado.status_code == 200, aceptado.text
    assert len(await _gastos(db_session, sample_tenant.tenant_id)) == 3
