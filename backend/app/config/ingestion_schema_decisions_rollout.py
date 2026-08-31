"""Bloque 5 — compuerta de rollout por tenant de la persistencia de decisiones
de mapeo por esquema (huella de archivo/hoja).

Mismo criterio que los blogues anteriores: lista vacía (default) = nadie
habilitado, comportamiento idéntico al de hoy (no se persiste ni se recupera
nada — cada confirm/relectura vuelve a pedir las mismas decisiones).
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS"


def ingestion_schema_decisions_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False

    configurados = get_settings().INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
