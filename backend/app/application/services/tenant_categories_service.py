"""Categorías custom por tenant (gasto y producto), persistidas sin tabla nueva.

Se guardan en ``business_profiles.custom_fields`` (JSONB ya existente):
  - ``expense_categories``: list[str] (labels de gasto "Otros" definidos por el usuario).
  - ``product_categories``: list[{code, label}] (categorías de producto del tenant).

Dedup insensible a mayúsculas, espacios y acentos. Reaparecen en los desplegables
de carga manual sin ninguna pantalla de administración.
"""

from __future__ import annotations

import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.business import BusinessProfile

_EXPENSE_KEY = "expense_categories"
_PRODUCT_KEY = "product_categories"


def normalize_label(label: str) -> str:
    """Clave de dedup: sin acentos, minúsculas, espacios colapsados."""
    nfkd = unicodedata.normalize("NFKD", label)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.lower().split())


def _slug(label: str) -> str:
    base = normalize_label(label).replace(" ", "_").upper()
    safe = "".join(c for c in base if c.isalnum() or c == "_")
    return f"CUSTOM_{safe}"[:100]


async def _get_profile(
    session: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False
) -> BusinessProfile | None:
    stmt = select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
    if for_update:
        # Bloquea la fila durante el upsert: evita lost-updates entre dos altas
        # concurrentes que leen/escriben la misma lista en custom_fields (JSONB).
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _write_custom_fields(profile: BusinessProfile, key: str, value: Any) -> None:
    # Reasignar un dict nuevo para que SQLAlchemy marque el JSONB como modificado.
    cf = dict(profile.custom_fields or {})
    cf[key] = value
    profile.custom_fields = cf


# ── Gasto ──────────────────────────────────────────────────────────────────────


async def list_expense_categories(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    profile = await _get_profile(session, tenant_id)
    if profile is None:
        return []
    raw = (profile.custom_fields or {}).get(_EXPENSE_KEY) or []
    return [str(x) for x in raw if str(x).strip()]


async def add_expense_category(
    session: AsyncSession, tenant_id: uuid.UUID, label: str
) -> bool:
    """Agrega una categoría de gasto del tenant (idempotente). True si se agregó."""
    label = (label or "").strip()[:50]
    if not label:
        return False
    profile = await _get_profile(session, tenant_id, for_update=True)
    if profile is None:
        return False
    raw = (profile.custom_fields or {}).get(_EXPENSE_KEY) or []
    current = [str(x) for x in raw if str(x).strip()]
    seen = {normalize_label(x) for x in current}
    if normalize_label(label) in seen:
        return False
    _write_custom_fields(profile, _EXPENSE_KEY, [*current, label])
    return True


# ── Producto ───────────────────────────────────────────────────────────────────


async def list_product_categories(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[dict[str, str]]:
    profile = await _get_profile(session, tenant_id)
    if profile is None:
        return []
    raw = (profile.custom_fields or {}).get(_PRODUCT_KEY) or []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("code") and item.get("label"):
            out.append({"code": str(item["code"]), "label": str(item["label"])})
    return out


async def add_product_category(
    session: AsyncSession, tenant_id: uuid.UUID, label: str
) -> dict[str, str] | None:
    """Agrega (o devuelve la existente) una categoría de producto del tenant."""
    label = (label or "").strip()[:100]
    if not label:
        return None
    profile = await _get_profile(session, tenant_id, for_update=True)
    if profile is None:
        return None
    current = _read_product_categories(profile)
    norm = normalize_label(label)
    for c in current:
        if normalize_label(c["label"]) == norm:
            return c
    new = {"code": _slug(label), "label": label}
    _write_custom_fields(profile, _PRODUCT_KEY, [*current, new])
    return new


def _read_product_categories(profile: BusinessProfile) -> list[dict[str, str]]:
    raw = (profile.custom_fields or {}).get(_PRODUCT_KEY) or []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("code") and item.get("label"):
            out.append({"code": str(item["code"]), "label": str(item["label"])})
    return out


async def resolve_custom_product_category(
    session: AsyncSession, tenant_id: uuid.UUID, raw: str
) -> dict[str, str] | None:
    """Devuelve {code,label} si ``raw`` matchea una categoría custom del tenant."""
    norm = normalize_label(raw)
    for c in await list_product_categories(session, tenant_id):
        if normalize_label(c["label"]) == norm:
            return c
    return None
