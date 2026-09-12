"""E4 — qué termina en la base cuando el archivo es difícil de interpretar.

Por qué e2e y no unitario
-------------------------
Las políticas viven en ``domain/numeric_parsing`` y ``domain/date_parsing``, y
sus corpus están en ``app/tests/domain/``. Acá se prueba otra cosa: que el
importador la **use**. Los tres defectos que motivan estos casos pasaban con la
política ya escrita y correcta:

* ``"12.50"`` en una columna cuyo punto es de miles entraba como **1250**, porque
  el convenio decidía qué separador borrar y nadie comprobaba que la celda
  tuviera esa forma;
* una cantidad ``2.5`` se truncaba a **2** en seis lectores que seguían haciendo
  ``int(float(str(...)))`` aunque ``_parse_qty`` ya estuviera arreglado;
* el camino legacy —summaries sin ``mapping_contexts``— entregaba la fila CRUDA,
  así que ni el ``ignore`` del usuario ni el convenio de columna lo alcanzaban;
* una planilla exportada en formato US entraba con las fechas cambiadas de mes,
  porque el orden día/mes se resolvía celda por celda con la convención AR.

Ninguno se ve desde la función: se ven en la fila guardada. Por eso el `.xlsx` lo
arma openpyxl, lo parsea el parser de producción, el confirm entra por HTTP y lo
que se afirma es la entidad persistida (o su ausencia, y la fila en "Otros").
"""

from __future__ import annotations

import io
import unittest.mock
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PRODUCTO = "Vela aromatica 200g"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro(filas: list[list[Any]], headers: list[str]) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Ventas"
    hoja.append(headers)
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _subir(
    db_session: AsyncSession,
    tenant: Tenant,
    contenido: bytes,
    nombre: str,
    *,
    legacy: bool = False,
) -> UploadedFile:
    summary = parse_uploaded_content(contenido, _XLSX_MIME, nombre)
    if legacy:
        # Un archivo cargado antes de que existieran los `mapping_contexts`. No se
        # simula con un summary inventado: se parsea con el parser real y se le
        # saca la clave, que es exactamente la diferencia entre un summary viejo y
        # uno nuevo. Cualquier otra cosa probaría un mundo que no existe.
        summary = {k: v for k, v in summary.items() if k != "mapping_contexts"}
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=_XLSX_MIME,
        size_bytes=4096,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {"source_column": source, "target_field": target}


async def _ventas(db_session: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    result = await db_session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None)
        )
    )
    return list(result.scalars().all())


async def _productos(db_session: AsyncSession, tenant: Tenant) -> list[Product]:
    result = await db_session.execute(
        select(Product).where(Product.tenant_id == tenant.tenant_id)
    )
    return list(result.scalars().all())


async def _otros(db_session: AsyncSession, tenant: Tenant) -> list[UnclassifiedRecord]:
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tenant.tenant_id)
    )
    return list(result.scalars().all())


#: La columna `cliente` no es decorativa: sin ella el clasificador manda la
#: hoja a productos y estos tests medirían otro bucket.
_HEADERS = ["fecha", "producto", "cantidad", "total", "cliente", "forma de pago"]
_CLIENTE = "Consumidor final"


@pytest_asyncio.fixture
async def _confirmar(
    client: AsyncClient, auth_headers: dict[str, str]
) -> Any:
    async def _hacer(file_id: uuid.UUID, mapeos: list[dict[str, Any]]) -> Any:
        return await client.post(
            f"/api/v1/ingestion/files/{file_id}/confirm",
            json={"column_mappings": mapeos, "confirmed_fields": {"ventas": True}},
            headers=auth_headers,
        )

    return _hacer


_MAPEO_VENTAS = [
    _map("fecha", "transaction_date"),
    _map("producto", "product_name"),
    _map("cantidad", "quantity"),
    _map("total", "amount"),
    _map("cliente", "customer_name"),
    _map("forma de pago", "payment_method"),
]


