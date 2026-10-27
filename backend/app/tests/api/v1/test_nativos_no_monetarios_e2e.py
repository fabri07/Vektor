"""I5-1 (revisión) — un número nativo de xlsx que NO es un monto llega como texto.

I5-1 hizo que el parser xlsx conserve las celdas numéricas como número, para que
un monto nativo no se reinterprete con el convenio de la columna. Pero en Excel
también se cargan como número los códigos de barras, los SKU, los comprobantes,
los CUIT, DNI, teléfonos, códigos postales y hasta nombres («1001»), y los
lectores del importador y de los maestros los tratan como texto (``.strip()``,
``re.sub``, ``normalize_*``): con un ``int`` ahí el confirm devolvía 500, y el
preview de maestros se quedaba vacío.

El principio: sólo los campos de monto, cantidad, precio y costo se benefician
del nativo; todo lo demás tiene que llegar como texto, igual que antes.

**Prueba diferencial.** Cada caso importa el MISMO xlsx en dos tenants gemelos:
uno con el parser actual y otro con el parser de antes de I5-1 (toda celda a
texto, que se reproduce forzando ``_celda_de_fila`` a ``str``). Los montos de
estos archivos no tienen tres decimales, así que lo persistido —tabla por tabla,
columna por columna— tiene que ser idéntico, y el preview de maestros también.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import file_parsing
from app.application.services.file_parsing import parse_uploaded_content
from app.domain.business_time import today_ar
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests._respaldo import productos_con_compra
from app.tests.conftest import FakeRedis, _pin_window_key, _suscripcion_free_legado
from app.utils.security import create_access_token

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Lo que el dump no compara: marcas de tiempo de carga y tablas de bitácora,
#: que difieren entre dos corridas aunque el import sea el mismo.
_COLUMNAS_DE_CARGA = {"created_at", "updated_at", "imported_at", "deleted_at", "tenant_id"}
_TABLAS_FUERA = {
    "users",
    "tenants",
    "subscriptions",
    "decision_audit_log",
    "business_profiles",
    "health_scores",
    "health_score_snapshots",
    "pipeline_events",
    "import_attempts",
    "uploaded_files",
}


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


@pytest_asyncio.fixture
async def gemelo(db_session: AsyncSession, fake_redis: FakeRedis) -> tuple[Tenant, dict[str, str]]:
    """Un tenant igual a ``sample_tenant`` (kiosco, plan FREE) con su OWNER."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco Gemelo",
        display_name="Kiosco Gemelo",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            vertical_code=Vertical.KIOSCO_ALMACEN.value,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
        )
    )
    db_session.add(_suscripcion_free_legado(tenant.tenant_id))
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"owner-{uuid.uuid4().hex[:6]}@gemelo.com",
        full_name="Dueño Gemelo",
        password_hash="x",
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await fake_redis.set(_pin_window_key(tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": "OWNER"}
    )
    return tenant, {"Authorization": f"Bearer {token}"}


def _xlsx(hojas: dict[str, list[list[Any]]], *, con_otra_hoja: bool) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    if con_otra_hoja:
        hojas = {**hojas, "Notas": [["nota", "detalle"], ["reponer", "velas"]]}
    for nombre, filas in hojas.items():
        hoja = wb.create_sheet(nombre)
        for fila in filas:
            hoja.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _parser_de_antes(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """El parser de antes de I5-1: toda celda de xlsx viaja como texto."""
    monkeypatch.setattr(file_parsing, "_celda_de_fila", lambda valor, _conservar: str(valor))
    return monkeypatch.undo


_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
_HASH = re.compile(r"\b[0-9a-f]{64}\b")
_SKU_INTERNO = re.compile(r"VKT-[A-Z0-9]{12}")


def _uuid_o_texto(valor: Any) -> str:
    """El valor sin lo que depende de la corrida y no del archivo.

    Dos tenants distintos dan ids, huellas (llevan el tenant adentro), SKU
    internos aleatorios y marcas de tiempo distintos aunque el import sea el
    mismo. Las marcas de HOY (UTC o AR) se tapan; las fechas de negocio del
    archivo (2026-03, 2015) no, porque no son de hoy.
    """
    s = _SKU_INTERNO.sub("<sku>", _HASH.sub("<hash>", _UUID.sub("<uuid>", str(valor))))
    for dia in {datetime.now(UTC).date().isoformat(), today_ar().isoformat()}:
        s = re.sub(rf"{dia}[ T][0-9:.]+", "<ts>", s)
    return s


async def _dump(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, list[str]]:
    """Todo lo persistido para el tenant, sin ids ni marcas de carga."""
    salida: dict[str, list[str]] = {}
    tablas = (
        await db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    ).scalars().all()
    for tabla in sorted(tablas):
        if tabla in _TABLAS_FUERA:
            continue
        columnas = [r[1] for r in (await db.execute(text(f'PRAGMA table_info("{tabla}")'))).all()]
        if "tenant_id" not in columnas:
            continue
        filas = (
            await db.execute(
                text(f'SELECT * FROM "{tabla}" WHERE tenant_id IN (:a, :b)'),
                {"a": tenant_id.hex, "b": str(tenant_id)},
            )
        ).mappings().all()
        if filas:
            salida[tabla] = sorted(
                json.dumps(
                    {k: _uuid_o_texto(v) for k, v in f.items() if k not in _COLUMNAS_DE_CARGA},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for f in filas
            )
    return salida


def _sin_uuids(valor: Any) -> Any:
    return json.loads(json.dumps(valor, default=str), object_hook=lambda d: {
        k: (_uuid_o_texto(v) if isinstance(v, str) else v) for k, v in d.items()
    })


async def _importar(
    client: AsyncClient,
    headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    contenido: bytes,
    entidad: str,
    mapeo: list[tuple[str, str]],
    productos: list[str],
) -> dict[str, Any]:
    if productos:
        await productos_con_compra(db, tenant.tenant_id, productos)
    summary = parse_uploaded_content(contenido, _XLSX, "planilla.xlsx")
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="planilla.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/planilla.xlsx",
        content_type=_XLSX,
        size_bytes=4096,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    contexto = summary["mapping_contexts"][0]["context_id"]
    clave = {
        "sale": "ventas",
        "expense": "gastos",
        "product": "productos",
        "customer": "clientes",
        "supplier": "proveedores",
    }[entidad]
    preview = await client.get(f"/api/v1/ingestion/files/{record.id}/preview", headers=headers)
    sugerencias = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings",
        params={"context_id": contexto},
        headers=headers,
    )
    confirm = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {
                    "source_column": origen,
                    "target_field": destino,
                    "context_id": contexto,
                    "entity_type": entidad,
                    "user_selected": True,
                }
                for origen, destino in mapeo
            ],
            "confirmed_fields": {clave: True},
            "context_confirmed": {contexto: True},
            "context_entity": {contexto: entidad},
        },
        headers=headers,
    )
    return {
        "preview_status": preview.status_code,
        "maestros": _sin_uuids(preview.json().get("master_previews"))
        if preview.status_code == 200
        else preview.text,
        "sugerencias_status": sugerencias.status_code,
        "sugerencias": _sin_uuids(sugerencias.json()),
        "confirm_status": confirm.status_code,
        "confirm": confirm.text if confirm.status_code != 200 else "",
        "db": await _dump(db, tenant.tenant_id),
    }


