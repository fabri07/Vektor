"""Service: vertical field definitions + tenant custom fields + undo log."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.field_definitions import (
    TenantCustomFieldDefinition,
    TenantFieldChangeLog,
    VerticalFieldDefinition,
)

log = get_logger(__name__)

VALID_ACTIONS = frozenset({"enabled", "disabled", "modified", "created", "undo"})


def _snapshot(field: TenantCustomFieldDefinition) -> dict[str, Any]:
    return {
        "field_key": field.field_key,
        "entity_type": field.entity_type,
        "data_type": field.data_type,
        "override_label": field.override_label,
        "override_required": field.override_required,
        "override_enum_options": field.override_enum_options,
        "is_enabled": field.is_enabled,
        "display_order": field.display_order,
    }


async def get_merged_definitions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vertical_code: str,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return base vertical fields merged with tenant overrides. Disabled fields excluded.

    Note: custom_fields values on transactions are NOT validated against these definitions
    at write time (MVP). Validation against field keys, types and enum options is a
    future enhancement — add a validate_custom_fields(tenant_id, entity_type, data) helper
    and call it from sales/expenses/products create/update endpoints.
    """
    base_q = select(VerticalFieldDefinition).where(
        VerticalFieldDefinition.vertical_code == vertical_code
    )
    if entity_type:
        base_q = base_q.where(VerticalFieldDefinition.entity_type == entity_type)
    base_rows = (await session.execute(base_q)).scalars().all()

    tenant_q = select(TenantCustomFieldDefinition).where(
        TenantCustomFieldDefinition.tenant_id == tenant_id
    )
    if entity_type:
        tenant_q = tenant_q.where(TenantCustomFieldDefinition.entity_type == entity_type)
    tenant_rows = (await session.execute(tenant_q)).scalars().all()

    tenant_index: dict[tuple[str, str], TenantCustomFieldDefinition] = {
        (r.field_key, r.entity_type): r for r in tenant_rows
    }

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for base in base_rows:
        key = (base.field_key, base.entity_type)
        override = tenant_index.get(key)
        if override and not override.is_enabled:
            seen.add(key)
            continue
        entry: dict[str, Any] = {
            "field_key": base.field_key,
            "entity_type": base.entity_type,
            "label": (
                override.override_label if override and override.override_label else base.label
            ),
            "data_type": base.data_type,
            "enum_options": (
                override.override_enum_options
                if override and override.override_enum_options is not None
                else base.enum_options
            ),
            "is_required": (
                override.override_required
                if override and override.override_required is not None
                else base.is_required
            ),
            "display_order": override.display_order if override else base.display_order,
            "is_base_field": True,
            "affects_scoring": base.affects_scoring,
        }
        result.append(entry)
        seen.add(key)

    for tenant_field in tenant_rows:
        key = (tenant_field.field_key, tenant_field.entity_type)
        if key in seen:
            continue
        if not tenant_field.is_enabled:
            continue
        result.append(
            {
                "field_key": tenant_field.field_key,
                "entity_type": tenant_field.entity_type,
                "label": tenant_field.override_label or tenant_field.field_key,
                "data_type": tenant_field.data_type or "text",
                "enum_options": tenant_field.override_enum_options,
                "is_required": tenant_field.override_required or False,
                "display_order": tenant_field.display_order,
                "is_base_field": False,
                "affects_scoring": False,
            }
        )

    result.sort(key=lambda x: (x["entity_type"], x["display_order"]))
    return result


