"""La ÚNICA forma de crear un engine async en la aplicación.

Existe porque la API y cada job de Celery crean el suyo (el worker no puede
compartir el de la API: cada task corre su propio ``asyncio.run`` y asyncpg ata
las conexiones al loop que las abrió). Con 18 lugares armando engines a mano, una
política que tiene que valer para toda escritura —como no mandar nunca un
``U+0000`` a un JSONB— vivía en uno solo, y el worker que falló en producción
usaba otro.

Sin efectos al importarla, a diferencia de ``engine.py``, que construye el engine
de la API en cuanto se importa. Compuerta: ``test_engine_unico.py`` falla si
aparece un ``create_async_engine`` fuera de este archivo.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.texto_nulo import NuloEnJsonError, buscar_nulo


def serializar_json(valor: Any) -> str:
    """El serializador de toda columna JSON/JSONB.

    Igual al default de SQLAlchemy (``json.dumps``) más una verificación: un
    ``U+0000`` se rechaza con la ruta donde está, en vez del
    ``UntranslatableCharacterError`` de asyncpg, que no dice nada útil.

    **No limpia.** La entrada ya se normaliza en ``parse_uploaded_content``; un
    nulo que llega hasta acá pasó por un camino que no normaliza, y limpiarlo en
    silencio escondería ese camino. El chequeo caro (recorrer la estructura) sólo
    corre si el texto serializado puede tener un nulo: ``json.dumps`` escribe
    ``U+0000`` como ``\\u0000``, así que si esa secuencia no aparece no hay nulo.
    """
    texto = json.dumps(valor)
    if "\\u0000" in texto:
        ruta = buscar_nulo(valor)
        # La secuencia también aparece si alguien escribió literalmente "\u0000"
        # (seis caracteres) en un texto: eso es un dato válido, no un nulo.
        if ruta is not None:
            raise NuloEnJsonError(ruta)
    return texto


def crear_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """``create_async_engine`` con la política común de la aplicación.

    ``hide_parameters``: el texto de un error de la base (lo que se loguea y va a
    Sentry en un 500) no lleva los parámetros de la sentencia. Varios tokens de
    un solo uso —verificación de email, reseteo de clave, solicitud de acceso—
    son la PK de su fila, así que un timeout buscándolos los imprimía en claro
    (revisión profunda 2026-10-24).
    """
    kwargs.setdefault("json_serializer", serializar_json)
    kwargs.setdefault("hide_parameters", True)
    return create_async_engine(url, **kwargs)