_CASOS: dict[str, tuple[dict[str, list[list[Any]]], str, list[tuple[str, str]], list[str]]] = {
    "venta_con_codigos_y_nombre_numerico": (
        {
            "Ventas": [
                ["fecha", "producto", "total", "cliente", "forma de pago", "dni cliente",
                 "tel cliente", "comprobante", "pto venta", "notas", "id op"],
                ["2026-03-01", "Vela", 1500, 12345, 1, 30123456, 1155551234, 1001, 3, 777, 55],
                ["2026-03-02", 1001, 2000.5, "Ana", "efectivo", None, None, 1002, 3, 0, 56],
            ]
        },
        "sale",
        [
            ("fecha", "transaction_date"), ("producto", "product_name"), ("total", "amount"),
            ("cliente", "customer_name"), ("forma de pago", "payment_method"),
            ("dni cliente", "customer_dni"), ("tel cliente", "customer_phone"),
            ("comprobante", "invoice_number"), ("pto venta", "document_series"),
            ("notas", "notes"), ("id op", "external_operation_id"),
        ],
        ["Vela", "1001"],
    ),
    "venta_con_barcode": (
        {"Ventas": [["fecha", "producto", "total", "cliente", "forma de pago", "codigo de barras"],
                    ["2026-03-01", "Vela", 1500, "Ana", "efectivo", 7790001234567]]},
        "sale",
        [("fecha", "transaction_date"), ("producto", "product_name"), ("total", "amount"),
         ("cliente", "customer_name"), ("forma de pago", "payment_method"),
         ("codigo de barras", "barcode")],
        ["Vela"],
    ),
    "venta_con_sku": (
        {"Ventas": [["fecha", "producto", "total", "cliente", "forma de pago", "sku"],
                    ["2026-03-01", "Vela", 1500, "Ana", "efectivo", 555]]},
        "sale",
        [("fecha", "transaction_date"), ("producto", "product_name"), ("total", "amount"),
         ("cliente", "customer_name"), ("forma de pago", "payment_method"), ("sku", "sku")],
        ["Vela"],
    ),
    "compra_con_codigos": (
        {"Compras": [["fecha", "proveedor", "categoria", "producto", "sku", "codigo de barras",
                      "cantidad", "total", "notas", "factura", "medio de pago"],
                     ["2026-03-01", 5555, 3, "Vela", 555, 7790001234567, 10, 1000, 99, 123, 2]]},
        "expense",
        [("fecha", "expense_date"), ("proveedor", "supplier_name"), ("categoria", "category"),
         ("producto", "product_name"), ("sku", "sku"), ("codigo de barras", "barcode"),
         ("cantidad", "quantity"), ("total", "amount"), ("notas", "notes"),
         ("factura", "invoice_number"), ("medio de pago", "payment_method")],
        [],
    ),
    "compra_de_producto_con_nombre_numerico": (
        {"Compras": [["fecha", "proveedor", "producto", "cantidad", "total"],
                     ["2026-03-01", "Distri", 1001, 10, 1000]]},
        "expense",
        [("fecha", "expense_date"), ("proveedor", "supplier_name"), ("producto", "product_name"),
         ("cantidad", "quantity"), ("total", "amount")],
        [],
    ),
    "gasto_con_notas_cero": (
        {"Gastos": [["fecha", "proveedor", "categoria", "total", "notas", "factura"],
                    ["2026-03-01", 5555, 3, 1000, 0, 123]]},
        "expense",
        [("fecha", "expense_date"), ("proveedor", "supplier_name"), ("categoria", "category"),
         ("total", "amount"), ("notas", "notes"), ("factura", "invoice_number")],
        [],
    ),
    "catalogo_con_codigos": (
        {"Productos": [["nombre", "sku", "codigo de barras", "categoria", "precio", "stock",
                        "costo"],
                       [1001, 555, 7790001234567, 3, 1200, 0, 800]]},
        "product",
        [("nombre", "name"), ("sku", "sku"), ("codigo de barras", "barcode"),
         ("categoria", "category"), ("precio", "sale_price_ars"), ("stock", "stock_units"),
         ("costo", "unit_cost_ars")],
        [],
    ),
    "clientes_con_cuit_dni_telefono_cp": (
        # Sin columna «código»: un listado con código se lee como catálogo.
        {"Clientes": [["cliente", "cuit", "dni", "telefono", "cp", "email", "direccion"],
                      ["Juan Perez", 20123456786, 30123456, 1155551234, 1406, "j@x.com", 123],
                      [4455, None, 31123456, 5491155556666, 0, "a@x.com", "Calle 1"]]},
        "customer",
        [("cliente", "name"), ("cuit", "cuit"), ("dni", "dni"), ("telefono", "phone"),
         ("cp", "postal_code"), ("email", "email"), ("direccion", "address")],
        [],
    ),
    "proveedores_con_cuit_y_telefono": (
        {"Proveedores": [["proveedor", "cuit", "telefono", "email"],
                         ["Distri SA", 30712345671, 1155551234, "d@x.com"],
                         [555, 30712345679, 1144445556, "e@x.com"]]},
        "supplier",
        [("proveedor", "name"), ("cuit", "cuit"), ("telefono", "phone"), ("email", "email")],
        [],
    ),
}


