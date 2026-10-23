"""E6b — qué import histórico no puede persistir operaciones sin identidad.

Un summary sin ``mapping_contexts`` que entra por el camino multihoja lee sus
filas por keyword: no hay mapeo de columnas del que derivar la identidad de un
comprobante. Ese camino **no calcula ni consulta identidad fuerte**, así que una
fila que entre por ahí puede duplicar una operación que otro archivo ya cargó
con identidad — nadie puede saber que es la misma.

La salida no es adivinar la clave. Una identidad inventada desde un encabezado
es lo que ``domain/operation_identity`` se niega a producir, y por una razón
peor que el duplicado: una clave FALSA hace que Véktor descarte plata real
creyendo que ya la tenía. La salida es no importar así y ofrecer la relectura,
que regenera el summary con contextos usando el parser de hoy.

**El alcance es el que se midió, no el que suena prudente.** Ningún camino de
parseo actual produce un summary con operaciones y sin contextos: los únicos sin
contextos son los de archivos vacíos, que no tienen una sola fila. Esto alcanza
sólo a summaries persistidos ANTES del mapeo universal — y por eso el mensaje
manda a releer y no a volver a subir: el archivo del usuario está perfecto, lo
viejo es su interpretación.

Y por eso el predicado exige que el import VAYA a escribir operaciones. Tres
cosas quedan deliberadamente afuera, cada una con su prueba:

* **Archivos vacíos**, que es lo único sin contextos que el parser emite hoy.
  Sin filas no hay nada que duplicar.
* **Imports que sólo traen productos.** Un producto no es una operación: no
  tiene identidad de comprobante que perder, y bloquearlos rompería un import
  que hoy funciona.
* **La tabla suelta** (``inferred_type`` distinto de mixto y sin
  ``multi_sheet``), que no pasa por esta rama y **sí** planifica identidades con
  las columnas que mapeó el usuario.
"""

from __future__ import annotations

from typing import Any, Final

#: Motivo del rechazo en la traza (`pipeline_events`), estable para diagnóstico.
MOTIVO_LEGACY_SIN_CONTEXTOS: Final = "legacy_sin_contextos"

#: Lo que ve el usuario. Nombra la acción concreta —releer— y no "volver a
#: subir": el archivo no tiene nada de malo, lo viejo es cómo se interpretó.
#:
#: Y NO dice "confirmalo de nuevo", que era lo que decía y estaba mal: la
#: relectura no deja el archivo listo para un segundo confirm — **importa ella
#: misma**, con el resumen fresco, que sí trae contextos y por lo tanto pasa por
#: la identidad fuerte. Medido: sobre el archivo histórico bloqueado, un
#: ``apply_reread`` inserta las filas con los montos bien leídos. Mandar a
#: reconfirmar habría sido mandar a un rechazo idéntico, porque el resumen
#: guardado sigue siendo el viejo.
MENSAJE_LEGACY_SIN_CONTEXTOS: Final = (
    "Este archivo se interpretó con una versión anterior del sistema, que no "
    "reconoce los comprobantes. Importarlo así podría duplicar ventas o gastos "
    "que ya cargaste desde otro archivo, sin forma de detectarlo. No se importó "
    "nada. Volvé a leer el archivo: esa lectura lo reinterpreta con las reglas "
    "actuales y lo importa reconociendo los comprobantes repetidos."
)

#: Los buckets de OPERACIONES y la clave de confirmación que los habilita. Los
#: productos y los maestros no están acá a propósito: no son operaciones.
_BUCKETS_DE_OPERACION: Final = (
    ("ventas", "ventas_detectadas"),
    ("gastos", "gastos_detectados"),
)


def _va_por_el_camino_multihoja(summary: dict[str, Any]) -> bool:
    """El despacho real de ``insert_confirmed_data``, repetido tal cual.

    Si esto divergiera del despacho, el guard protegería un camino distinto del
    que persiste — que es exactamente el error que hace que un bloqueo parezca
    aplicado y no aplique.
    """
    if summary.get("file_type") != "spreadsheet":
        return False
    return summary.get("inferred_type") == "mixed" or bool(summary.get("multi_sheet"))


def importaria_operaciones_sin_identidad(
    summary: dict[str, Any] | None,
    confirmed_fields: dict[str, bool] | None,
) -> bool:
    """¿Este confirm escribiría ventas o gastos por la rama legacy sin identidad?

    No alcanza con que el summary sea viejo: tiene que haber filas de operación
    **y** el usuario tiene que haber confirmado ese tipo. Un archivo histórico
    del que sólo se importan productos no rebota.
    """
    summary = summary or {}
    if not _va_por_el_camino_multihoja(summary):
        return False
    if summary.get("mapping_contexts"):
        return False
    confirmados = confirmed_fields or {}
    return any(
        confirmados.get(clave) and summary.get(bucket)
        for clave, bucket in _BUCKETS_DE_OPERACION
    )
