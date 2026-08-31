"""Bloque 2 — compuerta de rollout por tenant de los vínculos Producto↔Proveedor
declarados en catálogo (Tienda → proveedor, ``product_supplier_links``).

Mismo criterio que ``purchase_cost_rollout.py`` (F-H6.c/d): sin staging propio,
el rollout por tenant ES el staging. Lista vacía (default) ⇒ nadie habilitado,
comportamiento idéntico al de hoy — "Tienda" sigue guardándose como
``custom_fields["marca"]`` y nunca crea un ``Supplier``. Reusa el parseo de
``purchase_cost_rollout.py`` en vez de duplicarlo: dos parsers de la misma forma
de variable (csv/JSON array/lista) podrían divergir en un borde y la duda
siempre tiene que resolver al lado seguro (no habilitar).
"""

from __future__ import annotations

import uuid

from app.config.purchase_cost_rollout import normalizar_tenant_id

ENV_VAR = "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS"


def product_supplier_links_enabled_for(tenant_id: uuid.UUID | str) -> bool:
    """¿Este tenant tiene habilitado Tienda→proveedor (Bloque 2)?

    Lista vacía (el default) ⇒ ``False`` para todos. Un ``tenant_id`` que no es
    un UUID también da ``False``: no habilitar es el lado seguro de la duda.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    normalizado = normalizar_tenant_id(tenant_id)
    if normalizado is None:
        return False

    configurados = get_settings().PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS
    return any(normalizar_tenant_id(entrada) == normalizado for entrada in configurados)
