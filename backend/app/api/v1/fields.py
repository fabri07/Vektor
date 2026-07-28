"""Field definitions endpoints — vertical base + tenant customization."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user, require_modify_access
from app.application.services import field_definition_service as svc
from app.domain.verticals import Vertical, parse_vertical
from app.persistence.db.session import get_db_session
from app.persistence.models.field_definitions import VerticalFieldDefinition
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.fields import (
    CreateCustomFieldRequest,
    FieldChangeLogResponse,
    FieldDefinitionResponse,
    ToggleFieldRequest,
    UpdateCustomFieldRequest,
)

router = APIRouter()


async def _get_vertical_code(tenant_id: UUID, session: AsyncSession) -> Vertical:
    """Vertical del tenant dueño de la request.

    Sin `BusinessProfile` → 404: un tenant logueado sin perfil es un estado roto
    real (los signups sociales viejos), y devolverle el set de campos de otro
    rubro lo enmascara.
    """
    from app.persistence.repositories.business_profile_repository import BusinessProfileRepository

    profile = await BusinessProfileRepository(session).get_by_tenant_id(tenant_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="business_profile_not_found"
        )
    return parse_vertical(profile.vertical_code)


async def _resolve_data_type(
    field_key: str,
    entity_type: str,
    override_data_type: str | None,
    vertical_code: Vertical,
    session: AsyncSession,
) -> str:
    if override_data_type:
        return override_data_type
    base = (
        await session.execute(
            select(VerticalFieldDefinition).where(
                VerticalFieldDefinition.vertical_code == vertical_code,
                VerticalFieldDefinition.field_key == field_key,
                VerticalFieldDefinition.entity_type == entity_type,
            )
        )
    ).scalar_one_or_none()
    return base.data_type if base else "text"


@router.get("", response_model=list[FieldDefinitionResponse], summary="Get field definitions")
async def get_definitions(
    entity_type: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    vertical_code = await _get_vertical_code(tenant.tenant_id, session)
    return await svc.get_merged_definitions(
        session,
        tenant_id=tenant.tenant_id,
        vertical_code=vertical_code,
        entity_type=entity_type,
    )


@router.post("", response_model=FieldDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_field(
    body: CreateCustomFieldRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    field = await svc.create_custom_field(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        entity_type=body.entity_type,
        field_key=body.field_key,
        label=body.label,
        data_type=body.data_type,
        enum_options=[o.model_dump() for o in body.enum_options] if body.enum_options else None,
        display_order=body.display_order,
    )
    return {
        "field_key": field.field_key,
        "entity_type": field.entity_type,
        "label": field.override_label or field.field_key,
        "data_type": field.data_type or "text",
        "enum_options": field.override_enum_options,
        "is_required": field.override_required or False,
        "display_order": field.display_order,
        "is_base_field": False,
        "affects_scoring": False,
    }


@router.patch("/{field_key}", response_model=FieldDefinitionResponse)
async def update_field(
    field_key: str,
    body: UpdateCustomFieldRequest,
    entity_type: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    vertical_code = await _get_vertical_code(tenant.tenant_id, session)
    field = await svc.update_custom_field(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        field_key=field_key,
        entity_type=entity_type,
        vertical_code=vertical_code,
        label=body.label,
        override_required=body.override_required,
        override_enum_options=[o.model_dump() for o in body.override_enum_options]
        if body.override_enum_options
        else None,
        display_order=body.display_order,
    )
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campo no encontrado.")
    return {
        "field_key": field.field_key,
        "entity_type": field.entity_type,
        "label": field.override_label or field_key,
        "data_type": field.data_type or "text",
        "enum_options": field.override_enum_options,
        "is_required": field.override_required or False,
        "display_order": field.display_order,
        "is_base_field": field.is_base_field,
        "affects_scoring": False,
    }


@router.post("/{field_key}/toggle", response_model=FieldDefinitionResponse)
async def toggle_field(
    field_key: str,
    body: ToggleFieldRequest,
    entity_type: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    vertical_code = await _get_vertical_code(tenant.tenant_id, session)
    field = await svc.toggle_field(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        field_key=field_key,
        entity_type=entity_type,
        vertical_code=vertical_code,
        enabled=body.enabled,
    )
    data_type = await _resolve_data_type(
        field_key, entity_type, field.data_type, vertical_code, session
    )
    return {
        "field_key": field.field_key,
        "entity_type": field.entity_type,
        "label": field.override_label or field_key,
        "data_type": data_type,
        "enum_options": field.override_enum_options,
        "is_required": field.override_required or False,
        "display_order": field.display_order,
        "is_base_field": field.is_base_field,
        "affects_scoring": False,
    }


@router.post("/{field_key}/undo", status_code=status.HTTP_200_OK)
async def undo_field_change(
    field_key: str,
    entity_type: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_modify_access),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    undone = await svc.undo_last_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        field_key=field_key,
        entity_type=entity_type,
    )
    if not undone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cambios anteriores para deshacer.",
        )
    return {"undone": True}


@router.get("/history", response_model=list[FieldChangeLogResponse])
async def get_history(
    limit: int = Query(default=50, ge=1, le=200),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Any]:
    return await svc.get_change_history(session, tenant_id=tenant.tenant_id, limit=limit)
