"""Helper compartido del flag de sentinela (proveedores y clientes).

Un registro "sentinela" agrupa filas sin contraparte identificada por tenant:
- Proveedores: ``"No identificado"`` (compras de mercadería sin proveedor).
- Clientes: ``"Local"`` (ventas sin cliente — el kiosco que no sabe quién compró).

Se marca con ``custom_fields["_sentinel"]``. El valor puede llegar como string
``"true"`` (lo que escribe la ingestión) o como booleano JSON ``true`` (si se
edita la fila a mano). Fuente única de verdad para reconocerlo — evita que cada
call site invente su propia comparación frágil. Un índice único parcial por tabla
garantiza UN sentinela por tenant a nivel DB.
"""

SENTINEL_FLAG_KEY = "_sentinel"


def is_flag_true(value: object) -> bool:
    """Predicado genérico de flags de ``custom_fields`` con forma string-"true"/bool.

    Un flag "activo" puede llegar como string ``"true"`` (lo que escribe la
    ingestión) o como booleano JSON ``true`` (si se edita la fila a mano). Fuente
    única de verdad para reconocerlo — evita que cada call site invente su propia
    comparación frágil. Reutilizable para cualquier flag con esta convención
    (sentinela, provisional-desde-marca, etc.).
    """
    return value in ("true", True)


# Alias semántico: reconocer el flag de sentinela es el mismo predicado genérico.
is_sentinel_value = is_flag_true
