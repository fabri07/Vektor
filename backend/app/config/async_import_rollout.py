"""E6c-3 — compuerta de rollout de la importación ASÍNCRONA, por tenant.

Lista vacía (default) = nadie habilitado: el frontend sigue usando `/confirm` y
nada cambia. Mismo criterio que las otras cuatro compuertas del programa.

Lo que esta compuerta gatea, y lo que NO
-----------------------------------------
Gatea **únicamente el registro de intentos nuevos**. El publicador
(``jobs.publish_import_orders``) y el recuperador (``jobs.recover_import_attempts``)
corren siempre, para todos.

No es un detalle de implementación: es lo que hace que apagar la compuerta sea
seguro. Un intento ya registrado tiene su orden commiteada, y apagar el publicador
la dejaría sin entregar — el usuario vería una importación "pendiente" que nadie
va a ejecutar nunca. Frenar lo nuevo no puede significar abandonar lo que ya
entró.
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "ASYNC_IMPORT_ROLLOUT_TENANT_IDS"


def async_import_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿Este tenant puede REGISTRAR importaciones asíncronas?"""
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False
    configurados = get_settings().ASYNC_IMPORT_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
