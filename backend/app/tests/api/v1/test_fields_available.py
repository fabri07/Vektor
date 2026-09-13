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


async def test_un_libre_que_apunta_al_mismo_lugar_que_la_evidencia_no_duplica(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Si `purchase_base_cost` también quedara declarado como
    TenantCustomFieldDefinition, apunta al MISMO lugar (`custom_fields.
    purchase_base_cost`) que el descriptor estático — ahí sí corresponde
    deduplicar: no son dos datos, es la misma clave JSON."""
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
    assert ids.count("product:custom_fields.purchase_base_cost") == 1


async def test_un_libre_que_comparte_nombre_con_una_columna_real_no_se_pierde(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Bug real encontrado en la revisión del primer commit: `product.name` (la
    columna real) y un `custom_field:name` que un tenant declaró sin saber que
    colisionaba son datos DISTINTOS en lugares distintos. Deduplicar por
    field_key los fusionaba y uno desaparecía en silencio."""
    from app.application.services.field_definition_service import create_custom_field

    await create_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        entity_type="product",
        field_key="name",
        label="Nombre interno (otro dato)",
    )

    campos = await _disponibles(client, auth_headers)
    por_id = {c["field_id"]: c for c in campos}

    assert "product:name" in por_id
    assert por_id["product:name"]["value_path"] == "name"
    assert por_id["product:name"]["label"] == "Nombre"  # el canónico no se pisa

    assert "product:custom_fields.name" in por_id
    assert por_id["product:custom_fields.name"]["value_path"] == "custom_fields.name"
    assert por_id["product:custom_fields.name"]["label"] == "Nombre interno (otro dato)"
    assert por_id["product:custom_fields.name"]["origin"] == "additional"


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


async def test_campo_del_rubro_sin_columna_real_vive_en_custom_fields(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Bug real encontrado en la revisión: "Color"/"Estilo" (decoración del
    hogar) son campos del RUBRO (`VerticalFieldDefinition`, is_base_field=True
    al leerlos) pero `Product` no tiene esas columnas — asumir que
    is_base_field==columna real los dejaba apuntando a un atributo que no
    existe. Acá se prueba con "color" contra el vertical del propio tenant
    (kiosco), sin depender del JSON real de decoración del hogar."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.persistence.models.field_definitions import VerticalFieldDefinition

    db_session.add(
        VerticalFieldDefinition(
            id=_uuid.uuid4(),
            vertical_code="kiosco_almacen",
            entity_type="product",
            field_key="color",
            label="Color",
            data_type="text",
            is_required=False,
            display_order=1,
            context_weight=0.0,
            affects_scoring=False,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["color"]["value_path"] == "custom_fields.color"
    assert por_key["color"]["origin"] == "canonical"  # es propio del rubro, no libre del tenant
    assert por_key["color"]["field_id"] == "product:custom_fields.color"


async def test_override_de_un_campo_del_rubro_no_lo_pisa_el_estatico(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un tenant puede renombrar "Color" a "Tono" (`update_custom_field`, la
    misma vía que usa /settings). El catálogo de lectura tiene que mostrar ESE
    label, no el estático — pero solo cuando es un override REAL del campo del
    rubro (`is_base_field=True`), nunca por compartir field_key nomás."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.persistence.models.field_definitions import VerticalFieldDefinition

    db_session.add(
        VerticalFieldDefinition(
            id=_uuid.uuid4(),
            vertical_code="kiosco_almacen",
            entity_type="product",
            field_key="color",
            label="Color",
            data_type="text",
            is_required=False,
            display_order=1,
            context_weight=0.0,
            affects_scoring=False,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    from app.application.services.field_definition_service import update_custom_field

    await update_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        field_key="color",
        entity_type="product",
        vertical_code="kiosco_almacen",
        label="Tono",
    )

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["color"]["label"] == "Tono"
    assert por_key["color"]["value_path"] == "custom_fields.color"


async def test_deshabilitado_sigue_siendo_consultable_en_historico(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """`get_merged_definitions` (contrato de EDICIÓN) excluye deshabilitados a
    propósito — apagar un campo para nuevas cargas no puede borrar el pasado.
    El catálogo de LECTURA los sigue mostrando, marcados."""
    from app.application.services.field_definition_service import (
        create_custom_field,
        toggle_field,
    )

    await create_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        entity_type="product",
        field_key="atributo_viejo",
        label="Atributo viejo",
    )
    await toggle_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        field_key="atributo_viejo",
        entity_type="product",
        vertical_code="kiosco_almacen",
        enabled=False,
    )

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert "atributo_viejo" in por_key  # sigue consultable
    assert por_key["atributo_viejo"]["enabled_for_new_entries"] is False


async def test_recuperado_por_backfill_arranca_de_solo_lectura(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El plan es explícito: "las evidencias y recuperaciones históricas
    empiezan como solo lectura". Simula lo que hace el backfill (crea la
    definición + un TenantFieldChangeLog con `source` del script) sin
    depender de Postgres — la marca de origen es la misma que usa el script
    real."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.persistence.models.field_definitions import (
        TenantCustomFieldDefinition,
        TenantFieldChangeLog,
    )

    now = datetime.now(UTC)
    db_session.add(
        TenantCustomFieldDefinition(
            id=_uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            entity_type="product",
            field_key="marca",
            override_label="Marca",
            data_type="text",
            is_enabled=True,
            is_base_field=False,
            display_order=0,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        TenantFieldChangeLog(
            id=_uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            field_key="marca",
            entity_type="product",
            action="created",
            previous_state=None,
            new_state={
                "field_key": "marca",
                "entity_type": "product",
                "source": "backfill_field_definitions_from_history",
            },
            changed_by=None,
            changed_at=now,
        )
    )
    await db_session.commit()

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["marca"]["editable"] is False

    # Un humano lo edita después (misma vía que /settings) — deja de estar
    # "sin revisar": el plan pide que arranque de solo lectura, no que quede
    # así para siempre una vez que alguien lo tocó.
    from app.application.services.field_definition_service import update_custom_field

    await update_custom_field(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_tenant.tenant_id,
        field_key="marca",
        entity_type="product",
        vertical_code="kiosco_almacen",
        label="Marca del producto",
    )

    campos = await _disponibles(client, auth_headers)
    por_key = {c["field_key"]: c for c in campos}
    assert por_key["marca"]["editable"] is True
    assert por_key["marca"]["label"] == "Marca del producto"


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
