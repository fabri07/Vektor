"""F-H3.e — qué le hace al inventario cada hoja, servido a la UI.

Depende de la entidad de la hoja y de los campos que el mapeo BORRADOR cubre: la
misma hoja de ventas, con `cantidad` mapeada o sin mapear, mueve o no mueve stock.
Por eso lo calcula el backend con el mapeo en curso y no una tabla fija en la
pantalla — la copia de una regla de dominio en el frontend es lo que ya rompió el
mapeo de columnas (incidente ASTERIA): la UI mostraba una cosa y mandaba otra.

**F-F.4**: el endpoint dejó de ofrecer opciones y pasó a informar el efecto. Sigue
existiendo, y con la misma forma, porque la pantalla necesita saber qué mostrar (y
qué no mostrar) por hoja, y esa sigue siendo una regla de dominio.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.inventory_effect import (
    CURRENT_SNAPSHOT,
    EFFECT_LABELS,
    HISTORICAL_REPLAY,
)
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant

_VENTAS = "sheet:Ventas"
_CATALOGO = "sheet:Catalogo"


def _summary() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _VENTAS,
                "label": "Ventas marzo",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
            {
                "context_id": _CATALOGO,
                "label": "Catálogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["nombre", "stock"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="libro.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/libro.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _map(ctx: str, entity: str, source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": ctx,
        "entity_type": entity,
    }


_MAPEO_COMPLETO = [
    _map(_VENTAS, "sale", "fecha", "transaction_date"),
    _map(_VENTAS, "sale", "producto", "product_name"),
    _map(_VENTAS, "sale", "cantidad", "quantity"),
    _map(_VENTAS, "sale", "monto", "amount"),
    _map(_CATALOGO, "product", "nombre", "name"),
    _map(_CATALOGO, "product", "stock", "stock_units"),
]


async def _efectos(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    mapeos: list[dict[str, Any]],
    context_entity: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    cuerpo: dict[str, Any] = {"column_mappings": mapeos}
    if context_entity:
        cuerpo["context_entity"] = context_entity
    response = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/inventory-effects",
        json=cuerpo,
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return {hoja["context_id"]: hoja for hoja in response.json()}


class TestEfectoPorHoja:
    async def test_cada_hoja_trae_el_efecto_que_le_corresponde(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """Las ventas de mercadería descuentan; el catálogo declara su saldo.

        Y cada una trae UNA sola opción: desde F-F.4 no hay nada que elegir, así
        que la pantalla informa. Si acá volviera a haber dos, el selector habría
        vuelto sin que nadie lo declare.
        """
        hojas = await _efectos(client, auth_headers, archivo, _MAPEO_COMPLETO)

        ventas = hojas[_VENTAS]
        assert ventas["default"] == HISTORICAL_REPLAY
        assert [o["value"] for o in ventas["options"]] == [HISTORICAL_REPLAY]

        catalogo = hojas[_CATALOGO]
        assert catalogo["default"] == CURRENT_SNAPSHOT
        # Un saldo no es una secuencia: no se reproduce como movimientos.
        assert [o["value"] for o in catalogo["options"]] == [CURRENT_SNAPSHOT]

    async def test_el_nombre_de_la_hoja_es_el_legible(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """El `context_id` es interno; mostrárselo al usuario no lo ayuda."""
        hojas = await _efectos(client, auth_headers, archivo, _MAPEO_COMPLETO)
        assert hojas[_VENTAS]["label"] == "Ventas marzo"
        assert hojas[_CATALOGO]["label"] == "Catálogo"

    async def test_las_etiquetas_salen_del_dominio(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """Fuente única: si la UI las escribiera, dirían otra cosa que el backend."""
        hojas = await _efectos(client, auth_headers, archivo, _MAPEO_COMPLETO)
        for hoja in hojas.values():
            for opcion in hoja["options"]:
                assert opcion["label"] == EFFECT_LABELS[opcion["value"]]

    async def test_sin_cantidad_mapeada_la_hoja_no_habla_de_inventario(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """El caso que obliga a recalcular contra el mapeo BORRADOR y no una vez.

        Es la misma hoja del mismo archivo: lo único que cambia es que el usuario
        todavía no mapeó `cantidad`. Sin unidades no hay inventario que mover, y la
        pantalla no tiene que decir NADA sobre el stock de esa hoja — el cartel en
        una hoja que no habla de inventario es lo que F-F.4 vino a sacar.
        """
        sin_cantidad = [
            m for m in _MAPEO_COMPLETO if m["target_field"] != "quantity"
        ]
        hojas = await _efectos(client, auth_headers, archivo, sin_cantidad)

        ventas = hojas[_VENTAS]
        assert ventas["default"] is None
        assert ventas["options"] == []

    async def test_un_campo_propio_no_habilita_mover_inventario(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """`custom_field:quantity` guarda el dato, pero el importador no lo lee como
        cantidad — mismo criterio que el confirm, que sólo cuenta targets canónicos."""
        con_custom = [
            m if m["target_field"] != "quantity" else {**m, "target_field": "custom_field:quantity"}
            for m in _MAPEO_COMPLETO
        ]
        hojas = await _efectos(client, auth_headers, archivo, con_custom)

        assert hojas[_VENTAS]["default"] is None

    async def test_cambiar_la_seccion_de_la_hoja_cambia_su_efecto(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """F-F.4 + F-A: la sección la sigue eligiendo el usuario; el efecto la sigue.

        Es la consecuencia deliberada de derivar el efecto de la entidad EFECTIVA.
        El usuario reasigna la hoja de ventas a «gastos» —una compra— y esa hoja
        pasa a mover inventario igual (mercadería, con cantidad); la reasigna a
        «clientes» y deja de hablar de stock. Lo que NO cambia es que la sección es
        decisión suya: acá se prueba que el efecto la respete, no que la reemplace.
        """
        como_compra = await _efectos(
            client,
            auth_headers,
            archivo,
            _MAPEO_COMPLETO,
            context_entity={_VENTAS: "expense"},
        )
        assert como_compra[_VENTAS]["default"] == HISTORICAL_REPLAY

        como_maestro = await _efectos(
            client,
            auth_headers,
            archivo,
            _MAPEO_COMPLETO,
            context_entity={_VENTAS: "customer"},
        )
        assert como_maestro[_VENTAS]["default"] is None
        assert como_maestro[_VENTAS]["options"] == []

    async def test_archivo_inexistente_es_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{uuid.uuid4()}/inventory-effects",
            json={"column_mappings": []},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_requiere_autenticacion(
        self, client: AsyncClient, archivo: UploadedFile
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/inventory-effects",
            json={"column_mappings": []},
        )
        assert response.status_code in (401, 403)


class TestLaDecisionDeEnvioApuntaAUnaHojaConEnvio:
    """F-H6.b: una decisión que no se puede honrar no se ignora en silencio.

    Es la misma regla que ya rige para el efecto de inventario: si el usuario cree
    haber resuelto algo sobre sus costos y ese algo no va a pasar, el confirm lo
    dice en vez de importar como si nada.
    """

    async def test_decision_sobre_una_hoja_sin_columna_de_envio_es_422(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        respuesta = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [
                    _map(_VENTAS, "sale", "fecha", "transaction_date"),
                    _map(_VENTAS, "sale", "monto", "amount"),
                ],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_VENTAS: True},
                "shipping_decisions": [
                    {"context_id": _VENTAS, "action": "una_por_hoja"}
                ],
            },
            headers=auth_headers,
        )

        assert respuesta.status_code == 422
        assert "envío" in respuesta.json()["detail"].lower()
        # El mensaje nombra la hoja con su label legible, no el context_id crudo.
        assert "Ventas marzo" in respuesta.json()["detail"]

    async def test_una_accion_invalida_la_rechaza_el_schema(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        respuesta = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/confirm",
            json={
                "column_mappings": [_map(_VENTAS, "sale", "fecha", "transaction_date")],
                "confirmed_fields": {"ventas": True},
                "context_confirmed": {_VENTAS: True},
                "shipping_decisions": [
                    {"context_id": _VENTAS, "action": "lo_que_sea"}
                ],
            },
            headers=auth_headers,
        )
        assert respuesta.status_code == 422
