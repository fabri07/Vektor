"""El carácter nulo (``U+0000``) en lo que entra y en lo que se guarda.

Postgres no acepta ``\\x00`` ni en ``TEXT`` ni en ``JSONB``: el ``INSERT`` falla
entero con ``UntranslatableCharacterError``. SQLite lo guarda sin quejarse, así
que la suite no lo ve nunca. En producción alcanza con UNA celda para que el
worker no pueda guardar el resultado del archivo (ASTERIA, 2026-10-05).

Dos piezas, a propósito separadas:

* **Entrada** (``quitar_nulos_de_filas`` / ``quitar_nulos_de_texto``): un nulo
  dentro de una celda o de un texto extraído es basura de codificación —un CSV
  exportado en UTF-16 leído como 8 bits, un PDF con relleno, el escape
  ``_x0000_`` de Excel—, no un dato. Se quita **sólo** ``U+0000``: limpiar
  "caracteres no imprimibles" en general cambiaría datos válidos (un tab en un
  detalle, un salto de línea en una dirección). Lo que se quitó queda registrado
  para que la pantalla lo diga; el archivo original no se toca.
* **Salida** (``buscar_nulo``): la red del serializador JSON del engine. Ahí NO
  se limpia: un nulo que llega hasta la base pasó por un camino que no normaliza,
  y limpiarlo en silencio escondería ese camino. Se rechaza nombrando dónde está.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NULO = "\x00"

#: Cuántos ejemplos de celdas cambiadas se guardan en el resumen. El total se
#: cuenta siempre; los ejemplos son para que el usuario encuentre la celda.
MAX_EJEMPLOS_DE_NORMALIZACION = 20


@dataclass(frozen=True)
class CeldaNormalizada:
    """Una celda de la que se quitaron nulos. ``fila`` y ``columna`` son 1-based,
    como las ve el usuario en la planilla."""

    hoja: str | None
    fila: int
    columna: int
    nulos: int


@dataclass
class Normalizaciones:
    """Registro de lo que se quitó en un archivo."""

    celdas: int = 0
    nulos: int = 0
    ejemplos: list[CeldaNormalizada] | None = None

    def registrar(self, celda: CeldaNormalizada) -> None:
        self.celdas += 1
        self.nulos += celda.nulos
        if self.ejemplos is None:
            self.ejemplos = []
        if len(self.ejemplos) < MAX_EJEMPLOS_DE_NORMALIZACION:
            self.ejemplos.append(celda)

    def a_resumen(self) -> dict[str, Any]:
        return {
            "caracter": "U+0000",
            "celdas": self.celdas,
            "nulos": self.nulos,
            "ejemplos": [
                {"hoja": e.hoja, "fila": e.fila, "columna": e.columna, "nulos": e.nulos}
                for e in (self.ejemplos or [])
            ],
        }

    def aviso(self) -> str:
        return (
            f"Se quitaron {self.nulos} carácter(es) nulo(s) de {self.celdas} celda(s). "
            "Es basura de codificación que suele dejar la exportación desde otro "
            "sistema; el resto del contenido de esas celdas se conservó tal cual."
        )


def quitar_nulos_de_texto(texto: str) -> tuple[str, int]:
    """El texto sin ``U+0000`` y cuántos tenía."""
    cantidad = texto.count(NULO)
    if not cantidad:
        return texto, 0
    return texto.replace(NULO, ""), cantidad


def quitar_nulos_de_filas(
    filas: list[list[Any]],
    registro: Normalizaciones,
    *,
    hoja: str | None = None,
) -> list[list[Any]]:
    """Las filas sin ``U+0000`` en sus celdas de texto.

    Devuelve la MISMA lista si no había ningún nulo (el caso de casi todos los
    archivos), así que no cuesta una copia. Las celdas que no son texto —números
    y fechas nativas de openpyxl, ``None``— no se tocan.
    """
    if not any(isinstance(c, str) and NULO in c for fila in filas for c in (fila or ())):
        return filas
    limpias: list[list[Any]] = []
    for i, fila in enumerate(filas, start=1):
        if fila is None:
            limpias.append(fila)
            continue
        nueva = list(fila)
        for j, celda in enumerate(nueva, start=1):
            if isinstance(celda, str) and NULO in celda:
                nueva[j - 1], cantidad = quitar_nulos_de_texto(celda)
                registro.registrar(CeldaNormalizada(hoja, i, j, cantidad))
        limpias.append(nueva)
    return limpias


def desambiguar_encabezados(
    originales: list[Any], limpios: list[Any]
) -> tuple[list[Any], list[str]]:
    """Encabezados que quedaron IGUALES sólo por haberles quitado nulos se
    distinguen con un sufijo, y se avisa.

    Sin esto, ``"Monto\\x00"`` y ``"Monto"`` se vuelven la misma clave del dict de
    la fila y una de las dos columnas desaparece sin rastro. Sólo actúa sobre las
    colisiones que creó la limpieza: dos encabezados que ya venían repetidos en el
    archivo no son asunto de esta función.
    """
    resultado = list(limpios)
    avisos: list[str] = []
    # Los encabezados que la limpieza NO tocó se quedan con su nombre: el que se
    # renombra es siempre el que cambió, venga antes o después en el archivo.
    ocupados: dict[Any, Any] = {}
    for original, limpio in zip(originales, limpios, strict=True):
        if original == limpio:
            ocupados.setdefault(limpio, original)
    for i, (original, limpio) in enumerate(zip(originales, limpios, strict=True)):
        if original == limpio:
            continue
        if limpio in ocupados and ocupados[limpio] != original:
            n = 2
            candidato = f"{limpio} ({n})"
            while candidato in ocupados or candidato in resultado:
                n += 1
                candidato = f"{limpio} ({n})"
            resultado[i] = candidato
            avisos.append(
                f"La columna «{limpio}» aparecía dos veces una vez quitados los "
                f"caracteres nulos; la que tenía nulos se renombró «{candidato}» "
                "para no perder sus datos."
            )
            ocupados[candidato] = original
            continue
        ocupados.setdefault(limpio, original)
    return resultado, avisos


def buscar_nulo(valor: Any, ruta: str = "$") -> str | None:
    """La ruta del primer ``U+0000`` dentro de una estructura JSON, o ``None``.

    Mira claves y valores. La ruta es la que se loguea y la que va en el error:
    ``$.mapping_contexts[2].preview_rows[0].monto``.
    """
    if isinstance(valor, str):
        return ruta if NULO in valor else None
    if isinstance(valor, dict):
        for clave, sub in valor.items():
            if isinstance(clave, str) and NULO in clave:
                return f"{ruta}.{clave!r}"
            encontrado = buscar_nulo(sub, f"{ruta}.{clave}")
            if encontrado is not None:
                return encontrado
        return None
    if isinstance(valor, list | tuple):
        for i, sub in enumerate(valor):
            encontrado = buscar_nulo(sub, f"{ruta}[{i}]")
            if encontrado is not None:
                return encontrado
    return None


class NuloEnJsonError(ValueError):
    """Un ``U+0000`` llegó hasta la escritura de un JSON en la base.

    Se levanta en el serializador del engine, ANTES de mandar nada: así el error
    dice dónde está el nulo, en vez del ``UntranslatableCharacterError`` de
    asyncpg, que no lo dice.
    """

    def __init__(self, ruta: str) -> None:
        self.ruta = ruta
        super().__init__(f"carácter nulo (U+0000) en {ruta}: Postgres no lo acepta")