async def test_una_celda_en_otra_escala_no_entra_multiplicada_por_cien(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """`"12.500,50"` fija el convenio de la columna; `"12.50"` no lo cumple.

    Bajo ese convenio el punto es de miles, así que borrarlo dejaba `1250`: cien
    veces lo escrito, sin nada que revisar y sin diferencia visible con una venta
    legítima de mil doscientos cincuenta pesos. Ahora la celda no se interpreta y
    la fila cae a "Otros" con el original a la vista, que es la única salida
    honesta: la columna dice una cosa y la celda otra, y ninguna de las dos
    lecturas es "la más probable".
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["2024-03-10", _PRODUCTO, 1, "12.500,50", _CLIENTE, "efectivo"],
                ["2024-03-11", _PRODUCTO, 1, "12.50", _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "escalas.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [v.amount for v in ventas] == [Decimal("12500.50")], (
        f"la segunda fila no podía entrar como venta: {[str(v.amount) for v in ventas]}"
    )
    assert not any(v.amount == Decimal("1250") for v in ventas), (
        "«12.50» entró como 1250: la escala se cambió en silencio"
    )
    assert len(await _otros(db_session, sample_tenant)) == 1, (
        "la fila que no se pudo leer tiene que quedar para revisar, no desaparecer"
    )


async def test_una_cantidad_fraccionaria_no_se_trunca(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """`2.5` unidades no son 2.

    `int(float("2.5"))` truncaba, y el resultado era una venta con una cantidad
    que el archivo nunca dijo — indistinguible de una venta real de 2. Las
    unidades son enteras por dominio, así que la fila va a "Otros" para que el
    usuario decida, en vez de redondear por él.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["2024-03-10", _PRODUCTO, 2, 8400, _CLIENTE, "efectivo"],
                ["2024-03-11", _PRODUCTO, 2.5, 6300, _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "cantidades.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [(v.quantity, v.amount) for v in ventas] == [(2, Decimal("8400"))], (
        f"la fila con 2,5 unidades no podía entrar: {[(v.quantity, str(v.amount)) for v in ventas]}"
    )
    otros = await _otros(db_session, sample_tenant)
    assert len(otros) == 1
    assert "decimales" in (otros[0].context_label or "").lower(), (
        f"el motivo tiene que explicar el problema del archivo: {otros[0].context_label!r}"
    )
    # El confirm además lo AVISA: sin el aviso, el usuario ve una venta menos y
    # no tiene por qué mirar la bandeja.
    avisos = " ".join(resp.json().get("warnings") or [])
    assert "cantidad no se pudo leer" in avisos, f"el confirm no avisó: {avisos!r}"


async def test_una_cantidad_negativa_tampoco_entra_como_una_unidad(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """El piso en 1 convertía `-3` en una venta de una unidad.

    Es la otra mitad del mismo defecto: el `except: return 1` no distinguía "la
    celda está vacía" de "la celda dice algo que no puedo usar", y las dos
    terminaban en la misma venta de 1.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["2024-03-10", _PRODUCTO, 2, 8400, _CLIENTE, "efectivo"],
                ["2024-03-11", _PRODUCTO, -3, 6300, _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "negativas.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [(v.quantity, v.amount) for v in ventas] == [(2, Decimal("8400"))]
    assert len(await _otros(db_session, sample_tenant)) == 1


async def test_un_archivo_entero_de_cantidades_ilegibles_lo_dice(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """Si NINGUNA fila entra, el import corta —para que se pueda reintentar con
    otro mapeo— pero el mensaje tiene que decir la causa real.

    El default habla de "columnas requeridas no detectadas", y en este archivo las
    columnas se detectaron perfecto: lo que no se pudo usar fue lo que decían. Es
    el mismo criterio que ya tenía la escala ambigua de los montos."""
    record = await _subir(
        db_session,
        sample_tenant,
        _libro([["2024-03-11", _PRODUCTO, -3, 6300, _CLIENTE, "efectivo"]], _HEADERS),
        "todas_ilegibles.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 422, resp.text
    assert "cantidades" in resp.json()["detail"], resp.text
    assert await _ventas(db_session, sample_tenant) == []


async def test_una_celda_vacia_sigue_valiendo_una_unidad(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """La contracara, que es la que hace peligroso el arreglo: si "ilegible" y
    "vacío" se trataran igual, media base de imports históricos —donde la columna
    de cantidad no existe o viene en blanco— dejaría de importarse."""
    record = await _subir(
        db_session,
        sample_tenant,
        _libro([["2024-03-11", _PRODUCTO, None, 6300, _CLIENTE, "efectivo"]], _HEADERS),
        "vacias.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [(v.quantity, v.amount) for v in ventas] == [(1, Decimal("6300"))]
    assert await _otros(db_session, sample_tenant) == []


def _libro_multihoja_de_productos_con_montos_con_punto() -> bytes:
    """Dos hojas —catálogo + gastos— para entrar por el camino multihoja con el
    bucket de PRODUCTOS poblado, que es el que el legacy sigue importando."""
    wb = Workbook()
    productos = wb.active
    productos.title = "Productos"
    productos.append(["producto", "precio", "cantidad"])
    productos.append([_PRODUCTO, "8.400", 7])
    productos.append(["Sahumerio lavanda 20u", "7.000", 3])
    gastos = wb.create_sheet("Gastos")
    gastos.append(["fecha", "descripcion", "monto", "forma de pago"])
    gastos.append(["2024-03-10", "Alquiler", 150000, "transferencia"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _libro_multihoja_con_montos_con_punto() -> bytes:
    """Dos hojas, que es lo que hace falta: el camino legacy sólo se alcanza desde
    ``_insert_multisheet_data`` (``inferred_type == "mixed"`` o ``multi_sheet``).
    Un archivo de una tabla entra por el camino plano, que ya saneaba — y un test
    de una hoja habría pasado igual con el defecto puesto."""
    wb = Workbook()
    ventas = wb.active
    ventas.title = "Ventas"
    ventas.append(_HEADERS)
    ventas.append(["2024-03-10", _PRODUCTO, 7, "8.400", _CLIENTE, "efectivo"])
    ventas.append(["2024-03-11", _PRODUCTO, 7, "7.000", _CLIENTE, "efectivo"])
    gastos = wb.create_sheet("Gastos")
    gastos.append(["fecha", "descripcion", "monto", "forma de pago"])
    gastos.append(["2024-03-10", "Alquiler", 150000, "transferencia"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def _legacy_de_productos_importado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> list[Product]:
    """Un catálogo histórico —sin ``mapping_contexts``— importado UNA vez.

    E2/E4: este camino entregaba la fila CRUDA a los lectores. La columna marcada
    ``ignore`` la encontraba igual ``_row_val`` por keyword, y los montos se leían
    celda por celda sin el convenio de su columna. Los otros dos caminos ya
    preparaban las filas; éste no, y la diferencia no la justifica nada — la
    decisión del usuario no depende del formato en que se guardó el summary.

    **Se mide sobre PRODUCTOS y ya no sobre ventas** (E6b): desde que el camino
    legacy no puede persistir operaciones sin identidad, el bucket de productos
    es el que lo sigue ejerciendo. No es un sustituto lejano — las filas pasan
    por ``preparar_filas_de_hoja``, la MISMA función, con el mismo conjunto de
    columnas ignoradas.

    Las dos garantías se afirman por separado (una prueba cada una) sobre esta
    única corrida: son defectos independientes y tienen que poder fallar por
    separado.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro_multihoja_de_productos_con_montos_con_punto(),
        "legacy_productos.xlsx",
        legacy=True,
    )
    assert "mapping_contexts" not in record.parsed_summary_json
    assert record.parsed_summary_json.get("multi_sheet") or (
        record.parsed_summary_json.get("inferred_type") == "mixed"
    ), "sin esto el confirm entraría por el camino plano y el test no probaría nada"

    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                _map("producto", "name"),
                _map("precio", "sale_price_ars"),
                _map("cantidad", "ignore"),
            ],
            "confirmed_fields": {"productos": True},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    productos = await _productos(db_session, sample_tenant)
    assert len(productos) == 2, f"se esperaban los dos productos, hay {len(productos)}"
    return productos