async def ensure_custom_field_exists(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_type: str,
    field_key: str,
    label: str,
) -> None:
    """Crea una definición de campo personalizado si no existe. Sin commit — el caller lo maneja.

    Usado en el flujo de confirmación de ingesta cuando el usuario mapea una columna
    a custom_field:{key}, para que el campo aparezca en el ERD y en FieldDefinitionsPanel.
    """
    existing = (
        await session.execute(
            select(TenantCustomFieldDefinition).where(
                TenantCustomFieldDefinition.tenant_id == tenant_id,
                TenantCustomFieldDefinition.entity_type == entity_type,
                TenantCustomFieldDefinition.field_key == field_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Gana el label que ya está, a propósito: `/settings` deja renombrar un
        # campo propio, y pisarlo en cada import devolvería el encabezado del
        # archivo encima del nombre que la persona eligió. El header es el valor
        # INICIAL, no la fuente de verdad permanente.
        return

    now = datetime.now(UTC)
    session.add(
        TenantCustomFieldDefinition(
            id=uuid.uuid4(),  # explícito para compatibilidad con SQLite en tests
            tenant_id=tenant_id,
            entity_type=entity_type,
            field_key=field_key,
            override_label=label,
            data_type="text",
            is_enabled=True,
            is_base_field=False,
            display_order=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    log.info(
        "custom_field_ensured",
        tenant_id=str(tenant_id),
        entity_type=entity_type,
        field_key=field_key,
    )


async def create_custom_field(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    entity_type: str,
    field_key: str,
    label: str,
    data_type: str = "text",
    enum_options: list[dict[str, Any]] | None = None,
    display_order: int = 0,
) -> TenantCustomFieldDefinition:
    now = datetime.now(UTC)
    field = TenantCustomFieldDefinition(
        tenant_id=tenant_id,
        entity_type=entity_type,
        field_key=field_key,
        override_label=label,
        data_type=data_type,
        override_enum_options=enum_options,
        is_enabled=True,
        is_base_field=False,
        display_order=display_order,
        created_at=now,
        updated_at=now,
    )
    session.add(field)
    await session.flush()
    new_state = _snapshot(field)
    session.add(
        TenantFieldChangeLog(
            tenant_id=tenant_id,
            field_key=field_key,
            entity_type=entity_type,
            action="created",
            previous_state=None,
            new_state=new_state,
            changed_by=user_id,
            changed_at=now,
        )
    )
    await session.commit()
    log.info("custom_field_created", tenant_id=str(tenant_id), field_key=field_key)
    return field


async def toggle_field(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    field_key: str,
    entity_type: str,
    vertical_code: str,
    enabled: bool,
) -> TenantCustomFieldDefinition:
    now = datetime.now(UTC)
    result = await session.execute(
        select(TenantCustomFieldDefinition).where(
            TenantCustomFieldDefinition.tenant_id == tenant_id,
            TenantCustomFieldDefinition.field_key == field_key,
            TenantCustomFieldDefinition.entity_type == entity_type,
        )
    )
    field = result.scalar_one_or_none()

    if field is None:
        base = await session.execute(
            select(VerticalFieldDefinition).where(
                VerticalFieldDefinition.vertical_code == vertical_code,
                VerticalFieldDefinition.field_key == field_key,
                VerticalFieldDefinition.entity_type == entity_type,
            )
        )
        base_field = base.scalar_one_or_none()
        field = TenantCustomFieldDefinition(
            tenant_id=tenant_id,
            vertical_field_id=base_field.id if base_field else None,
            entity_type=entity_type,
            field_key=field_key,
            is_enabled=enabled,
            is_base_field=base_field is not None,
            display_order=base_field.display_order if base_field else 0,
            created_at=now,
            updated_at=now,
        )
        session.add(field)
        prev_state = None
    else:
        prev_state = _snapshot(field)
        field.is_enabled = enabled
        field.updated_at = now

    await session.flush()
    session.add(
        TenantFieldChangeLog(
            tenant_id=tenant_id,
            field_key=field_key,
            entity_type=entity_type,
            action="enabled" if enabled else "disabled",
            previous_state=prev_state,
            new_state=_snapshot(field),
            changed_by=user_id,
            changed_at=now,
        )
    )
    await session.commit()
    return field


async def undo_last_change(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    field_key: str,
    entity_type: str,
) -> bool:
    """Restore previous state from the most recent change log entry. Returns True if undone."""
    last_log = await session.execute(
        select(TenantFieldChangeLog)
        .where(
            TenantFieldChangeLog.tenant_id == tenant_id,
            TenantFieldChangeLog.field_key == field_key,
            TenantFieldChangeLog.entity_type == entity_type,
        )
        .order_by(TenantFieldChangeLog.changed_at.desc())
        .limit(1)
    )
    entry = last_log.scalar_one_or_none()
    if entry is None or entry.previous_state is None:
        return False

    prev = entry.previous_state
    now = datetime.now(UTC)

    field = (
        await session.execute(
            select(TenantCustomFieldDefinition).where(
                TenantCustomFieldDefinition.tenant_id == tenant_id,
                TenantCustomFieldDefinition.field_key == field_key,
                TenantCustomFieldDefinition.entity_type == entity_type,
            )
        )
    ).scalar_one_or_none()

    if field is None:
        return False

    current_state = _snapshot(field)
    field.override_label = prev.get("override_label")
    field.override_required = prev.get("override_required")
    field.override_enum_options = prev.get("override_enum_options")
    field.is_enabled = prev.get("is_enabled", True)
    field.display_order = prev.get("display_order", 0)
    field.updated_at = now

    session.add(
        TenantFieldChangeLog(
            tenant_id=tenant_id,
            field_key=field_key,
            entity_type=entity_type,
            action="undo",
            previous_state=current_state,
            new_state=prev,
            changed_by=user_id,
            changed_at=now,
        )
    )
    await session.commit()
    log.info("field_undo", tenant_id=str(tenant_id), field_key=field_key)
    return True


async def update_custom_field(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    field_key: str,
    entity_type: str,
    vertical_code: str,
    label: str | None = None,
    override_required: bool | None = None,
    override_enum_options: list[dict[str, Any]] | None = None,
    display_order: int | None = None,
) -> TenantCustomFieldDefinition | None:
    field = (
        await session.execute(
            select(TenantCustomFieldDefinition).where(
                TenantCustomFieldDefinition.tenant_id == tenant_id,
                TenantCustomFieldDefinition.field_key == field_key,
                TenantCustomFieldDefinition.entity_type == entity_type,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)

    if field is None:
        # First-time customization of a base field — create override from vertical definition
        base = (
            await session.execute(
                select(VerticalFieldDefinition).where(
                    VerticalFieldDefinition.vertical_code == vertical_code,
                    VerticalFieldDefinition.field_key == field_key,
                    VerticalFieldDefinition.entity_type == entity_type,
                )
            )
        ).scalar_one_or_none()
        if base is None:
            return None  # field not found anywhere
        field = TenantCustomFieldDefinition(
            tenant_id=tenant_id,
            vertical_field_id=base.id,
            entity_type=entity_type,
            field_key=field_key,
            override_label=label,
            override_required=override_required,
            override_enum_options=override_enum_options,
            is_enabled=True,
            is_base_field=True,
            display_order=display_order if display_order is not None else base.display_order,
            created_at=now,
            updated_at=now,
        )
        session.add(field)
        await session.flush()
        session.add(
            TenantFieldChangeLog(
                tenant_id=tenant_id,
                field_key=field_key,
                entity_type=entity_type,
                action="created",
                previous_state=None,
                new_state=_snapshot(field),
                changed_by=user_id,
                changed_at=now,
            )
        )
        await session.commit()
        log.info("base_field_override_created", tenant_id=str(tenant_id), field_key=field_key)
        return field

    prev_state = _snapshot(field)

    if label is not None:
        field.override_label = label
    if override_required is not None:
        field.override_required = override_required
    if override_enum_options is not None:
        field.override_enum_options = override_enum_options
    if display_order is not None:
        field.display_order = display_order
    field.updated_at = now

    session.add(
        TenantFieldChangeLog(
            tenant_id=tenant_id,
            field_key=field_key,
            entity_type=entity_type,
            action="modified",
            previous_state=prev_state,
            new_state=_snapshot(field),
            changed_by=user_id,
            changed_at=now,
        )
    )
    await session.commit()
    log.info("custom_field_updated", tenant_id=str(tenant_id), field_key=field_key)
    return field


async def get_change_history(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    limit: int = 50,
) -> list[TenantFieldChangeLog]:
    result = await session.execute(
        select(TenantFieldChangeLog)
        .where(TenantFieldChangeLog.tenant_id == tenant_id)
        .order_by(TenantFieldChangeLog.changed_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
