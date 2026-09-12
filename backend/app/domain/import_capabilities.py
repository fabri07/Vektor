"""E7a-lite — qué capacidades estaban efectivas cuando el usuario confirmó.

El problema que cierra
----------------------
Las cuatro compuertas de rollout del importador se leen **en el momento de
importar**, desde el entorno del proceso que importa. Con el confirm sincrónico
eso no se notaba: el mismo proceso mostraba el preview y escribía los efectos.
Con la ejecución asíncrona el registro pasa por ``vektor-api`` y la ejecución por
``vektor-worker`` — dos procesos, dos entornos — y Railway los redespliega **en
paralelo y sin orden garantizado**.

O sea que esto es alcanzable: alguien confirma un archivo viendo el preview con
el motor de costos de compra encendido, y el worker lo importa con el motor
apagado. Los costos que quedan en los libros no son los que la pantalla mostró,
nadie recibe un error, y lo único raro es un margen que no cierra. Exactamente la
clase de divergencia silenciosa que este programa vino a cerrar.

Por qué verificar y no forzar
-----------------------------
La otra opción era congelar las capacidades y hacer que el ejecutor las IMPONGA,
pasando overrides hasta los ocho lugares donde se leen las compuertas, adentro
del importador. Sería más invasivo y más frágil: una compuerta nueva que alguien
agregue mañana no quedaría cubierta por el override y volvería a leer el entorno,
con el agravante de que ahora el sistema afirmaría estar congelando algo que no
congela.

Verificar es chico y es fail-safe: si las capacidades cambiaron entre el registro
y la ejecución, el intento **no se ejecuta** y lo dice. El usuario vuelve a
confirmar y lo hace bajo las reglas nuevas, habiéndolas visto. Se cambia un
resultado distinto en silencio por un fallo explícito y recuperable, que es el
intercambio correcto cuando lo que está en juego son los números de un negocio.

El precio, declarado: prender una compuerta invalida los intentos que estaban en
vuelo para ese tenant. Son segundos o minutos de trabajo, visibles, y el runbook
del rollout lo dice.
"""

from __future__ import annotations

import uuid
from typing import Any


def capacidades_efectivas(tenant_id: uuid.UUID | str) -> dict[str, bool]:
    """Las compuertas de este tenant, ahora mismo, en este proceso.

    Sólo las que cambian **qué se persiste** de un mismo archivo. La clave es el
    nombre de la variable de entorno —no un alias interno— para que el mensaje de
    error nombre lo que el operador tiene que ir a mirar, y para que no pueda
    quedar desfasada del campo real de ``Settings``.

    ``ASYNC_IMPORT_ROLLOUT_TENANT_IDS`` NO está acá a propósito: gatea si la
    solicitud se registra, no qué se importa. Apagarla mientras un intento está en
    vuelo tiene que dejarlo terminar —es la condición que se pidió explícitamente
    al habilitar la ruta nueva— y meterla acá lo haría fallar.
    """
    from app.config.catalog_final_cost_rollout import (  # noqa: PLC0415
        ENV_VAR as ENV_CATALOG,
    )
    from app.config.catalog_final_cost_rollout import (  # noqa: PLC0415
        catalog_final_cost_enabled_for,
    )
    from app.config.ingestion_schema_decisions_rollout import (  # noqa: PLC0415
        ENV_VAR as ENV_SCHEMA,
    )
    from app.config.ingestion_schema_decisions_rollout import (  # noqa: PLC0415
        ingestion_schema_decisions_enabled_for,
    )
    from app.config.product_supplier_links_rollout import (  # noqa: PLC0415
        ENV_VAR as ENV_LINKS,
    )
    from app.config.product_supplier_links_rollout import (  # noqa: PLC0415
        product_supplier_links_enabled_for,
    )
    from app.config.purchase_cost_rollout import ENV_VAR as ENV_COSTOS  # noqa: PLC0415
    from app.config.purchase_cost_rollout import (  # noqa: PLC0415
        purchase_cost_enabled_for,
    )

    return {
        ENV_COSTOS: purchase_cost_enabled_for(tenant_id),
        ENV_LINKS: product_supplier_links_enabled_for(tenant_id),
        ENV_CATALOG: catalog_final_cost_enabled_for(tenant_id),
        ENV_SCHEMA: ingestion_schema_decisions_enabled_for(tenant_id),
    }


def diferencias(
    congeladas: dict[str, Any] | None, vigentes: dict[str, bool]
) -> dict[str, tuple[bool | None, bool | None]]:
    """Qué cambió entre el registro y la ejecución.

    ``congeladas`` vacío o ``None`` devuelve ``{}`` **a propósito**: un intento
    registrado antes de que esta columna existiera no tiene con qué comparar, y
    hacerlo fallar sería romper en el deploy justamente los intentos en vuelo que
    la ruta asíncrona existe para no perder. Sin snapshot, el comportamiento es el
    de antes — que es el lado seguro de no saber.

    Una capacidad que está en un lado y no en el otro SÍ es una diferencia: pasa
    cuando el código que registró y el que ejecuta no tienen el mismo juego de
    compuertas, y eso es tan capaz de cambiar los números como un flag apagado.
    """
    if not congeladas:
        return {}
    difs: dict[str, tuple[bool | None, bool | None]] = {}
    for clave in sorted(set(congeladas) | set(vigentes)):
        antes = congeladas.get(clave)
        ahora = vigentes.get(clave)
        antes_norm = bool(antes) if clave in congeladas else None
        ahora_norm = ahora if clave in vigentes else None
        if antes_norm != ahora_norm:
            difs[clave] = (antes_norm, ahora_norm)
    return difs


def texto_de_diferencias(difs: dict[str, tuple[bool | None, bool | None]]) -> str:
    """El detalle que lee una persona. Dice qué cambió y qué hacer."""

    def _estado(v: bool | None) -> str:
        if v is None:
            return "no existía"
        return "activa" if v else "inactiva"

    cambios = ", ".join(
        f"{clave} ({_estado(antes)} → {_estado(ahora)})" for clave, (antes, ahora) in difs.items()
    )
    return (
        "La configuración del importador cambió entre el momento en que "
        f"confirmaste y ahora: {cambios}. Para no guardar números distintos de los "
        "que viste en el preview, no se importó nada. Volvé a confirmar el archivo."
    )
