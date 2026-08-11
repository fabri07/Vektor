"""F-F — las compras del propio archivo respaldan a sus ventas, por fecha.

Este archivo probaba lo contrario: hasta F-F, un libro de UNA sola tabla que
declaraba el stock *y* las ventas se **rechazaba con 422** antes del lease
(F-H3.d.6). No era un capricho: el gate miraba un saldo estático previo al
archivo, y las compras de la fila de abajo todavía no se habían aplicado, así que
"validar cada venta contra el stock" habría mandado a «Otros» ventas que el propio
archivo respalda.

Con el gate cronológico ese rechazo dejó de tener razón de ser: las compras entran
como créditos CON FECHA (`CreditEvent`), se intercalan con las ventas y el archivo
plano se evalúa igual que uno multi-hoja. Lo que antes era un 422 ahora es un
import que respeta el orden de los movimientos.

Lo que este camino todavía no gatea —y por eso hay un test que lo fija— es la
venta de un producto que el propio archivo CREA: al pre-escanear no existe, no
entra como candidata y la venta se importa sin validar. F-F.2 lo convierte en un
descuento pendiente contado, en vez del silencio actual.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_CTX = "table:0"
_LABEL = "Movimientos 2024"
_PRODUCTO = "Vela aromatica 200g"


def _summary() -> dict[str, Any]:
    """Una sola tabla que es a la vez catálogo y libro de ventas.

    `has_producto` y `has_venta` juntos es lo que hace que el confirm cree el
    producto Y registre la venta desde LA MISMA fila — el caso sin saldo previo.
    """
    # Dos filas del mismo producto: la del 03/03 entra en un stock de 2, la del
    # 10/03 no. Con una sola fila no se podría distinguir un gate que elige de uno
    # que apaga el archivo entero.
    filas = [
        {
            "fecha": "2024-03-03",
            "producto": _PRODUCTO,
            "cantidad": "2",
            "monto": "4200",
            "__context__": _CTX,
        },
        {
            "fecha": "2024-03-10",
            "producto": _PRODUCTO,
            "cantidad": "6",
            "monto": "12600",
            "__context__": _CTX,
        },
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "general",
        "multi_sheet": False,
        "has_venta": True,
        "has_producto": True,
        "row_count": 2,
        "headers": ["fecha", "producto", "cantidad", "monto"],
        "ventas_detectadas": filas,
        "stock_detectado": filas,
        "preview_rows": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": _LABEL,
                "source_kind": "table",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 2,
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="movimientos.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/movimientos.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "sale",
    }


_MAPEOS = [
    _map("fecha", "transaction_date"),
    _map("producto", "product_name"),
    _map("cantidad", "quantity"),
    _map("monto", "amount"),
]


def _payload(efecto: str, *, productos: bool = True) -> dict[str, Any]:
    confirmados: dict[str, bool] = {"ventas": True}
    if productos:
        confirmados["productos"] = True
    return {
        "column_mappings": _MAPEOS,
        "confirmed_fields": confirmados,
        "context_confirmed": {_CTX: True},
        "inventory_effect": {_CTX: efecto},
    }


async def _confirmar(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    payload: dict[str, Any],
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json=payload,
        headers=auth_headers,
    )


async def _cuantas(db_session: AsyncSession, tenant: Tenant, modelo: Any) -> int:
    result = await db_session.execute(
        select(modelo).where(modelo.tenant_id == tenant.tenant_id)
    )
    return len(list(result.scalars().all()))


class TestUnArchivoPlanoYaNoSeRechaza:
    async def test_el_archivo_que_declara_stock_y_ventas_se_confirma(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El 422 de F-H3.d.6 ya no existe: ningún archivo se rechaza por ser plano.

        Este archivo crea el producto desde las mismas filas que venden, así que al
        pre-escanear no hay identidad todavía y las ventas entran sin validar contra
        stock. Eso es una limitación conocida y acotada de este camino —F-F.2 la
        convierte en un descuento pendiente contado—, no el silencio que motivaba el
        rechazo: antes el usuario no podía importar el archivo en absoluto.
        """
        response = await _confirmar(
            client, auth_headers, archivo, _payload("historical_replay")
        )

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 2
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 0

    async def test_efecto_sin_hoja_no_se_descarta_en_silencio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Mapeos planos (sin `context_id`) + `inventory_effect`: 422, no silencio.

        No hay hojas contra las cuales resolver el efecto, así que `_inventory_effects`
        quedaba vacío y el import salía con el default: el usuario elegía reconstruir
        su inventario y no pasaba nada — ni el efecto ni un error.
        """
        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            {
                "column_mappings": [
                    {"source_column": s, "target_field": t}
                    for s, t in (
                        ("fecha", "transaction_date"),
                        ("producto", "product_name"),
                        ("cantidad", "quantity"),
                        ("monto", "amount"),
                    )
                ],
                "confirmed_fields": {"ventas": True},
                "inventory_effect": {_CTX: "historical_replay"},
            },
        )

        assert response.status_code == 422
        assert "por hoja" in response.json()["detail"]
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 0

    async def test_ya_no_se_rechaza_por_ser_plano(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Control del anterior por el otro lado: ni un `STAGE_REJECT` en la traza.

        Sin esto, un 200 podría venir de que el rechazo se movió de lugar en vez de
        haber desaparecido.
        """
        await _confirmar(client, auth_headers, archivo, _payload("historical_replay"))

        eventos = list(
            (
                await db_session.execute(
                    select(PipelineEvent).where(PipelineEvent.stage == "reject")
                )
            )
            .scalars()
            .all()
        )
        assert eventos == []

    async def test_el_mismo_archivo_sin_replay_entra(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """La salida que el mensaje ofrece tiene que existir de verdad: el archivo
        se importa igual, sólo que sin tocar el inventario."""
        response = await _confirmar(
            client, auth_headers, archivo, _payload("informational")
        )

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 2

    async def test_sin_alta_de_productos_el_replay_se_gatea_normalmente(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El bloqueo es del archivo que carga productos, no de `historical_replay`.

        Sin `productos` confirmado no hay alta: el producto ya existe con su saldo
        y el gate corre contra él. De las dos filas entra la del 03/03 (2 unidades
        contra 2 en stock) y la del 10/03 se va a "Otros" (6 contra 0). Sin este
        caso, el bloqueo podría estar apagando el replay entero y los tests
        seguirían en verde.
        """
        db_session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                name=_PRODUCTO,
                sale_price_ars=Decimal("2100"),
                unit_cost_ars=Decimal("1200"),
                stock_units=2,
            )
        )
        await db_session.commit()

        response = await _confirmar(
            client,
            auth_headers,
            archivo,
            _payload("historical_replay", productos=False),
        )

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 1
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 1


_CTX_COMPRAS = "sheet:Compras"
_CTX_VENTAS = "sheet:Ventas"
_HEADERS_COMPRAS = ["fecha", "producto", "cantidad", "monto", "proveedor"]
_HEADERS_VENTAS = ["fecha", "producto", "cantidad", "monto"]


def _summary_dos_hojas(dia_compra: str, dia_venta: str) -> dict[str, Any]:
    """Un libro con la compra en una hoja y la venta en otra.

    Es el camino donde la propiedad cronológica es OBSERVABLE de punta a punta: la
    hoja de compras declara unidades que entran con su fecha, y la de ventas las
    consume. En el archivo plano no se puede montar el mismo caso —con `amount`
    mapeado, venta y gasto salen de la misma columna, así que cada fila suma y resta
    lo mismo— y por eso ese camino se cubre con los tests de "ya no se rechaza".
    """
    compras = [
        {
            "fecha": dia_compra,
            "producto": _PRODUCTO,
            "cantidad": "10",
            "monto": "12000",
            "proveedor": "Distribuidora Sur",
            "__context__": _CTX_COMPRAS,
        }
    ]
    ventas = [
        {
            "fecha": dia_venta,
            "producto": _PRODUCTO,
            "cantidad": "6",
            "monto": "12600",
            "__context__": _CTX_VENTAS,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_venta": True,
        "has_gasto": True,
        "row_count": 2,
        "ventas_detectadas": ventas,
        "gastos_detectados": compras,
        "preview_rows": [*compras, *ventas],
        "mapping_contexts": [
            {
                "context_id": _CTX_COMPRAS,
                "label": "Compras",
                "source_kind": "sheet",
                "entity_type": "expense",
                "headers": _HEADERS_COMPRAS,
                "fields": None,
                "preview_rows": compras,
                "row_count": 1,
            },
            {
                "context_id": _CTX_VENTAS,
                "label": "Ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": _HEADERS_VENTAS,
                "fields": None,
                "preview_rows": ventas,
                "row_count": 1,
            },
        ],
    }


async def _archivo_dos_hojas(
    db_session: AsyncSession, tenant: Tenant, dia_compra: str, dia_venta: str
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras_y_ventas_2024.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/compras_y_ventas_2024.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary_dos_hojas(dia_compra, dia_venta),
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _producto_sin_stock(db_session: AsyncSession, tenant: Tenant) -> None:
    """El producto ya existe y está en cero: todo lo que entre lo trae el archivo."""
    db_session.add(
        Product(
            id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            name=_PRODUCTO,
            sale_price_ars=Decimal("2100"),
            unit_cost_ars=Decimal("1200"),
            stock_units=0,
        )
    )
    await db_session.commit()


def _map_en(ctx: str, entidad: str, source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": ctx,
        "entity_type": entidad,
    }


_PAYLOAD_DOS_HOJAS = {
    "column_mappings": [
        _map_en(_CTX_COMPRAS, "expense", "fecha", "expense_date"),
        _map_en(_CTX_COMPRAS, "expense", "producto", "product_name"),
        _map_en(_CTX_COMPRAS, "expense", "cantidad", "quantity"),
        _map_en(_CTX_COMPRAS, "expense", "monto", "amount"),
        _map_en(_CTX_VENTAS, "sale", "fecha", "transaction_date"),
        _map_en(_CTX_VENTAS, "sale", "producto", "product_name"),
        _map_en(_CTX_VENTAS, "sale", "cantidad", "quantity"),
        _map_en(_CTX_VENTAS, "sale", "monto", "amount"),
    ],
    "confirmed_fields": {"ventas": True, "gastos": True},
    "context_confirmed": {_CTX_COMPRAS: True, _CTX_VENTAS: True},
    "inventory_effect": {_CTX_VENTAS: "historical_replay"},
}


class TestLaCompraDelArchivoRespaldaSuVentaPorFecha:
    """F-F — el respaldo se evalúa por fecha, no como un saldo sin tiempo.

    Antes las compras del archivo llegaban al gate metidas dentro del saldo inicial
    (el `stock_units` de ese momento, que ya las incluía) y por lo tanto SIN fecha:
    una compra del 20/03 respaldaba una venta del 10/03. Ahora entran como créditos
    datados y el saldo de partida es el previo al archivo.
    """

    async def test_la_compra_anterior_respalda_la_venta(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        await _producto_sin_stock(db_session, sample_tenant)
        archivo = await _archivo_dos_hojas(
            db_session, sample_tenant, dia_compra="2024-03-01", dia_venta="2024-03-10"
        )

        response = await _confirmar(client, auth_headers, archivo, _PAYLOAD_DOS_HOJAS)

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 1
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 0

    async def test_la_compra_posterior_no_respalda_la_venta(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El control del anterior, y la propiedad que pidió el usuario.

        Mismas dos filas, fechas invertidas: se compró DESPUÉS de vender. Las
        unidades que la venta necesitaba no existían todavía, así que la fila no
        entra y queda en «Otros». Bajo el gate viejo esta venta entraba, porque la
        compra ya estaba sumada al saldo contra el que se la validaba.
        """
        await _producto_sin_stock(db_session, sample_tenant)
        archivo = await _archivo_dos_hojas(
            db_session, sample_tenant, dia_compra="2024-03-20", dia_venta="2024-03-10"
        )

        response = await _confirmar(client, auth_headers, archivo, _PAYLOAD_DOS_HOJAS)

        assert response.status_code == 200, response.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 0
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 1


class TestElBloqueoSeAlcanzaDesdeLaPantalla:
    """F-H3.e — la compuerta de la fase.

    El 422 de d.6 estaba probado desde un payload armado a mano, pero el frontend
    NO mandaba `inventory_effect`: todas las hojas entraban con su default y
    `historical_replay` —el único modo que escribe stock— era inalcanzable desde
    la UI. Una regla que no se puede disparar desde la pantalla no está entregada.

    Este test arma el payload con la MISMA forma que manda `confirmFile`
    (`inventory_effect: {context_id: modo}` junto al resto), y verifica que el
    modo que ofrece el selector llega hasta la decisión del backend.
    """

    async def test_el_modo_que_ofrece_el_selector_llega_al_backend(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El modo que ofrece la pantalla tiene que CAMBIAR lo que hace el confirm.

        Antes esto se verificaba contra el 422 de F-H3.d.6, que ya no existe. La
        evidencia equivalente es el gate corriendo: con el producto ya cargado con 2
        unidades, de las dos ventas entra la del 03/03 y la del 10/03 queda en
        «Otros». Si el modo no llegara al backend, entrarían las dos.
        """
        db_session.add(
            Product(
                id=uuid.uuid4(),
                tenant_id=sample_tenant.tenant_id,
                name=_PRODUCTO,
                sale_price_ars=Decimal("2100"),
                unit_cost_ars=Decimal("1200"),
                stock_units=2,
            )
        )
        await db_session.commit()

        # Lo que la pantalla ofrece para esta hoja, servido por el endpoint nuevo.
        opciones = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/inventory-effects",
            json={"column_mappings": _MAPEOS},
            headers=auth_headers,
        )
        assert opciones.status_code == 200, opciones.text
        hoja = opciones.json()[0]
        assert "historical_replay" in [o["value"] for o in hoja["options"]]

        respuesta = await _confirmar(
            client,
            auth_headers,
            archivo,
            _payload("historical_replay", productos=False),
        )
        assert respuesta.status_code == 200, respuesta.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 1
        assert await _cuantas(db_session, sample_tenant, UnclassifiedRecord) == 1

    async def test_el_default_que_muestra_la_pantalla_es_el_que_aplica_el_backend(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Si divergieran, el usuario vería un modo y el archivo entraría con otro."""
        opciones = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/inventory-effects",
            json={"column_mappings": _MAPEOS},
            headers=auth_headers,
        )
        default_mostrado = opciones.json()[0]["default"]

        # Confirmar mandando EXPLÍCITAMENTE el default que muestra la pantalla
        # tiene que comportarse igual que no mandar nada.
        respuesta = await _confirmar(
            client, auth_headers, archivo, _payload(default_mostrado)
        )
        assert respuesta.status_code == 200, respuesta.text
        assert await _cuantas(db_session, sample_tenant, SaleEntry) == 2