@pytest.mark.parametrize("con_otra_hoja", [False, True], ids=["una_hoja", "varias_hojas"])
@pytest.mark.parametrize("caso", list(_CASOS))
async def test_un_nativo_que_no_es_monto_se_importa_como_antes(
    caso: str,
    con_otra_hoja: bool,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    gemelo: tuple[Tenant, dict[str, str]],
) -> None:
    hojas, entidad, mapeo, productos = _CASOS[caso]
    contenido = _xlsx(hojas, con_otra_hoja=con_otra_hoja)

    ahora = await _importar(
        client, auth_headers, db_session, sample_tenant, contenido, entidad, mapeo, productos
    )
    restaurar = _parser_de_antes(monkeypatch)
    try:
        tenant_antes, headers_antes = gemelo
        antes = await _importar(
            client, headers_antes, db_session, tenant_antes, contenido, entidad, mapeo, productos
        )
    finally:
        restaurar()

    assert ahora["confirm_status"] == 200, ahora["confirm"]
    assert ahora["preview_status"] == 200
    assert antes["confirm_status"] == 200, "el caso tiene que andar con el parser de antes"
    if entidad in ("customer", "supplier"):
        assert ahora["maestros"], (
            f"el preview de maestros no puede quedar vacío (antes: {antes['maestros']})"
        )
    assert ahora["maestros"] == antes["maestros"]
    assert ahora["sugerencias_status"] == 200
    assert ahora["sugerencias"] == antes["sugerencias"], "el mapeo sugerido cambió"
    assert ahora["db"] == antes["db"], _diferencias(ahora["db"], antes["db"])


def _diferencias(ahora: dict[str, list[str]], antes: dict[str, list[str]]) -> str:
    """Las filas que cambiaron, campo por campo, para que el fallo diga qué."""
    lineas = ["lo persistido cambió respecto del parser de antes:"]
    for tabla in sorted(set(ahora) | set(antes)):
        a, b = ahora.get(tabla, []), antes.get(tabla, [])
        if a == b:
            continue
        for fila_a, fila_b in zip(a, b, strict=False):
            da, db = json.loads(fila_a), json.loads(fila_b)
            campos = {
                k: (da.get(k), db.get(k)) for k in set(da) | set(db) if da.get(k) != db.get(k)
            }
            if campos:
                lineas.append(f"  {tabla}: {campos}")
        if len(a) != len(b):
            lineas.append(f"  {tabla}: {len(a)} filas ahora, {len(b)} antes")
    return "\n".join(lineas)
