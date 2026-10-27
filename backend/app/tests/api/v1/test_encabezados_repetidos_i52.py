"""I5-2 — dos encabezados repetidos (o dos vacíos en un CSV) no se pisan.

El dict de cada fila se arma por encabezado. Con «Nombre | Precio | Precio» (el
doble encabezado «Compra | Venta» de una lista de precios) o un CSV con dos
columnas sin título, las dos eran la MISMA clave: ganaba la última, la otra
columna desaparecía sin aviso y, si la persona mapeaba una a costo y otra a precio
de venta, los dos campos leían el mismo dato.

Ahora el parser desambigua (`encabezados_unicos`): la ÚLTIMA conserva el nombre
—la columna que el nombre leía antes, así que historial, mapeos por nombre y
relecturas no cambian de dato— y las anteriores pasan a ``Precio (1)``…; un vacío
anterior con datos pasa a ``col_{i}``. Acá se mide de punta a punta: bytes reales
→ resumen → sugerencias del mapeo → confirm por HTTP → lo que quedó en la base, y
contra lo que leía cada nombre antes del arreglo (``_como_antes``).
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import (
    compute_column_null_stats,
    encabezados_unicos,
    flag_columns_at_risk,
    parse_uploaded_content,
)
from app.persistence.models.column_mapping import TenantColumnMapping
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

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


def _como_antes(contenido: bytes) -> dict[str, str | None]:
    """Lo que cada nombre leía en la primera fila ANTES de I5-2: el dict por
    encabezado crudo, donde el último repetido pisaba al anterior."""
    filas = [f.split(",") for f in contenido.decode().splitlines()]
    return dict(zip(filas[0], filas[1], strict=True))


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

#: La primera «Precio» (compra) es la que antes se perdía: ahora es «Precio (1)».
#: «Precio» sigue siendo la última (venta), como leía antes. En CSV el último
#: vacío conserva ``""`` y el anterior, que tiene datos, pasa a ``col_3``.
_ENCABEZADOS = {
    "csv": ["Nombre", "Precio (1)", "Precio", "col_3", ""],
    "xlsx": ["Nombre", "Precio (1)", "Precio", "col_3", "col_4"],
}
_MAPEO = {
    "csv": {
        "Nombre": "name",
        "Precio (1)": "unit_cost_ars",
        "Precio": "sale_price_ars",
        "col_3": "sku",
    },
    "xlsx": {
        "Nombre": "name",
        "Precio (1)": "unit_cost_ars",
        "Precio": "sale_price_ars",
        "col_3": "sku",
        "col_4": "barcode",
    },
}


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


async def _confirmar(
    client: AsyncClient, h: dict[str, Any], record: UploadedFile,
    mapeo: dict[str, str], campos: dict[str, bool],
) -> None:
    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": c, "target_field": t} for c, t in mapeo.items()
            ],
            "confirmed_fields": campos,
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("contenido", "mime", "nombre", "formato"),
    [
        (_CSV, _CSV_MIME, "catalogo.csv", "csv"),
        (_XLSX, _XLSX_MIME, "catalogo.xlsx", "xlsx"),
    ],
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
    formato: str,
) -> None:
    record = await _archivo(db_session, sample_tenant, contenido, mime, nombre)
    resumen = record.parsed_summary_json
    assert resumen["headers"] == _ENCABEZADOS[formato]
    assert resumen["mapping_contexts"][0]["headers"] == _ENCABEZADOS[formato]
    # La columna de compra no se perdió: cada una conserva su dato.
    primera = resumen["preview_rows"][0]
    assert (primera["Precio (1)"], primera["Precio"]) == ("800", "1200")
    assert any("«Precio (1)»" in w for w in resumen["warnings"])
    if formato == "csv":
        assert (primera["col_3"], primera[""]) == ("VEL-1", "7790001000011")
        assert any("sin título con datos" in w and "«col_3»" in w for w in resumen["warnings"])
        # Diferencial: cada nombre sin sufijo lee lo MISMO que antes del arreglo.
        antes = _como_antes(contenido)
        for nombre_col in ("Nombre", "Precio", ""):
            assert primera[nombre_col] == antes[nombre_col]

    # El mapeador ve dos columnas distintas, con muestras distintas.
    sugerencias = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings?entity_type=product",
        headers=auth_headers,
    )
    assert sugerencias.status_code == 200, sugerencias.text
    muestras = {s["source_column"]: s["sample_values"] for s in sugerencias.json()}
    assert set(_MAPEO[formato]) <= set(muestras)
    assert "800" in muestras["Precio (1)"] and "1200" not in muestras["Precio (1)"]
    assert "1200" in muestras["Precio"] and "800" not in muestras["Precio"]

    await _confirmar(client, auth_headers, record, _MAPEO[formato], {"productos": True})

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
    if formato == "xlsx":
        assert vela.barcode == "7790001000011"
    difusor = productos["Difusor bambu"]
    assert (difusor.unit_cost_ars, difusor.sale_price_ars) == (Decimal("1500"), Decimal("2600"))
    assert difusor.sku == "DIF-2"


async def test_el_historial_del_tenant_sigue_leyendo_la_misma_columna(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un tenant que ya aprendió «precio → precio de venta» con este formato de
    archivo: antes del arreglo «Precio» leía la última columna (venta). Si el
    nombre pasara a la primera, el historial sugeriría el COSTO como precio de
    venta, con confianza alta."""
    ahora = datetime.now(tz=UTC)
    db_session.add(
        TenantColumnMapping(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            entity_type="product",
            source_column="precio",
            target_field="sale_price_ars",
            confirmed_count=3,
            last_seen_at=ahora,
            created_at=ahora,
        )
    )
    await db_session.commit()
    record = await _archivo(db_session, sample_tenant, _CSV, _CSV_MIME, "catalogo.csv")

    sugerencias = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings?entity_type=product",
        headers=auth_headers,
    )
    assert sugerencias.status_code == 200, sugerencias.text
    precio = next(s for s in sugerencias.json() if s["source_column"] == "Precio")
    assert precio["target_field"] == "sale_price_ars"
    assert precio["source"] == "tenant_history"
    antes = _como_antes(_CSV)["Precio"]
    assert antes == "1200"
    assert antes in precio["sample_values"] and "800" not in precio["sample_values"]


