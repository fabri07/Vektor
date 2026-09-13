"""Cambio 1 (docs/plans/conservacion-y-acceso-datos-negocio.md):
`GET /fields/definitions/available` — catálogo de LECTURA que une campos
canónicos, evidencia (F-H6.d) y adicionales del tenant, con identidad estable.
El path real cuelga de `/fields/definitions` (mismo prefix que el resto del
router de campos; el plan proponía `/fields/available` como ilustración, no
como contrato fijo).

Objetivo del test: que los cuatro campos citados explícitamente en el plan
(`unit_cost_ars`, `list_price_ars`, `purchase_base_cost`,
`shipping_percentage`) aparezcan siempre, sin duplicarse contra un campo
adicional declarado con el mismo `field_key`, y que el endpoint respete el
aislamiento por tenant.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


async def _disponibles(
    client: AsyncClient, headers: dict[str, Any], entity_type: str = "product"
) -> list[dict[str, Any]]:
    res = await client.get(
        "/api/v1/fields/definitions/available",
        params={"entity_type": entity_type},
        headers=headers,
    )
    assert res.status_code == 200
    return res.json()


async def test_expone_los_cuatro_campos_citados_por_el_plan(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}

    for key in ("unit_cost_ars", "list_price_ars", "purchase_base_cost", "shipping_percentage"):
        assert key in por_key, f"falta {key}"

    assert por_key["unit_cost_ars"]["origin"] == "canonical"
    assert por_key["unit_cost_ars"]["value_path"] == "unit_cost_ars"

    # Evidencia (F-H6.d): vive en custom_fields, nunca editable desde acá, y
    # el plan exige distinguirla explícitamente de un dato canónico.
    assert por_key["purchase_base_cost"]["origin"] == "evidence"
    assert por_key["purchase_base_cost"]["value_path"] == "custom_fields.purchase_base_cost"
    assert por_key["purchase_base_cost"]["editable"] is False
    assert por_key["shipping_percentage"]["origin"] == "evidence"

    # F10: list_price_ars es informativo — la regla existente queda legible,
    # no una nueva.
    assert "no entra al margen" in (por_key["list_price_ars"]["financial_rule"] or "").lower()


async def test_identidad_estable_no_depende_del_label(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["unit_cost_ars"]["field_id"] == "product:unit_cost_ars"


async def test_un_adicional_declarado_no_duplica_un_canonico(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Si algún día `purchase_base_cost` también quedara registrado como
    TenantCustomFieldDefinition (no pasa hoy, pero el catálogo no debe asumirlo
    para siempre), el field_id estático gana y no aparece dos veces."""
    from app.application.services.field_definition_service import create_custom_field

    await create_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,  # cualquier UUID sirve como changed_by
        entity_type="product",
        field_key="purchase_base_cost",
        label="Costo base (declarado)",
    )

    campos = await _disponibles(client, auth_headers)
    ids = [c["field_id"] for c in campos]
    assert ids.count("product:purchase_base_cost") == 1


async def test_un_campo_libre_del_tenant_aparece_como_additional(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.application.services.field_definition_service import create_custom_field

    await create_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        entity_type="product",
        field_key="col_8",
        label="Columna sin identificar",
    )

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["col_8"]["origin"] == "additional"
    assert por_key["col_8"]["value_path"] == "custom_fields.col_8"


async def test_entidad_no_cubierta_todavia_da_404(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    res = await client.get(
        "/api/v1/fields/definitions/available",
        params={"entity_type": "marketing"},
        headers=auth_headers,
    )
    assert res.status_code == 404


async def test_aislamiento_entre_tenants(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    second_auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.application.services.field_definition_service import create_custom_field

    await create_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        entity_type="product",
        field_key="solo_de_kiosco",
        label="Solo de este tenant",
    )

    campos = await _disponibles(client, auth_headers)
    assert "solo_de_kiosco" in {c["field_key"] for c in campos}

    otros = await _disponibles(client, second_auth_headers)
    assert "solo_de_kiosco" not in {c["field_key"] for c in otros}
    # Los canónicos/evidencia son del catálogo estático: SÍ los ve cualquier
    # tenant, no son propiedad de quien los declaró.
    assert "unit_cost_ars" in {c["field_key"] for c in otros}
