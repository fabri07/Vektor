"""F-H3.e — qué le propone Véktor al inventario de cada hoja, servido a la UI.

El default y las opciones dependen de la entidad de la hoja y de los campos que el
mapeo BORRADOR cubre: la misma hoja de ventas, con `cantidad` mapeada o sin
mapear, puede o no aplicar su historia al stock. Por eso lo calcula el backend con
el mapeo en curso y no una tabla fija en la pantalla — la copia de una regla de
dominio en el frontend es lo que ya rompió el mapeo de columnas (incidente
ASTERIA): la UI mostraba una cosa y mandaba otra.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.inventory_effect import (
    CURRENT_SNAPSHOT,
    EFFECT_LABELS,
    HISTORICAL_REPLAY,
    INFORMATIONAL,
    NO_INVENTORY,
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
) -> dict[str, dict[str, Any]]:
    response = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/inventory-effects",
        json={"column_mappings": mapeos},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return {hoja["context_id"]: hoja for hoja in response.json()}


@pytest.mark.asyncio
class TestEfectoPropuestoPorHoja:
    async def test_cada_hoja_trae_su_default_y_sus_opciones(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        hojas = await _efectos(client, auth_headers, archivo, _MAPEO_COMPLETO)

        ventas = hojas[_VENTAS]
        assert ventas["default"] == INFORMATIONAL
        assert [o["value"] for o in ventas["options"]] == [
            INFORMATIONAL,
            HISTORICAL_REPLAY,
            NO_INVENTORY,
        ]

        catalogo = hojas[_CATALOGO]
        assert catalogo["default"] == CURRENT_SNAPSHOT
        # Un saldo no es una secuencia: no hay historia que aplicar.
        assert HISTORICAL_REPLAY not in [o["value"] for o in catalogo["options"]]

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

    async def test_sin_cantidad_mapeada_la_hoja_no_puede_aplicar_su_historia(
        self, client: AsyncClient, auth_headers: dict[str, Any], archivo: UploadedFile
    ) -> None:
        """El caso que obliga a recalcular contra el mapeo BORRADOR y no una vez.

        Es la misma hoja del mismo archivo: lo único que cambia es que el usuario
        todavía no mapeó `cantidad`. Sin unidades no hay inventario que mover, y
        ofrecer "aplicar la historia" ahí sería ofrecer algo que no pasa.
        """
        sin_cantidad = [
            m for m in _MAPEO_COMPLETO if m["target_field"] != "quantity"
        ]
        hojas = await _efectos(client, auth_headers, archivo, sin_cantidad)

        ventas = hojas[_VENTAS]
        assert ventas["default"] == NO_INVENTORY
        assert [o["value"] for o in ventas["options"]] == [NO_INVENTORY]

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

        assert hojas[_VENTAS]["default"] == NO_INVENTORY

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
