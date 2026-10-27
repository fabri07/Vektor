"""I5-2 — dos encabezados repetidos (o dos vacíos en un CSV) no se pisan.

El dict de cada fila se arma por encabezado. Con «Nombre | Precio | Precio» (el
doble encabezado «Compra | Venta» de una lista de precios) o un CSV con dos
columnas sin título, las dos eran la MISMA clave: ganaba la última, la otra
columna desaparecía sin aviso y, si la persona mapeaba una a costo y otra a precio
de venta, los dos campos leían el mismo dato.

Ahora el parser desambigua (`encabezados_unicos`): la primera conserva el nombre,
las siguientes ``Precio (2)``; un vacío repetido pasa a ``col_{i}``. Acá se mide
de punta a punta: bytes reales → resumen → sugerencias del mapeo → confirm por
HTTP → lo que quedó en la base.
"""

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

from app.application.services.file_parsing import encabezados_unicos, parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CSV_MIME = "text/csv"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro(hojas: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for fila in filas:
            ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


#: Catálogo con «Precio» dos veces (compra y venta) y dos columnas sin título
#: (código y código de barras). En xlsx la celda vacía llega como ``None``.
_FILAS = [
    ["Vela aromatica", "800", "1200", "VEL-1", "7790001000011"],
    ["Difusor bambu", "1500", "2600", "DIF-2", "7790001000028"],
]
_CSV = (
    "Nombre,Precio,Precio,,\n"
    + "".join(",".join(f) + "\n" for f in _FILAS)
).encode()
_XLSX = _libro({"Catalogo": [["Nombre", "Precio", "Precio", None, None], *_FILAS]})

_MAPEO = {
    "Nombre": "name",
    "Precio": "unit_cost_ars",
    "Precio (2)": "sale_price_ars",
    "col_3": "sku",
    "col_4": "barcode",
}
_ENCABEZADOS = ["Nombre", "Precio", "Precio (2)", "col_3", "col_4"]


async def _archivo(
    db: AsyncSession, tenant: Tenant, contenido: bytes, mime: str, nombre: str
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=mime,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, mime, nombre),
    )
    db.add(record)
    await db.commit()
    return record


@pytest.mark.parametrize(
    ("contenido", "mime", "nombre"),
    [(_CSV, _CSV_MIME, "catalogo.csv"), (_XLSX, _XLSX_MIME, "catalogo.xlsx")],
    ids=["csv", "xlsx"],
)
async def test_las_dos_columnas_llegan_se_mapean_por_separado_y_se_persisten(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    contenido: bytes,
    mime: str,
    nombre: str,
) -> None:
    record = await _archivo(db_session, sample_tenant, contenido, mime, nombre)
    resumen = record.parsed_summary_json
    assert resumen["headers"] == _ENCABEZADOS
    assert resumen["mapping_contexts"][0]["headers"] == _ENCABEZADOS
    # La columna de compra no se perdió: cada una conserva su dato.
    primera = resumen["preview_rows"][0]
    assert (primera["Precio"], primera["Precio (2)"]) == ("800", "1200")
    assert any("«Precio (2)»" in w for w in resumen["warnings"])
    if mime == _CSV_MIME:
        assert (primera["col_3"], primera["col_4"]) == ("VEL-1", "7790001000011")
        assert any("sin título" in w for w in resumen["warnings"])

    # El mapeador ve dos columnas distintas, con muestras distintas.
    sugerencias = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings?entity_type=product",
        headers=auth_headers,
    )
    assert sugerencias.status_code == 200, sugerencias.text
    muestras = {s["source_column"]: s["sample_values"] for s in sugerencias.json()}
    assert set(_ENCABEZADOS) <= set(muestras)
    assert "800" in muestras["Precio"] and "1200" not in muestras["Precio"]
    assert "1200" in muestras["Precio (2)"] and "800" not in muestras["Precio (2)"]

    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": c, "target_field": t} for c, t in _MAPEO.items()
            ],
            "confirmed_fields": {"productos": True},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    db_session.expunge_all()
    productos = {
        p.name: p
        for p in (
            await db_session.execute(
                select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
            )
        ).scalars()
    }
    vela = productos["Vela aromatica"]
    assert vela.unit_cost_ars == Decimal("800")
    assert vela.sale_price_ars == Decimal("1200")
    assert vela.sku == "VEL-1"
    assert vela.barcode == "7790001000011"
    difusor = productos["Difusor bambu"]
    assert (difusor.unit_cost_ars, difusor.sale_price_ars) == (Decimal("1500"), Decimal("2600"))
    assert difusor.sku == "DIF-2"


def test_multihoja_desambigua_por_hoja_y_avisa_con_su_nombre() -> None:
    contenido = _libro(
        {
            "Catalogo": [["Nombre", "Precio", "Precio"], ["Vela", 800, 1200]],
            "Ventas": [["Fecha", "Producto", "Monto"], ["2024-03-05", "Vela", 1200]],
        }
    )
    resumen = parse_uploaded_content(contenido, _XLSX_MIME, "libro.xlsx")
    catalogo = next(c for c in resumen["mapping_contexts"] if c["label"] == "Catalogo")
    assert catalogo["headers"] == ["Nombre", "Precio", "Precio (2)"]
    fila = catalogo["preview_rows"][0]
    assert str(fila["Precio"]) == "800" and str(fila["Precio (2)"]) == "1200"
    assert any("«Catalogo»" in w and "«Precio (2)»" in w for w in resumen["warnings"])
    ventas = next(c for c in resumen["mapping_contexts"] if c["label"] == "Ventas")
    assert ventas["headers"] == ["Fecha", "Producto", "Monto"]


def test_sin_repetidos_los_nombres_no_cambian_y_no_hay_aviso() -> None:
    """Los mapeos guardados por nombre tienen que seguir resolviendo igual: un
    archivo sin repetidos produce exactamente los encabezados de antes, incluido
    un único vacío de CSV (``""``) y el ``col_{i}`` de una celda vacía de xlsx."""
    fila = ["Fecha", "", None, "Precio", "Precio "]
    assert encabezados_unicos(fila) == (["Fecha", "", "col_2", "Precio", "Precio "], [])

    csv = b"Fecha,Producto,Monto,\n2024-03-05,Vela,1200,x\n"
    resumen = parse_uploaded_content(csv, _CSV_MIME, "v.csv")
    assert resumen["headers"] == ["Fecha", "Producto", "Monto", ""]
    assert not any("mismo nombre" in w or "sin título" in w for w in resumen["warnings"])


def test_determinista_y_sin_choques_con_nombres_del_archivo() -> None:
    """Preview, relectura y confirm parsean los mismos bytes: los nombres tienen
    que salir iguales cada vez, y nunca pisar un encabezado que ya existía."""
    fila = ["A", "A (2)", "A", "", "", "col_4"]
    primera = encabezados_unicos(fila)
    assert primera == encabezados_unicos(list(fila))
    nombres = primera[0]
    assert nombres == ["A", "A (2)", "A (3)", "col_3", "col_4 (2)", "col_4"]
    assert len(set(nombres)) == len(nombres)


def test_csv_avisa_las_celdas_mas_alla_del_ultimo_encabezado() -> None:
    csv = b"Fecha,Monto\n2024-03-05,1200,dato suelto\n2024-03-06,900\n"
    resumen = parse_uploaded_content(csv, _CSV_MIME, "v.csv")
    assert any("1 fila(s) traen datos a la derecha" in w for w in resumen["warnings"])
