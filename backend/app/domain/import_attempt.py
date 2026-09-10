"""El ciclo de vida de UN intento de importación, y qué transiciones existen.

Por qué un intento y no "el archivo importando"
-----------------------------------------------
Hoy el estado de una importación vive en ``uploaded_files`` (``import_phase``,
``import_attempt_id``) y el trabajo corre **dentro del request HTTP**. Eso trae
tres problemas que se ven de una sola vez:

1. El cliente sostiene una conexión de hasta 16 minutos. Si se corta —el usuario
   cierra la pestaña, el proxy corta, el wifi se cae— no hay adónde volver a
   preguntar: el trabajo puede haber terminado, puede estar a medias, y desde
   afuera se ven igual.
2. Reintentar por timeout crea una importación NUEVA, no reanuda la anterior.
3. El archivo tiene un solo estado, así que dos intentos sobre el mismo archivo
   se pisan el rastro: el segundo borra la historia del primero.

Un intento es una fila propia: tiene identidad, se puede consultar, se puede
reanudar y deja historia aunque haya otro después.

La versión se congela
---------------------
El intento guarda **la solicitud entera** —mapeos, decisiones, confirmaciones,
sobre qué revisión del archivo— y el ejecutor consume ESA versión, no lo que el
borrador diga cuando le toque correr. Sin eso, un usuario que edita el mapeo
mientras su import está encolado obtiene un resultado que no pidió y que no
coincide con lo que la pantalla le mostró al confirmar.

Idempotencia de la PETICIÓN, no de la ejecución
-----------------------------------------------
Dos cosas distintas que se confunden seguido:

* **La misma petición repetida** (el usuario apretó dos veces, el cliente
  reintentó por timeout) tiene que devolver **el mismo intento**, no crear otro.
  Eso lo da la clave de petición.
* **La misma orden entregada dos veces** por el transporte (Celery no promete
  "exactly once") tiene que ejecutarse una sola vez. Eso NO lo da la clave de
  petición: lo da el lease con token al ejecutar.

Por eso el intento lleva las dos cosas: ``request_key`` para la primera y
``lease_token`` para la segunda.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

# ── Estados ──────────────────────────────────────────────────────────────────
#: Registrado y con su orden escrita; todavía nadie lo tomó.
PENDIENTE: Final = "PENDIENTE"
#: Un ejecutor lo reclamó y tiene el token. Lleva ``lease_expires_at``.
EJECUTANDO: Final = "EJECUTANDO"
#: Terminó y publicó sus efectos. ``result_json`` tiene el resultado.
COMPLETADO: Final = "COMPLETADO"
#: Terminó sin publicar. ``error_code``/``error_detail`` dicen por qué.
FALLADO: Final = "FALLADO"
#: El usuario lo canceló antes de que publicara. No es un fallo.
CANCELADO: Final = "CANCELADO"

ESTADOS: Final[frozenset[str]] = frozenset(
    {PENDIENTE, EJECUTANDO, COMPLETADO, FALLADO, CANCELADO}
)

#: Los estados donde el intento ya no se mueve más. Un intento terminal es
#: seguro de devolver como respuesta a una petición repetida.
TERMINALES: Final[frozenset[str]] = frozenset({COMPLETADO, FALLADO, CANCELADO})

#: Qué transiciones existen. Todo lo que no está acá es un bug, no un caso raro:
#: un ``COMPLETADO`` que vuelve a ``EJECUTANDO`` significa que un ejecutor viejo
#: revivió y está por publicar encima de un resultado bueno.
_TRANSICIONES: Final[dict[str, frozenset[str]]] = {
    PENDIENTE: frozenset({EJECUTANDO, CANCELADO, FALLADO}),
    # Vuelve a PENDIENTE cuando su lease vence sin que nadie lo cierre: el
    # ejecutor murió. No es un fallo — es el caso que la recuperación de
    # huérfanos existe para atender.
    EJECUTANDO: frozenset({COMPLETADO, FALLADO, PENDIENTE, CANCELADO}),
    COMPLETADO: frozenset(),
    FALLADO: frozenset(),
    CANCELADO: frozenset(),
}


class TransicionInvalidaError(Exception):
    """Se intentó mover un intento a un estado al que no puede ir.

    Es un error de programa y no una condición del usuario: si un ejecutor
    vencido intenta completar un intento que otro ya cerró, lo que corresponde es
    que falle ruidosamente y no que pise el resultado.
    """

    def __init__(self, desde: str, hacia: str) -> None:
        self.desde = desde
        self.hacia = hacia
        super().__init__(f"Un intento no puede pasar de {desde} a {hacia}.")


def puede_pasar(desde: str, hacia: str) -> bool:
    """¿La máquina de estados admite esta transición?"""
    return hacia in _TRANSICIONES.get(desde, frozenset())


def exigir_transicion(desde: str, hacia: str) -> None:
    """Levanta si la transición no existe. Ver ``TransicionInvalidaError``."""
    if not puede_pasar(desde, hacia):
        raise TransicionInvalidaError(desde, hacia)


def es_terminal(estado: str) -> bool:
    return estado in TERMINALES


# ── Errores estructurados ────────────────────────────────────────────────────
# Set CERRADO. El código se cuenta, se filtra y decide si conviene reintentar; el
# detalle lo lee una persona. Un `str(exc)` suelto no sirve para ninguna de las
# dos cosas.
ERROR_LIMITE: Final = "limite_excedido"
ERROR_VALIDACION: Final = "validacion"
ERROR_IMPORT_VACIO: Final = "import_vacio"
ERROR_LEASE_PERDIDO: Final = "lease_perdido"
ERROR_ARCHIVO_BORRADO: Final = "archivo_borrado"
ERROR_TRANSITORIO: Final = "transitorio"
ERROR_DESCONOCIDO: Final = "desconocido"

#: Cuáles vale la pena reintentar. El default es NO: un archivo que excede un
#: límite o que no valida va a fallar igual las tres veces, y reintentarlo
#: retrasa el diagnóstico y ocupa al ejecutor. Mismo criterio que
#: ``_es_transitorio`` en el worker de parseo.
REINTENTABLES: Final[frozenset[str]] = frozenset({ERROR_TRANSITORIO})


def conviene_reintentar(error_code: str | None, intentos: int, maximo: int) -> bool:
    return bool(error_code in REINTENTABLES and intentos < maximo)


# ── Identidad de la petición ─────────────────────────────────────────────────
def huella_de_solicitud(payload: dict[str, Any]) -> str:
    """Huella del CONTENIDO de una solicitud de importación.

    Sirve para responder la pregunta que separa "el usuario apretó dos veces" de
    "el usuario mandó otra cosa con la misma clave": misma clave y misma huella es
    la misma petición y devuelve el mismo intento; misma clave y huella distinta
    es un conflicto y se dice, porque devolver el intento viejo importaría algo
    que el usuario no pidió y crear uno nuevo rompería la promesa de la clave.

    Se serializa con las claves ordenadas: dos JSON con el mismo contenido y
    distinto orden de campos son la misma solicitud, y el navegador no garantiza
    el orden.
    """
    canonico = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode()).hexdigest()
