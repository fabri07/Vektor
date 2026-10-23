"""Evaluador ACOTADO de fórmulas de planilla (Fase 3, incidente ASTERIA 2026-10-05).

Un ``.xlsx`` generado por script —o uno que Excel nunca recalculó— trae las
fórmulas sin su resultado guardado. Hasta acá esas celdas quedaban marcadas
(``FORMULA_SIN_RESULTADO``) y su fila iba a «Otros»: en ASTERIA, 1.601 celdas de
precios y envíos. La mayoría son aritmética de la misma fila (``=(E4+F4)``,
``=G4+(G4*H$2)``), que se puede calcular sin adivinar nada.

Qué promete y qué NO
--------------------
**No promete "igual que Excel".** Promete esto, y nada más:

* **Subconjunto declarado** (``VERSION``): números, referencias A1 de la MISMA
  hoja (relativas o absolutas: ``E4``, ``$H$2``), rangos sólo dentro de ``SUM``,
  ``+ - * /``, ``^`` con exponente entero, paréntesis, ``-``/``+`` unarios,
  ``SUM`` y ``ROUND``. Cualquier otra cosa —otra hoja, texto, ``%``,
  comparaciones, ``IF``, ``VLOOKUP``, nombres definidos— queda **pendiente** con su
  motivo. Nunca se aproxima.
* **Semántica documentada:**
  - aritmética en ``Decimal`` exacto y resultado redondeado a 15 dígitos
    significativos, que es la precisión que muestra Excel (Excel usa binario de
    doble precisión; un ``1/3`` puede diferir en el dígito 16, nunca antes);
  - celda vacía = 0 en aritmética, como Excel, y se registra que se usó;
  - en ``SUM`` sobre un rango, el texto y las vacías se ignoran, como Excel; un
    texto usado directamente en aritmética queda pendiente (Excel lo convertiría
    o daría ``#VALUE!``; no se elige);
  - ``ROUND`` redondea la mitad alejándose del cero, como Excel;
  - división por cero, error de Excel guardado en una celda (``#N/A``…) o
    exponente no entero → pendiente, **nunca 0**.
* **Límites:** largo de la fórmula, celdas que puede referenciar (contando las de
  un rango), profundidad de dependencias y **ciclos** (una referencia circular
  queda pendiente). El tiempo lo corta el reloj del parseo del caller.

Lo que no se calcula queda exactamente como antes: con la marca, en «Otros».
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from typing import Any

#: Versión del subconjunto soportado. Ampliarlo cambia qué celdas se calculan.
VERSION = 1

MAX_LARGO_FORMULA = 1000
MAX_CELDAS_POR_FORMULA = 10_000
MAX_PROFUNDIDAD = 64
#: Dígitos significativos del resultado: la precisión visible de Excel.
DIGITOS_SIGNIFICATIVOS = 15
#: Exponente decimal máximo de un número de Excel (~1,8E+308, doble precisión).
#: Más allá Excel da ``#NUM!``; acá, pendiente. Sin este tope, ``=A1^999999``
#: devolvía un número de un millón de dígitos que viajaba al resumen como texto.
EXPONENTE_MAXIMO = 308
#: Exponente de ``^`` más allá del cual el resultado es seguro fuera de rango o
#: indistinguible de cero: se rechaza ANTES de calcular la potencia.
MAX_EXPONENTE_DE_POTENCIA = 10_000

_ERRORES_DE_EXCEL = ("#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!")


class Pendiente(Exception):  # noqa: N818 — es un resultado, no un "error" del evaluador
    """La fórmula no se puede calcular con garantías. ``motivo`` en castellano."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo
        super().__init__(motivo)


# ── Coordenadas ──────────────────────────────────────────────────────────────


