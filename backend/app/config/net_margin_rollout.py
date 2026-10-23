"""M·3-1 — compuerta de rollout, por tenant, del subscore de margen con el NETO.

Lista vacía (default) = nadie: el subscore de margen sigue con la fórmula previa
(ingreso − costo vendido − gastos fijos recurrentes, con el fallback del
onboarding). Habilitado, lee el margen neto del último mes cerrado de
FactsService (el mismo número de la tarjeta y del chat). El dueño habilita cada
cuenta después de mirar la sombra de ``scripts/diag_margen.py``.
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "NET_MARGIN_ROLLOUT_TENANT_IDS"


def net_margin_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿El subscore de margen de este tenant sale del margen neto del mes cerrado?"""
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False
    configurados = get_settings().NET_MARGIN_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
