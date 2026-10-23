"""M·P4 — compuerta de rollout, por tenant, de MOSTRAR el costo promedio.

Lista vacía (default) = nadie habilitado: los campos nuevos de la API salen en
``null`` y la pantalla queda como antes. El costo promedio se sigue calculando
para todos (M·P2/M·P3); esto gatea sólo que se vea, para que el dueño revise el
informe de sombra de cada cuenta antes de mostrárselo.
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS"


def avg_cost_display_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿Este tenant ve el costo promedio?"""
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False
    configurados = get_settings().AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
