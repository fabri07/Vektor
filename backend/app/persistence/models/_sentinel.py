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


def is_sentinel_value(value: object) -> bool:
    """True si el valor del flag de sentinela representa "activo" (string o bool)."""
    return value in ("true", True)
