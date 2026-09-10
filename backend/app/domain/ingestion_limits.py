"""Hasta dónde Véktor lee un archivo, y qué dice cuando no puede.

Por qué existe
--------------
Hasta acá el único límite era el tamaño del upload: **16 MB**. Todo lo demás no
tenía tope — ni filas, ni columnas, ni hojas, ni celdas, ni tiempo de parsing, ni
el tamaño **expandido** de un ``.xlsx``, que es un ZIP y puede descomprimir a
cientos de veces su tamaño.

Un archivo de 16 MB con una hoja de un millón de filas no es un ataque: es una
exportación mal hecha, y hoy el worker la intenta entera. Lo que pasa entonces no
es un error: es un proceso que consume memoria hasta que alguien lo mata, sin
dejar dicho por qué.

Dos reglas que gobiernan todo este módulo
------------------------------------------
1. **Se rechaza el archivo entero o se lee entero. Nunca la mitad.** Importar las
   primeras 200.000 filas de un archivo de 500.000 y no decirlo produce libros que
   no cuadran y que nadie sabe que están incompletos — es peor que no importar.
   Por eso los topes lanzan ``LimiteExcedidoError``, no truncan.
2. **El motivo es específico o no sirve.** "El archivo es demasiado grande" no le
   dice a nadie qué hacer. Cada límite nombra qué se excedió, con qué valor, cuál
   era el tope y qué hacer al respecto.

Sobre el tope de tiempo, dicho con precisión
---------------------------------------------
No se promete interrumpir código arbitrario: Python no preempta una llamada C a
mitad de camino. Lo que se acota son las **dos fases donde está el trabajo real**
y que sí controlamos: la descompresión (que se hace con un contador de bytes y se
corta al pasarse) y la iteración de filas (que chequea el reloj cada tanto y
levanta). Un `.xlsx` se abre en modo ``read_only``, que es perezoso: no materializa
las filas al abrir, así que el grueso del trabajo ocurre dentro del bucle que sí
se puede detener. Lo que queda afuera es el armado del esqueleto del libro, y ése
está acotado por el tope de tamaño expandido, que corre antes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

#: 16 MB — el tope de subida que ya existía. Se mueve acá para que el parser lo
#: pueda verificar por su cuenta: los caminos del worker leen de S3 y nunca pasan
#: por el chequeo del endpoint.
MAX_BYTES_COMPRIMIDO = 16 * 1024 * 1024

#: 256 MB expandidos. Un `.xlsx` es un ZIP: 16 MB comprimidos de XML repetitivo
#: descomprimen a mucho más. El tope no se mide con los metadatos del ZIP —que los
#: escribe quien arma el archivo— sino contando los bytes que realmente salen.
MAX_BYTES_EXPANDIDO = 256 * 1024 * 1024

#: Topes estructurales. Salen de lo que un negocio real manda: el archivo más
#: grande visto en producción (Asteria) tiene ~3.000 filas. Los topes están dos
#: órdenes de magnitud arriba a propósito — no están para apretar el caso normal
#: sino para que el patológico falle DICIENDO algo en vez de morir por OOM.
MAX_FILAS = 200_000
MAX_COLUMNAS = 512
MAX_HOJAS = 64
MAX_CELDAS = 5_000_000

#: 120 s de parsing. El confirm sincrónico tiene un cliente de 16 minutos y un
#: lease de 15; el parsing es una fracción de eso y pasarse dos minutos ya indica
#: que el archivo no es el que el usuario cree que subió.
MAX_SEGUNDOS_PARSING = 120.0


@dataclass(frozen=True)
class LimitesDeArchivo:
    """Los topes, juntos y sustituibles (los tests usan valores chicos)."""

    bytes_comprimido: int = MAX_BYTES_COMPRIMIDO
    bytes_expandido: int = MAX_BYTES_EXPANDIDO
    filas: int = MAX_FILAS
    columnas: int = MAX_COLUMNAS
    hojas: int = MAX_HOJAS
    celdas: int = MAX_CELDAS
    segundos: float = MAX_SEGUNDOS_PARSING


LIMITES = LimitesDeArchivo()

# ── Códigos de límite. Set CERRADO: el código se cuenta y se audita, el texto lo
# lee una persona. ────────────────────────────────────────────────────────────
LIMITE_BYTES = "bytes"
LIMITE_EXPANDIDO = "expandido"
LIMITE_FILAS = "filas"
LIMITE_COLUMNAS = "columnas"
LIMITE_HOJAS = "hojas"
LIMITE_CELDAS = "celdas"
LIMITE_TIEMPO = "tiempo"
LIMITE_ARCHIVO_DANADO = "danado"


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


#: Qué decirle al usuario por cada límite. Cada texto nombra **el número que se
#: excedió** y **una salida concreta**: un mensaje que sólo dice "demasiado
#: grande" obliga a adivinar cuál de las cinco dimensiones era.
_TEXTOS: dict[str, Callable[[float, float], str]] = {
    LIMITE_BYTES: lambda v, t: (
        f"El archivo pesa {_mb(v)} y el máximo es {_mb(t)}. Si es una exportación "
        "de varios años, dividila por período y subí un archivo por vez."
    ),
    LIMITE_EXPANDIDO: lambda v, t: (
        f"El archivo ocupa más de {_mb(t)} al descomprimirse (un .xlsx es un ZIP). "
        "Suele pasar con planillas que arrastran miles de filas vacías con formato: "
        "seleccioná las filas de más, borralas y volvé a guardar el archivo."
    ),
    LIMITE_FILAS: lambda v, t: (
        f"El archivo tiene {v:,} filas y el máximo es {t:,}. Dividilo por período y "
        "subí una parte por vez — Véktor no importa una parte y calla el resto."
    ).replace(",", "."),
    LIMITE_COLUMNAS: lambda v, t: (
        f"Una hoja tiene {v} columnas y el máximo es {t}. Suele ser una planilla con "
        "columnas vacías a la derecha: borralas y volvé a guardar el archivo."
    ),
    LIMITE_HOJAS: lambda v, t: (
        f"El archivo tiene {v} hojas y el máximo es {t}. Dejá sólo las que quieras "
        "importar y volvé a guardarlo."
    ),
    LIMITE_CELDAS: lambda v, t: (
        f"El archivo tiene alrededor de {v:,} celdas y el máximo es {t:,}. Es mucho "
        "más de lo que una planilla de negocio suele tener: revisá si no quedaron "
        "filas o columnas vacías con formato."
    ).replace(",", "."),
    LIMITE_ARCHIVO_DANADO: lambda v, t: (  # noqa: ARG005
        "El archivo está dañado o fue modificado por fuera del programa que lo "
        "generó: su contenido no coincide con lo que declara. Volvé a exportarlo "
        "desde donde salió y subilo de nuevo."
    ),
    LIMITE_TIEMPO: lambda v, t: (
        f"Leer el archivo pasó de {t:.0f} segundos y se detuvo. Puede ser un archivo "
        "muy grande o con muchas fórmulas: probá exportarlo como CSV, o dividirlo."
    ),
}


class LimiteExcedidoError(Exception):
    """Un archivo se pasó de un tope. Se rechaza ENTERO, con el motivo.

    No es un error interno: es una respuesta al usuario. Por eso lleva el código
    —cerrado, para contar y auditar— además del texto.
    """

    def __init__(self, limite: str, valor: float, tope: float) -> None:
        self.limite = limite
        self.valor = valor
        self.tope = tope
        super().__init__(self.mensaje)

    @property
    def mensaje(self) -> str:
        texto = _TEXTOS.get(self.limite)
        if texto is None:  # pragma: no cover — el set es cerrado
            return f"El archivo excede el límite de {self.limite}."
        return texto(self.valor, self.tope)


class RelojDeParsing:
    """Cuánto lleva el parsing, para poder cortarlo.

    Se chequea cada ``_CADA`` llamadas y no en cada una: ``perf_counter`` es
    barato pero no gratis, y llamarlo un millón de veces por archivo se nota. El
    intervalo está elegido para que el retraso máximo entre pasarse del tope y
    enterarse sea de milisegundos, no de minutos.
    """

    __slots__ = ("_cuenta", "_limite", "_t0")

    _CADA = 512

    def __init__(self, limites: LimitesDeArchivo = LIMITES) -> None:
        self._t0 = time.perf_counter()
        self._limite = limites.segundos
        self._cuenta = 0

    def controlar(self) -> None:
        """Levanta ``LimiteExcedidoError`` si ya se pasó. Llamar dentro de los bucles."""
        self._cuenta += 1
        # La PRIMERA llamada siempre controla, y después cada `_CADA`. Sin el
        # caso especial, un archivo de menos de 512 filas no consultaba el reloj
        # ni una vez: el tope de tiempo no existía para él. Y no es un caso
        # teórico — 500 filas con celdas enormes o miles de fórmulas tardan tanto
        # como 50.000 filas simples, que es justo cuando el tope hace falta.
        if self._cuenta != 1 and self._cuenta % self._CADA:
            return
        transcurrido = time.perf_counter() - self._t0
        if transcurrido > self._limite:
            raise LimiteExcedidoError(LIMITE_TIEMPO, transcurrido, self._limite)

    @property
    def transcurrido(self) -> float:
        return time.perf_counter() - self._t0