async def test_un_mapeo_explicito_por_nombre_persiste_el_mismo_dato_que_antes(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Gasto con «total» dos veces (neto y con IVA) y el mapeo ``total → amount``
    que ya traía el cliente: antes se persistía la última (1210); tiene que
    seguir siendo esa, no el neto."""
    contenido = b"fecha,detalle,total,total\n2024-03-05,Luz,1000,1210\n"
    record = await _archivo(db_session, sample_tenant, contenido, _CSV_MIME, "gastos.csv")
    assert record.parsed_summary_json["headers"] == ["fecha", "detalle", "total (1)", "total"]
    await _confirmar(
        client,
        auth_headers,
        record,
        {"fecha": "expense_date", "detalle": "description", "total": "amount"},
        {"gastos": True},
    )
    db_session.expunge_all()
    gasto = (
        await db_session.execute(
            select(ExpenseEntry).where(ExpenseEntry.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()
    assert _como_antes(contenido)["total"] == "1210"
    assert gasto.amount == Decimal("1210")


def test_multihoja_desambigua_por_hoja_y_avisa_con_su_nombre() -> None:
    contenido = _libro(
        {
            "Catalogo": [["Nombre", "Precio", "Precio"], ["Vela", 800, 1200]],
            "Ventas": [["Fecha", "Producto", "Monto"], ["2024-03-05", "Vela", 1200]],
        }
    )
    resumen = parse_uploaded_content(contenido, _XLSX_MIME, "libro.xlsx")
    catalogo = next(c for c in resumen["mapping_contexts"] if c["label"] == "Catalogo")
    assert catalogo["headers"] == ["Nombre", "Precio (1)", "Precio"]
    fila = catalogo["preview_rows"][0]
    assert str(fila["Precio (1)"]) == "800" and str(fila["Precio"]) == "1200"
    assert any("«Catalogo»" in w and "«Precio (1)»" in w for w in resumen["warnings"])
    ventas = next(c for c in resumen["mapping_contexts"] if c["label"] == "Ventas")
    assert ventas["headers"] == ["Fecha", "Producto", "Monto"]


def test_sin_repetidos_los_nombres_no_cambian_y_no_hay_aviso() -> None:
    """Los mapeos guardados por nombre tienen que seguir resolviendo igual: un
    archivo sin repetidos produce exactamente los encabezados de antes, incluido
    un único vacío de CSV (``""``) y el ``col_{i}`` de una celda vacía de xlsx."""
    fila = ["Fecha", "", None, "Precio", "Precio "]
    assert encabezados_unicos(fila, []) == (["Fecha", "", "col_2", "Precio", "Precio "], [])

    csv = b"Fecha,Producto,Monto,\n2024-03-05,Vela,1200,x\n"
    resumen = parse_uploaded_content(csv, _CSV_MIME, "v.csv")
    assert resumen["headers"] == ["Fecha", "Producto", "Monto", ""]
    assert not any("mismo nombre" in w or "sin título" in w for w in resumen["warnings"])


def test_comas_finales_de_excel_quedan_como_antes_y_sin_aviso() -> None:
    """``Fecha,Producto,Monto,,``: dos columnas sin título vacías en TODAS las
    filas. No hay dato que perder: el encabezado y las columnas en riesgo son
    los de antes, y no se avisa nada nuevo."""
    csv = b"Fecha,Producto,Monto,,\n2024-03-05,Vela,1200,,\n2024-03-06,Difusor,900,,\n"
    resumen = parse_uploaded_content(csv, _CSV_MIME, "v.csv")
    assert resumen["headers"] == ["Fecha", "Producto", "Monto", "", ""]
    assert set(resumen["preview_rows"][0]) == {"Fecha", "Producto", "Monto", ""}
    assert not any("sin título" in w or "mismo nombre" in w for w in resumen["warnings"])
    filas = [f.split(",") for f in csv.decode().splitlines()]
    antes = [dict(zip(filas[0], f, strict=True)) for f in filas[1:]]
    assert resumen.get("columns_at_risk") == flag_columns_at_risk(
        compute_column_null_stats(antes)
    )


def test_determinista_y_sin_choques_con_nombres_del_archivo() -> None:
    """Preview, relectura y confirm parsean los mismos bytes: los nombres tienen
    que salir iguales cada vez, y nunca pisar un encabezado que ya existía."""
    fila = ["A", "A (1)", "A", "", "", "col_3", "A"]
    datos = [["1", "2", "3", "x", "y", "z", "4"]]
    primera = encabezados_unicos(fila, datos)
    assert primera == encabezados_unicos(list(fila), [list(datos[0])])
    nombres = primera[0]
    assert nombres == ["A (2)", "A (1)", "A (3)", "col_3 (2)", "", "col_3", "A"]
    assert len(set(nombres)) == len(nombres)


def test_csv_avisa_las_celdas_mas_alla_del_ultimo_encabezado() -> None:
    csv = b"Fecha,Monto\n2024-03-05,1200,dato suelto\n2024-03-06,900\n"
    resumen = parse_uploaded_content(csv, _CSV_MIME, "v.csv")
    assert any("1 fila(s) traen datos a la derecha" in w for w in resumen["warnings"])