async def test_el_legacy_de_productos_respeta_el_ignore(
    _legacy_de_productos_importado: list[Product],
) -> None:
    """La columna ignorada es ``cantidad``, elegida porque su efecto es
    observable en la fila guardada: sin el filtrado entra como stock."""
    stocks = [p.stock_units for p in _legacy_de_productos_importado]
    assert set(stocks) == {0}, f"la cantidad ignorada entró igual como stock: {stocks}"


async def test_el_legacy_de_productos_respeta_el_convenio_numerico(
    _legacy_de_productos_importado: list[Product],
) -> None:
    """`"8.400"` es ocho mil cuatrocientos: el punto de esa columna es de miles."""
    precios = sorted(p.sale_price_ars for p in _legacy_de_productos_importado)
    assert precios == [Decimal("7000"), Decimal("8400")], (
        f"los precios se leyeron sin el convenio de su columna: {[str(p) for p in precios]}"
    )


async def test_el_legacy_de_ventas_ya_no_importa_y_explica_como_releer(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """E6b — el MISMO archivo que antes importaba ventas por acá, ahora rebota.

    El contrato cambió a propósito: este camino no calcula ni consulta identidad
    fuerte, así que una fila que entre por acá puede duplicar una operación que
    otro archivo ya cargó — y nadie puede saber que es la misma. Lo que se afirma
    es el rechazo, su acción concreta y que no quedó NINGÚN efecto económico.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro_multihoja_con_montos_con_punto(),
        "legacy.xlsx",
        legacy=True,
    )

    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                _map("fecha", "transaction_date"),
                _map("producto", "product_name"),
                _map("cantidad", "ignore"),
                _map("total", "amount"),
                _map("cliente", "customer_name"),
                _map("forma de pago", "payment_method"),
            ],
            "confirmed_fields": {"ventas": True},
        },
        headers=auth_headers,
    )

    assert resp.status_code == 422, resp.text
    detalle = resp.json()["detail"]
    assert "volvé a leer el archivo" in detalle.lower(), detalle
    assert "subir" not in detalle.lower(), (
        "el archivo está perfecto: lo viejo es su interpretación, no sus bytes"
    )

    assert await _ventas(db_session, sample_tenant) == []
    assert await _otros(db_session, sample_tenant) == []


async def test_la_relectura_importa_el_historico_con_su_interpretacion(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_score_trigger: Any,
) -> None:
    """La salida que ofrece el rechazo tiene que terminar con el archivo adentro.

    No alcanza con que la relectura devuelva contextos: lo que cierra el circuito
    es que las filas queden persistidas y que la interpretación numérica de E4
    siga valiendo sobre el resumen nuevo — releer no puede ser la puerta trasera
    por donde `8.400` vuelve a entrar como 8,4.

    Y se afirma acá lo que el rechazo promete, que **no** es reconfirmar: la
    relectura importa ella misma, con el resumen fresco. Ese resumen sí trae
    ``mapping_contexts``, así que la fila entra por el camino que planifica
    identidades — que es exactamente el motivo del bloqueo.

    Sobre el ``ignore``: en este recorrido no hay ninguno que conservar. El
    confirm rebotó ANTES de persistir borrador, así que no existe decisión del
    usuario sobre estas columnas y la relectura re-deriva el mapeo. Las
    decisiones que sí existen las cubre la suite de relectura; acá se afirma lo
    que este archivo puede afirmar.
    """
    contenido = _libro_multihoja_con_montos_con_punto()
    record = await _subir(
        db_session, sample_tenant, contenido, "legacy.xlsx", legacy=True
    )

    with unittest.mock.patch(
        "app.integrations.s3.S3Client.download",
        new_callable=unittest.mock.AsyncMock,
        return_value=contenido,
    ):
        resultado = await reread_service.apply_reread(
            db_session, record.id, sample_tenant.tenant_id
        )
        await db_session.commit()

    # `legacy_fallback` False = la relectura interpretó el archivo de verdad, no
    # cayó al contexto único que se inventa cuando no puede.
    assert resultado.legacy_fallback is False
    assert resultado.inserted == 3, f"insertó {resultado.inserted}"

    ventas = await _ventas(db_session, sample_tenant)
    assert sorted(v.amount for v in ventas) == [Decimal("7000"), Decimal("8400")], (
        "tras la relectura los montos se leyeron sin el convenio de su columna: "
        f"{[str(v.amount) for v in ventas]}"
    )


async def test_el_orden_dia_mes_lo_decide_la_columna_no_la_celda(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """Una planilla exportada en formato US entraba con las fechas cambiadas.

    `04/13/2026` sólo se puede leer mm/dd —no hay mes 13—, y eso dice cómo hay que
    leer `03/04/2026` en esa MISMA columna: 4 de marzo. El importador la leía como
    3 de abril, porque resolvía cada celda por su cuenta con la convención
    argentina. El error es de un mes entero, no rompe nada visible y termina en
    los cortes por período de todo el producto.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["04/13/2026", _PRODUCTO, 1, 8400, _CLIENTE, "efectivo"],
                ["03/04/2026", _PRODUCTO, 1, 6300, _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "fechas_us.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    fechas = {v.amount: v.transaction_date.date() for v in ventas}
    assert fechas == {
        Decimal("8400"): date(2026, 4, 13),
        Decimal("6300"): date(2026, 3, 4),
    }, f"la fecha ambigua se leyó con el orden equivocado: {fechas}"


async def test_una_planilla_argentina_sigue_leyendose_igual(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """La contracara, que es la que hace peligroso el arreglo: sin señal en la
    columna manda la convención argentina, como siempre."""
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["13/04/2026", _PRODUCTO, 1, 8400, _CLIENTE, "efectivo"],
                ["03/04/2026", _PRODUCTO, 1, 6300, _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "fechas_ar.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    fechas = {v.amount: v.transaction_date.date() for v in ventas}
    assert fechas == {
        Decimal("8400"): date(2026, 4, 13),
        Decimal("6300"): date(2026, 4, 3),
    }


async def test_un_serial_de_excel_ya_no_manda_la_fila_a_revision(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """Una celda de fecha sin formato viaja como número de días desde 1899-12-30.

    Quedaba ilegible, así que la fila iba a "Otros" por "sin fecha reconocible" —
    correcto en el sentido de no inventar nada, y aun así una pérdida evitable:
    la fecha estaba ahí, escrita como la escribe Excel.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro([[45123, _PRODUCTO, 1, 8400, _CLIENTE, "efectivo"]], _HEADERS),
        "serial.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [v.transaction_date.date() for v in ventas] == [date(2023, 7, 16)]
    assert await _otros(db_session, sample_tenant) == []


async def test_otros_guarda_lo_que_decia_el_archivo_no_lo_que_entendio_vektor(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """La fila capturada llega ya interpretada, y guardarla así rompía dos cosas.

    Con los números y las fechas resueltos por columna, la fila que va a "Otros"
    trae `Decimal("12500.50")` y un `datetime`. Persistir eso significaba mostrarle
    al usuario algo distinto de lo que escribió —justo en la pantalla donde le
    pedimos que revise— y, peor, dejaba la fila sin poder reimportarse: fuera de
    su columna, el texto `"12500.50"` es ambiguo y `_parse_amount` lo rechaza,
    correctamente. Antes de la normalización por columna se guardaba
    `"12.500,50"`, que sí se lee solo.

    Se afirma la ida y la vuelta: lo guardado es el original, y lo guardado se
    puede volver a leer.
    """
    from app.application.services.ingestion_import_service import _parse_amount

    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["05/03/2024", _PRODUCTO, 1, "12.500,50", _CLIENTE, "efectivo"],
                ["06/03/2024", _PRODUCTO, 2.5, "8.400,00", _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "otros_original.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    otros = await _otros(db_session, sample_tenant)
    assert len(otros) == 1
    fila = otros[0].row_data
    assert fila["total"] == "8.400,00", f"se guardó la interpretación: {fila['total']!r}"
    assert fila["fecha"] == "06/03/2024", f"se guardó la interpretación: {fila['fecha']!r}"
    assert fila["cantidad"] == "2.5", "el valor problemático tiene que quedar a la vista"
    assert _parse_amount(fila["total"]) == Decimal("8400.00"), (
        "lo guardado no se puede volver a leer: la fila queda atrapada en la bandeja"
    )


async def test_otros_explica_que_no_se_pudo_leer_el_monto_no_que_falta_mapearlo(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    _confirmar: Any,
) -> None:
    """El mensaje por defecto pide mapear la columna del monto, y en este archivo
    la columna está mapeada: lo que no se pudo fue leer la celda.

    Mandar a corregir lo que no está roto es la forma más rápida de que el usuario
    concluya que la bandeja no sirve. El motivo tiene que nombrar el problema real
    —y mostrar el valor— para que sepa qué corregir en el archivo.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _libro(
            [
                ["05/03/2024", _PRODUCTO, 1, "12.500,50", _CLIENTE, "efectivo"],
                ["06/03/2024", _PRODUCTO, 1, "12.50", _CLIENTE, "efectivo"],
            ],
            _HEADERS,
        ),
        "motivo_monto.xlsx",
    )
    resp = await _confirmar(record.id, _MAPEO_VENTAS)
    assert resp.status_code == 200, resp.text

    otros = await _otros(db_session, sample_tenant)
    assert len(otros) == 1
    label = otros[0].context_label or ""
    assert "escala" in label, f"el motivo no dice qué pasó: {label!r}"
    assert "12.50" in label, f"el motivo no muestra el valor del archivo: {label!r}"
    assert "Mapeá la columna del monto" not in label, (
        f"manda a corregir un mapeo que está bien: {label!r}"
    )
