"""El catálogo de campos publica los destinos en OTRA sección.

La columna "Tienda" de un catálogo de productos puede declarar el proveedor del
artículo. El importador ya sabe ejecutarlo —`_store_mapped_as_supplier` +
`link_product_to_declared_supplier`, con la tabla `product_supplier_links` y sus
tests—, pero `/ingestion/field-catalog` devolvía sólo `CANONICAL_FIELDS`, así que
el `<select>` del mapeador no tenía la opción y no había forma de elegirlo.

Dos invariantes que estos tests fijan, y que son la razón de que la lista sea
SEPARADA de `fields`:

1. Un cruzado nunca cubre un requerido de esta hoja (igual que `custom_field:`).
2. Sólo se publica lo que el importador REALMENTE escribe, no todo lo que el
   confirm acepta sin rechazar. Ofrecer un destino que se acepta y se tira es
   peor que no ofrecerlo: el dato desaparece sin que la pantalla se queje.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _catalogo(client: AsyncClient, headers: dict[str, Any]) -> dict[str, Any]:
    res = await client.get("/api/v1/ingestion/field-catalog", headers=headers)
    assert res.status_code == 200
    return res.json()


async def test_product_publica_proveedor_nombre_como_destino_cruzado(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    catalogo = await _catalogo(client, auth_headers)

    cruzados = catalogo["product"]["cross_fields"]
    assert [c["value"] for c in cruzados] == ["supplier:name"]
    entrada = cruzados[0]
    # La etiqueta lleva la entidad destino adelante: sin eso, la opción diría
    # sólo "Nombre" y se leería como un campo del producto.
    assert entrada["label"] == "Proveedor — Nombre"
    assert entrada["entity"] == "supplier"


async def test_un_cruzado_no_cuenta_como_campo_de_la_hoja(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    """Si `supplier:name` apareciera en `fields`, `coversRequired` lo tomaría
    como si cubriera un requerido del producto. Va aparte por eso."""
    catalogo = await _catalogo(client, auth_headers)

    valores = {f["value"] for f in catalogo["product"]["fields"]}
    assert "supplier:name" not in valores
    assert "supplier:name" not in set(catalogo["product"]["required"])


async def test_no_se_publica_lo_que_el_importador_descarta(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    """`CROSS_ENTITY_TARGETS` permite mandar `sale → customer:*` y
    `expense → product:*`, pero `insert_confirmed_data` los filtra y los cuenta
    en `targets_cruzados_descartados` (F-D no entregada). Mientras eso sea así,
    la pantalla no puede ofrecerlos.

    El día que se implementen, se agregan a `CROSS_ENTITY_TARGETS_IMPLEMENTED` y
    este test es el que hay que actualizar — que es exactamente la revisión que
    ese cambio merece.
    """
    catalogo = await _catalogo(client, auth_headers)

    for entidad in ("sale", "expense", "customer", "supplier"):
        assert catalogo[entidad]["cross_fields"] == [], entidad


async def test_todo_cruzado_publicado_tiene_el_formato_que_espera_el_confirm(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    """`parse_target` sólo reconoce `"{entidad}:{campo}"` con la entidad en
    `CROSS_ENTITY_PREFIXES`. Un `value` con otra forma llegaría al confirm como
    canónico y sería rechazado por campo inexistente."""
    from app.application.services.column_mapping_service import parse_target

    catalogo = await _catalogo(client, auth_headers)

    for entidad, datos in catalogo.items():
        for cruzado in datos["cross_fields"]:
            parsed = parse_target(cruzado["value"])
            assert parsed.kind == "cross", f"{entidad}: {cruzado['value']}"
            assert parsed.entity == cruzado["entity"]
