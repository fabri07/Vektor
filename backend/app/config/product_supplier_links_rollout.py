"""Bloque 2 — vínculos Producto↔Proveedor declarados en catálogo (Tienda →
proveedor, ``product_supplier_links``).

**Graduada (2026-09-13):** esto era una compuerta de rollout por tenant
(lista vacía ⇒ nadie habilitado). Probada en ASTERIA y decidido por el
usuario que sea el comportamiento por default para TODOS los tenants,
existentes y futuros — no una lista que haya que mantener a mano en Railway
cada vez que se crea una cuenta nueva. `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS`
queda sin uso (no se borra el campo de `Settings` ni la migración: borrarlos
no aporta nada y una var de entorno vieja en Railway no rompe nada al no
leerse más).
"""

from __future__ import annotations

import uuid

ENV_VAR = "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS"


def product_supplier_links_enabled_for(tenant_id: uuid.UUID | str) -> bool:  # noqa: ARG001
    """Siempre ``True`` — ver docstring del módulo (compuerta graduada).

    Se conserva la función (no se inlinea `True` en los call sites) porque
    sigue siendo el único punto que ``import_capabilities.py`` y
    ``ingestion_import_service._add_product`` consultan; si el día de mañana
    hiciera falta volver a gatear esto por tenant, hay un solo lugar que tocar.
    """
    return True
