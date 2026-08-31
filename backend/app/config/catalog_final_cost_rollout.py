"""Bloque 3A — compuerta de rollout por tenant de "compra+envío como costo final".

Mismo criterio que ``purchase_cost_rollout.py``: sin staging propio, el rollout
por tenant ES el staging. Con la lista vacía (default), un catálogo con
"Precio de compra" y "compra+envío" sigue resolviendo `unit_cost_ars` como
hasta ahora (orden no determinístico por columna del archivo, comportamiento
histórico) — habilitar el tenant es lo que hace que "compra+envío" gane
siempre, porque cambia un número (costo/margen) sin que el usuario lo pida
explícitamente por mapeo manual.
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS"


def catalog_final_cost_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿Este tenant tiene habilitado que "compra+envío" gane como unit_cost_ars?

    Lista vacía (el default) ⇒ ``False`` para todos. Un mapeo EXPLÍCITO a
    ``unit_cost_ars`` no pasa por acá — ese siempre gana, con o sin flag.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False

    configurados = get_settings().CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
