"""V-16 (revalidación) — las claves reservadas de «Otros» no vienen del archivo.

Una columna del archivo que se llamara ``__monto_canonico__`` habilitaba la forma
canónica del monto en una fila de archivo (G-1), y una ``__convenios_de_fecha__``
decidía el orden día/mes de su fecha (m-1). `_capture_unclassified` descarta toda
clave ``__x__`` de la fila; las que escribe Véktor llegan por ``internas``, por
``row_ref`` o desde ``ORIGINALES_KEY``.
"""

from __future__ import annotations

import io
import json
import uuid
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    CONVENIOS_DE_FECHA_KEY,
    IDENTITY_REF_KEY,
    MONTO_CANONICO_KEY,
    ORIGINALES_KEY,
    RISK_REF_KEY,
    ROW_REF_KEY,
    _capture_unclassified,
    interpretar_fecha_de_fila_de_otros,
    monto_de_fila_de_otros,
)
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra
from app.tests.api.v1.test_otros_lote_compuertas_i245 import (
    _VELA,
    _importar_sugeridos,
    _pendiente,
    _sin_broker,  # noqa: F401
    _todos,
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Sesion:
    def __init__(self) -> None:
        self.agregados: list[Any] = []

    def add(self, obj: Any) -> None:
        self.agregados.append(obj)


def _capturar(row: dict[str, Any], **kw: Any) -> dict[str, Any]:
    sesion = _Sesion()
    n = _capture_unclassified(
        sesion,  # type: ignore[arg-type]
        uuid.uuid4(), [row], None, "ingestion", None, **kw,
    )
    assert n == 1
    datos: dict[str, Any] = sesion.agregados[0].row_data
    return datos


def test_las_reservadas_del_archivo_se_descartan() -> None:
    datos = _capturar({
        "fecha": "04/03/2025",
        "monto": "12.50",
        MONTO_CANONICO_KEY: "1",
        CONVENIOS_DE_FECHA_KEY: '{"fecha": "mdy"}',
        ROW_REF_KEY: "falso",
        IDENTITY_REF_KEY: "falsa",
        " __monto_canonico__ ": "1",
        "__context__": "sheet:Ventas",
    })
    assert datos == {"fecha": "04/03/2025", "monto": "12.50"}


def test_las_de_vektor_se_escriben_y_le_ganan_al_archivo() -> None:
    datos = _capturar(
        {
            "fecha": "04/03/2025",
            "monto": "12.50",
            MONTO_CANONICO_KEY: "0",
            ROW_REF_KEY: "falso",
            CONVENIOS_DE_FECHA_KEY: '{"fecha": "dmy"}',
            ORIGINALES_KEY: {CONVENIOS_DE_FECHA_KEY: {"fecha": "mdy"}},
        },
        row_ref="ref-legitimo",
        internas={MONTO_CANONICO_KEY: "1", RISK_REF_KEY: '{"row_index": 3}'},
    )
    assert datos[MONTO_CANONICO_KEY] == "1"
    assert datos[ROW_REF_KEY] == "ref-legitimo"
    assert datos[RISK_REF_KEY] == '{"row_index": 3}'
    # El convenio que decidió la carga (dentro de ORIGINALES), no el del archivo.
    assert json.loads(datos[CONVENIOS_DE_FECHA_KEY]) == {"fecha": "mdy"}


def test_originales_que_no_son_de_vektor_no_rompen_la_captura() -> None:
    datos = _capturar({"monto": "10", ORIGINALES_KEY: '{"monto": "99"}'})
    assert datos == {"monto": "10"}


def test_convenio_falsificado_no_desambigua_la_fecha() -> None:
    """m-1: «04/03/2025» sin convenio de la carga queda a resolver a mano."""
    datos = _capturar({"fecha": "04/03/2025", CONVENIOS_DE_FECHA_KEY: '{"fecha": "mdy"}'})
    interpretada = interpretar_fecha_de_fila_de_otros(datos)
    assert interpretada is not None and interpretada.valor is None


def test_la_columna_exacta_del_monto_le_gana_a_una_parecida_vacia() -> None:
    """m-2: «subtotal» o «total_iva» vacías no le ganan a «monto» / «total»."""
    assert monto_de_fila_de_otros({"subtotal": "", "monto": "1200"}) == Decimal("1200")
    assert monto_de_fila_de_otros({"total_iva": "", "total": "1200"}) == Decimal("1200")
    # Sin columna exacta: la parecida vacía deja la fila sin monto (no se adivina).
    assert monto_de_fila_de_otros({"subtotal": "", "precio_unitario": "5"}) is None


async def _subir(
    client: AsyncClient, auth_headers: dict[str, str], db: AsyncSession, tid: uuid.UUID,
    headers: list[str], filas: list[list[Any]],
) -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Ventas"
    ws.append(headers)
    for fila in filas:
        ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    contenido = buf.getvalue()
    rec = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="f.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/f.xlsx", content_type=_XLSX,
        size_bytes=len(contenido), purpose="ingestion", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX, "f.xlsx"),
    )
    db.add(rec)
    await db.commit()
    col_art = "producto" if "producto" in headers else "articulo"
    mapeo = {"fecha": "transaction_date", col_art: "product_name",
             "cantidad": "quantity", "total": "amount"}
    r = await client.post(
        f"/api/v1/ingestion/files/{rec.id}/confirm",
        json={
            "column_mappings": [{"source_column": c, "target_field": t} for c, t in mapeo.items()],
            "confirmed_fields": {"ventas": True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    db.expunge_all()


async def test_columna_con_la_marca_no_habilita_la_forma_canonica_en_el_lote(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """G-1: «12.50» en una columna de coma decimal no entra aunque el archivo
    traiga una columna ``__monto_canonico__`` en «1»."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    await _subir(
        client, auth_headers, db_session, tid,
        ["fecha", "cliente", "articulo", "cantidad", "total", "__monto_canonico__"],
        [["05/03/2025", "Ana", _VELA, 1, "1.234,50", "1"],
         ["06/03/2025", "Ana", _VELA, 1, "12.50", "1"]],
    )
    filas = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert len(filas) == 1
    assert MONTO_CANONICO_KEY not in filas[0].row_data

    cuerpo = await _importar_sugeridos(client, auth_headers)

    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (0, 1), cuerpo
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert sorted(v.amount for v in ventas) == [Decimal("1234.50")]


async def test_columna_con_la_marca_no_habilita_la_forma_canonica_en_link_product(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """G-1, por `link-product`: sin la marca de Véktor, «12.50» → 422 MONTO_REQUERIDO."""
    tid = sample_tenant.tenant_id
    coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
    await producto_con_compra(db_session, tid, nombre=_VELA)
    await _subir(
        client, auth_headers, db_session, tid,
        ["fecha", "cliente", "producto", "cantidad", "total", "__monto_canonico__"],
        [["05/03/2025", "Ana", _VELA, 1, "1.234,50", "1"],
         ["06/03/2025", "Ana", "Negrita chica", 1, "12.50", "1"]],
    )
    filas = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert len(filas) == 1
    assert MONTO_CANONICO_KEY not in filas[0].row_data

    r = await client.post(
        f"/api/v1/others/{filas[0].id}/link-product",
        json={"product_id": str(coca.id)}, headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "MONTO_REQUERIDO"
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert Decimal("12.50") not in {v.amount for v in ventas}


async def test_la_fila_que_escribe_b5e_conserva_su_marca(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La venta que B·5e saca a «Otros» lleva la marca de Véktor aunque el archivo
    traiga una columna que se llame igual, y `link-product` lee su «12.50»."""
    tid = sample_tenant.tenant_id
    coca = await producto_con_compra(db_session, tid, nombre="Coca Cola 500ml")
    await producto_con_compra(db_session, tid, nombre=_VELA)
    await _subir(
        client, auth_headers, db_session, tid,
        ["fecha", "cliente", "producto", "cantidad", "total", "__monto_canonico__"],
        [["05/03/2025", "Ana", _VELA, 1, "1.234,50", "0"],
         ["06/03/2025", "Ana", "Negrita chica", 1, "12,50", "0"]],
    )

    filas = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert len(filas) == 1, [f.row_data for f in filas]
    assert filas[0].row_data[MONTO_CANONICO_KEY] == "1"
    assert filas[0].row_data["monto"] == "12.50"

    r = await client.post(
        f"/api/v1/others/{filas[0].id}/link-product",
        json={"product_id": str(coca.id)}, headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert sorted(v.amount for v in ventas) == [Decimal("12.50"), Decimal("1234.50")]
