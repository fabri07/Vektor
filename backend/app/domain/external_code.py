"""El código con que el negocio identifica a un cliente, proveedor o producto.

Qué problema resuelve
---------------------
Las claves fuertes que ya existen son ajenas al negocio: el CUIT lo asigna el
Estado, el código de barras lo asigna el fabricante. Ninguna de las dos existe
para el 90% de los maestros de un kiosco. El código externo es **el identificador
que el negocio ya usa en su propio sistema** —o el que le asigna su proveedor— y
es lo único que permite decir "esta fila es el mismo cliente que aquella" cuando
no hay documento ni código de barras.

Ámbito: por tenant, por tipo de entidad, y por sistema de origen
----------------------------------------------------------------
La unicidad la da la tabla (el tipo de entidad) más el tenant. Pero un mismo
tenant puede exportar desde más de un sistema, y el "1024" de uno no es el "1024"
del otro: si el código viene de un sistema declarado, ese sistema entra en la
clave. Sin sistema declarado, el código se toma como **propio del negocio**, que
es un espacio de nombres más y no un comodín — dos códigos propios iguales siguen
siendo la misma entidad, que es justamente lo que se quiere.

Por qué los ceros a la izquierda NO se recortan
-----------------------------------------------
Un código externo es una CADENA, no un número. ``007`` y ``7`` pueden ser dos
artículos distintos del mismo catálogo, y recortar para "normalizar" fusionaría
dos identidades — el error más caro que puede cometer este módulo.

Es la diferencia deliberada con ``document_series`` en ``operation_identity``,
donde el punto de venta SÍ se recorta porque ahí es un entero que cada sistema
escribe con distinto padding. Los dos criterios conviven a propósito y ninguno es
"el general": lo que decide es qué representa el dato.

Qué NO hace este módulo
-----------------------
No compara contenido y no dice si dos maestros son equivalentes. Un maestro no es
una operación económica: dos filas con el mismo código y el teléfono distinto son
**la misma entidad con un dato actualizado**, no un conflicto. La huella de
contenido de ``operation_identity`` responde otra pregunta ("¿este documento dice
lo mismo?") y usarla acá mezclaría dos contratos.
"""

from __future__ import annotations

import unicodedata
from typing import Any

#: Versión de la representación de la clave. Cambiarla invalida el matcheo con
#: las claves ya guardadas — a propósito: si la forma cambió, no son comparables.
CLAVE_VERSION = "e1"

#: Separador entre sistema y código. Un carácter de control, no un guion ni un
#: dos puntos: los dos aparecen dentro de códigos reales ("A-0001", "SIS:7") y
#: usarlos haría que ``sistema="a", codigo="b"`` y ``sistema="", codigo="a-b"``
#: colisionaran.
_SEP = "\x1f"

#: Tope de cada componente. La columna es ``String(180)``; se cortan los dos
#: componentes antes de unirlos para que un código larguísimo no empuje al
#: sistema fuera de la clave y dos sistemas distintos terminen colisionando.
_MAX_CODIGO = 100
_MAX_SISTEMA = 60


def _plegar(valor: Any, tope: int) -> str:
    """Minúsculas, sin tildes, sin espacios repetidos. **Con** ceros a la izquierda.

    Se pliega el caso y los acentos porque el mismo código escrito ``A-001`` y
    ``a-001`` es el mismo código: son variantes de tipeo, no de identidad. Lo que
    no se toca son los dígitos.
    """
    if valor is None:
        return ""
    texto = str(valor).strip()
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())[:tope]


def normalizar_codigo(valor: Any) -> str:
    """Forma comparable del código. ``""`` si no hay nada legible."""
    return _plegar(valor, _MAX_CODIGO)


def normalizar_sistema(valor: Any) -> str:
    """Forma comparable del sistema de origen. ``""`` = código propio del negocio."""
    return _plegar(valor, _MAX_SISTEMA)


def clave_de_codigo_externo(codigo: Any, sistema: Any = None) -> str | None:
    """Clave canónica del código, o ``None`` si no hay código.

    ``None`` —y no cadena vacía— porque es lo que va a la columna indexada: el
    índice único es PARCIAL sobre los no nulos, así que una entidad sin código no
    compite con ninguna otra. Con cadena vacía, todas las entidades sin código del
    tenant colisionarían entre sí y la primera bloquearía a las demás.
    """
    normalizado = normalizar_codigo(codigo)
    if not normalizado:
        return None
    return f"{CLAVE_VERSION}{_SEP}{normalizar_sistema(sistema)}{_SEP}{normalizado}"


def mismo_codigo(codigo_a: Any, sistema_a: Any, codigo_b: Any, sistema_b: Any) -> bool:
    """¿Los dos códigos identifican a la misma entidad?

    Dos ausencias NO son iguales: dos maestros sin código no son el mismo maestro.
    """
    clave_a = clave_de_codigo_externo(codigo_a, sistema_a)
    return clave_a is not None and clave_a == clave_de_codigo_externo(codigo_b, sistema_b)