def columna_a_numero(letras: str) -> int:
    n = 0
    for ch in letras.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def numero_a_columna(n: int) -> str:
    letras = ""
    while n:
        n, resto = divmod(n - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def coordenada(fila: int, columna: int) -> str:
    return f"{numero_a_columna(columna)}{fila}"


# ── Tokens ───────────────────────────────────────────────────────────────────

_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
     |(?P<ref>\$?[A-Za-z]{1,3}\$?\d+)
     |(?P<func>[A-Za-z][A-Za-z0-9.]*)\s*\(
     |(?P<op>[-+*/^(),:])
    )""",
    re.VERBOSE,
)
_REF = re.compile(r"\$?([A-Za-z]{1,3})\$?(\d+)")
_FUNCIONES = {"SUM", "ROUND"}


def _tokenizar(texto: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    texto = texto.rstrip()
    while pos < len(texto):
        m = _TOKEN.match(texto, pos)
        if m is None or m.end() == pos:
            resto = texto[pos:].strip()
            if resto.startswith(("'", "!")) or "!" in resto.split("(")[0]:
                raise Pendiente("La fórmula usa otra hoja, y sólo se calculan las de la misma")
            raise Pendiente(f"La fórmula usa «{resto[:20]}», que Véktor no calcula")
        tipo = m.lastgroup or ""
        valor = m.group(tipo)
        if tipo == "ref" and m.end() < len(texto) and texto[m.end()] == "!":
            raise Pendiente("La fórmula usa otra hoja, y sólo se calculan las de la misma")
        if tipo == "func":
            nombre = valor.upper()
            if nombre not in _FUNCIONES:
                raise Pendiente(f"La fórmula usa la función {nombre}, que Véktor no calcula")
            valor = nombre
        tokens.append((tipo, valor))
        pos = m.end()
    return tokens


# ── Árbol ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Num:
    valor: Decimal


@dataclass(frozen=True)
class _Ref:
    fila: int
    columna: int


@dataclass(frozen=True)
class _Rango:
    desde: _Ref
    hasta: _Ref


@dataclass(frozen=True)
class _Unario:
    op: str
    x: Any


@dataclass(frozen=True)
class _Binario:
    op: str
    a: Any
    b: Any


@dataclass(frozen=True)
class _Funcion:
    nombre: str
    args: tuple[Any, ...]


class _Parser:
    """Precedencia de Excel: unario > ``^`` > ``* /`` > ``+ -``.

    En Excel el menos unario liga MÁS fuerte que la potencia: ``=-2^2`` es 4.
    """

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t = tokens
        self.i = 0

    def _ver(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def _tomar(self, valor: str | None = None) -> tuple[str, str]:
        tok = self._ver()
        if tok is None or (valor is not None and tok[1] != valor):
            raise Pendiente("La fórmula está incompleta o mal formada")
        self.i += 1
        return tok

    def todo(self) -> Any:
        nodo = self._suma()
        if self._ver() is not None:
            raise Pendiente("La fórmula está mal formada")
        return nodo

    def _suma(self) -> Any:
        nodo = self._producto()
        while (tok := self._ver()) is not None and tok[1] in "+-" and tok[0] == "op":
            self.i += 1
            nodo = _Binario(tok[1], nodo, self._producto())
        return nodo

    def _producto(self) -> Any:
        nodo = self._potencia()
        while (tok := self._ver()) is not None and tok[0] == "op" and tok[1] in "*/":
            self.i += 1
            nodo = _Binario(tok[1], nodo, self._potencia())
        return nodo

    def _potencia(self) -> Any:
        nodo = self._unario()
        while (tok := self._ver()) is not None and tok == ("op", "^"):
            self.i += 1
            nodo = _Binario("^", nodo, self._unario())
        return nodo

    def _unario(self) -> Any:
        tok = self._ver()
        if tok is not None and tok[0] == "op" and tok[1] in "+-":
            self.i += 1
            return _Unario(tok[1], self._unario())
        return self._primario()

    def _primario(self) -> Any:
        tok = self._tomar()
        tipo, valor = tok
        if tipo == "num":
            return _Num(Decimal(valor))
        if tipo == "ref":
            ref = _ref(valor)
            if (sig := self._ver()) is not None and sig == ("op", ":"):
                raise Pendiente("Un rango sólo se calcula adentro de SUM")
            return ref
        if tipo == "func":
            args: list[Any] = []
            if self._ver() != ("op", ")"):
                while True:
                    args.append(self._argumento(valor))
                    if self._ver() == ("op", ","):
                        self.i += 1
                        continue
                    break
            self._tomar(")")
            return _Funcion(valor, tuple(args))
        if tok == ("op", "("):
            nodo = self._suma()
            self._tomar(")")
            return nodo
        raise Pendiente("La fórmula está mal formada")

    def _argumento(self, funcion: str) -> Any:
        tok = self._ver()
        siguiente = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
        if (
            funcion == "SUM"
            and tok is not None
            and tok[0] == "ref"
            and siguiente == ("op", ":")
        ):
            desde = _ref(self._tomar()[1])
            self._tomar(":")
            hasta_tok = self._tomar()
            if hasta_tok[0] != "ref":
                raise Pendiente("El rango está mal formado")
            return _Rango(desde, _ref(hasta_tok[1]))
        return self._suma()


def _ref(texto: str) -> _Ref:
    m = _REF.fullmatch(texto)
    if m is None:
        raise Pendiente("Referencia mal formada")
    return _Ref(int(m.group(2)), columna_a_numero(m.group(1)))


def analizar(formula: str) -> Any:
    """El árbol de una fórmula (con o sin ``=``), o ``Pendiente``."""
    texto = formula[1:] if formula.startswith("=") else formula
    if len(texto) > MAX_LARGO_FORMULA:
        raise Pendiente("La fórmula es demasiado larga para calcularla")
    if not texto.strip():
        raise Pendiente("La fórmula está vacía")
    return _Parser(_tokenizar(texto)).todo()


# ── Evaluación ───────────────────────────────────────────────────────────────


@dataclass
class Calculada:
    """Resultado de UNA celda."""

    valor: Decimal | None
    motivo: str | None = None
    #: La fórmula usó alguna celda vacía como 0 (Excel hace lo mismo).
    uso_vacias: bool = False


@dataclass
class Evaluador:
    """Calcula las fórmulas de UNA hoja.

    ``valor_de(fila, columna)`` devuelve el valor cacheado/escrito de una celda
    (``None`` si está vacía); ``formulas`` son las celdas sin resultado, por
    coordenada 1-based. Una referencia a otra celda de ``formulas`` se calcula
    primero (con memoria y detección de ciclos).
    """

    valor_de: Callable[[int, int], Any]
    formulas: dict[tuple[int, int], str]
    #: Llamado una vez por fórmula: el caller corta por tiempo desde acá.
    controlar: Callable[[], None] = lambda: None
    _hechas: dict[tuple[int, int], Calculada] = field(default_factory=dict)
    _en_curso: set[tuple[int, int]] = field(default_factory=set)

    def calcular(self, fila: int, columna: int) -> Calculada:
        clave = (fila, columna)
        if clave in self._hechas:
            return self._hechas[clave]
        if clave in self._en_curso:
            raise Pendiente("La fórmula tiene una referencia circular")
        if len(self._en_curso) >= MAX_PROFUNDIDAD:
            raise Pendiente("La fórmula depende de demasiadas otras fórmulas encadenadas")
        self.controlar()
        self._en_curso.add(clave)
        estado = _Estado()
        try:
            arbol = analizar(self.formulas[clave])
            with localcontext() as ctx:
                ctx.prec = 34
                # Overflow queda atrapado (es el default) y ahora a la escala de
                # Excel: cualquier paso que se pase levanta `decimal.Overflow`.
                ctx.Emax = EXPONENTE_MAXIMO
                ctx.Emin = -EXPONENTE_MAXIMO
                bruto = self._eval(arbol, estado)
                ctx.prec = DIGITOS_SIGNIFICATIVOS
                valor = +bruto
            # Notación decimal PLANA: `normalize()` deja `1E+3`, y esa celda se
            # serializa como texto más adelante; leída así ya no es un número.
            plano = Decimal(format(valor.normalize(), "f"))
            resultado = Calculada(plano, uso_vacias=estado.vacias)
        except Pendiente as p:
            resultado = Calculada(None, p.motivo)
        except (DecimalException, OverflowError):
            # `decimal.Overflow` NO es `InvalidOperation` ni `OverflowError`: sin
            # la base común, `=1E+999999*10` hacía fallar el parseo del archivo.
            resultado = Calculada(
                None, "El cálculo no da un número válido o es más grande de lo que Excel admite"
            )
        finally:
            self._en_curso.discard(clave)
        self._hechas[clave] = resultado
        return resultado

    def _celda(self, ref: _Ref, estado: _Estado, en_rango: bool) -> Decimal | None:
        estado.contar()
        clave = (ref.fila, ref.columna)
        if clave in self.formulas:
            calculada = self.calcular(*clave)
            if calculada.valor is None:
                raise Pendiente(
                    f"Depende de {coordenada(*clave)}, que no se pudo calcular "
                    f"({calculada.motivo})"
                )
            estado.vacias = estado.vacias or calculada.uso_vacias
            return calculada.valor
        valor = self.valor_de(*clave)
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            if en_rango:
                return None
            estado.vacias = True
            return Decimal(0)
        if isinstance(valor, bool):
            if en_rango:
                return None
            raise Pendiente(f"{coordenada(*clave)} es VERDADERO/FALSO, no un número")
        if isinstance(valor, int | float | Decimal):
            return Decimal(str(valor))
        texto = str(valor).strip()
        if texto.upper() in _ERRORES_DE_EXCEL:
            raise Pendiente(f"{coordenada(*clave)} tiene un error de Excel ({texto})")
        if en_rango:
            return None  # SUM ignora el texto de un rango, como Excel
        raise Pendiente(f"{coordenada(*clave)} es texto («{texto[:20]}»), no un número")

    def _eval(self, nodo: Any, estado: _Estado) -> Decimal:
        if isinstance(nodo, _Num):
            return nodo.valor
        if isinstance(nodo, _Ref):
            valor = self._celda(nodo, estado, en_rango=False)
            return valor if valor is not None else Decimal(0)
        if isinstance(nodo, _Unario):
            x = self._eval(nodo.x, estado)
            return -x if nodo.op == "-" else x
        if isinstance(nodo, _Binario):
            a = self._eval(nodo.a, estado)
            b = self._eval(nodo.b, estado)
            if nodo.op == "+":
                return a + b
            if nodo.op == "-":
                return a - b
            if nodo.op == "*":
                return a * b
            if nodo.op == "/":
                if b == 0:
                    raise Pendiente("La fórmula divide por cero")
                return a / b
            if b != b.to_integral_value():
                raise Pendiente("La fórmula eleva a un exponente con decimales")
            if a == 0 and b < 0:
                raise Pendiente("La fórmula divide por cero")
            if abs(b) > MAX_EXPONENTE_DE_POTENCIA:
                raise Pendiente("La fórmula eleva a un exponente demasiado grande")
            return a ** int(b)
        if isinstance(nodo, _Funcion):
            return self._funcion(nodo, estado)
        raise Pendiente("La fórmula está mal formada")

    def _funcion(self, nodo: _Funcion, estado: _Estado) -> Decimal:
        if nodo.nombre == "SUM":
            total = Decimal(0)
            for arg in nodo.args:
                if isinstance(arg, _Rango):
                    f0, f1 = sorted((arg.desde.fila, arg.hasta.fila))
                    c0, c1 = sorted((arg.desde.columna, arg.hasta.columna))
                    if (f1 - f0 + 1) * (c1 - c0 + 1) > MAX_CELDAS_POR_FORMULA:
                        raise Pendiente("El rango es demasiado grande para calcularlo")
                    for f in range(f0, f1 + 1):
                        for c in range(c0, c1 + 1):
                            v = self._celda(_Ref(f, c), estado, en_rango=True)
                            if v is not None:
                                total += v
                else:
                    total += self._eval(arg, estado)
            return total
        # ROUND(valor, decimales)
        if len(nodo.args) != 2:
            raise Pendiente("ROUND necesita dos argumentos")
        valor = self._eval(nodo.args[0], estado)
        decimales = self._eval(nodo.args[1], estado)
        if decimales != decimales.to_integral_value():
            raise Pendiente("ROUND con una cantidad de decimales no entera")
        cuanto = Decimal(1).scaleb(-int(decimales))
        return valor.quantize(cuanto, rounding=ROUND_HALF_UP) if int(decimales) >= 0 else (
            (valor / cuanto).quantize(Decimal(1), rounding=ROUND_HALF_UP) * cuanto
        )


@dataclass
class _Estado:
    vacias: bool = False
    celdas: int = 0

    def contar(self) -> None:
        self.celdas += 1
        if self.celdas > MAX_CELDAS_POR_FORMULA:
            raise Pendiente("La fórmula referencia demasiadas celdas para calcularla")


# ── Procedencia: qué se calculó y qué quedó pendiente ────────────────────────

#: Cuántos ejemplos se guardan en el resumen (el total se cuenta siempre).
MAX_EJEMPLOS = 20


@dataclass
class RegistroDeCalculo:
    """Lo que hizo el evaluador sobre un archivo, para el resumen y el aviso."""

    celdas: int = 0
    con_vacias: int = 0
    #: (hoja, columna en letras) → celdas calculadas.
    por_columna: dict[tuple[str, str], int] = field(default_factory=dict)
    ejemplos: list[dict[str, str]] = field(default_factory=list)
    #: motivo → cuántas quedaron pendientes por eso.
    pendientes: dict[str, int] = field(default_factory=dict)
    #: El reloj del parseo cortó el cálculo: lo que faltaba quedó con la marca.
    incompleto: bool = False

    def calculada(
        self, hoja: str, fila: int, columna: int, formula: str, resultado: Calculada
    ) -> None:
        self.celdas += 1
        if resultado.uso_vacias:
            self.con_vacias += 1
        clave = (hoja, numero_a_columna(columna))
        self.por_columna[clave] = self.por_columna.get(clave, 0) + 1
        if len(self.ejemplos) < MAX_EJEMPLOS:
            self.ejemplos.append(
                {
                    "hoja": hoja,
                    "celda": coordenada(fila, columna),
                    "formula": formula,
                    "resultado": str(resultado.valor),
                }
            )

    def pendiente(self, motivo: str) -> None:
        self.pendientes[motivo] = self.pendientes.get(motivo, 0) + 1

    def a_resumen(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "celdas": self.celdas,
            "con_celdas_vacias": self.con_vacias,
            "por_columna": [
                {"hoja": h, "columna": c, "celdas": n}
                for (h, c), n in sorted(self.por_columna.items())
            ],
            "ejemplos": self.ejemplos,
            "pendientes": dict(sorted(self.pendientes.items(), key=lambda kv: -kv[1])),
            "incompleto": self.incompleto,
        }

    def aviso(self) -> str | None:
        if not self.celdas:
            return None
        columnas = ", ".join(
            f"{h} {c} ({n})" for (h, c), n in sorted(self.por_columna.items())[:6]
        )
        texto = (
            f"Véktor calculó {self.celdas} celda(s) desde sus fórmulas porque el "
            f"archivo no traía el resultado guardado ({columnas}). Revisá que "
            "coincidan con lo que ves en tu planilla."
        )
        if self.con_vacias:
            texto += (
                f" En {self.con_vacias} de ellas la fórmula usó celdas vacías, que "
                "se tomaron como 0 —igual que Excel—."
            )
        return texto
