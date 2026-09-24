"""Qué es un código escaneado, ANTES de buscarlo.

El lector de barras es un teclado: "tipea" lo que lee y un Enter. Del otro lado
llega un string, y el error que este módulo existe para evitar es tratar ese
string entero como identidad. ``normalize_barcode`` borra letras y símbolos, así
que un alfanumérico como ``AB-12345678`` se reduce a ``12345678`` y puede
coincidir con el EAN-8 de OTRO producto: la caja cobraría el producto
equivocado sin que nadie lo note.

Por eso primero se clasifica, y cada tipo se busca sólo en SU columna:

======================  =====================================  =====================
Tipo                    Regla                                  Dónde se busca
======================  =====================================  =====================
``INTERNAL_SKU``        ``VKT-`` + 12 caracteres               ``internal_sku``
``GTIN``                8/12/13/14 dígitos, checksum válido    ``barcode_normalized``
``SCALE``               EAN-13 con prefijo ``2`` (balanza)     ver abajo
``OPAQUE``              cualquier otra cosa                    ``sku_normalized``,
                                                               literal
======================  =====================================  =====================

Un número con la forma de un GTIN pero con el dígito verificador mal NO es un
GTIN: es un código opaco. Aceptarlo como GTIN haría que una lectura corrupta
(el lector se comió o cambió un dígito) coincida con otro producto.

**Balanza.** Los EAN-13 que empiezan con ``2`` son de circulación restringida:
los genera el local, y una balanza mete adentro el peso o el importe. Leer ese
peso exige un parser por modelo de balanza que v1 no tiene. Así que un código de
balanza sólo se resuelve si un producto tiene EXACTAMENTE ese código cargado
(una coincidencia exacta no inventa nada), y nunca se aprende: aprenderlo
ataría el producto a UN peso, y el siguiente escaneo, con otro peso, no
coincidiría — o peor, coincidiría con otra cosa.

Puro: sin sesión, sin ORM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TipoDeCodigo(StrEnum):
    INTERNAL_SKU = "INTERNAL_SKU"
    GTIN = "GTIN"
    SCALE = "SCALE"
    OPAQUE = "OPAQUE"


#: Formato de `domain/internal_sku.py`: prefijo + 12 caracteres Crockford.
#: El regex es un superconjunto (A-Z0-9) a propósito: rechazar una ``I`` o una
#: ``O`` acá mandaría el código a OPAQUE, donde tampoco lo encuentra nadie.
_INTERNAL_SKU = re.compile(r"^VKT-[A-Z0-9]{12}$")

_LARGOS_GTIN = frozenset({8, 12, 13, 14})

#: Lo que un lector agrega al final o al principio: CR/LF del sufijo, Tab en los
#: que lo emiten así, espacios. Nunca se toca el interior del código.
_BORDES = " \t\r\n\x00"


@dataclass(frozen=True)
class CodigoEscaneado:
    tipo: TipoDeCodigo
    #: El código limpio de bordes y, si es INTERNAL_SKU, en mayúsculas.
    valor: str

    @property
    def aprendible(self) -> bool:
        """Sólo un GTIN se puede vincular a un producto desde la caja.

        - ``SCALE`` lleva peso o importe adentro (ver el docstring del módulo).
        - ``INTERNAL_SKU`` ya ES del producto: lo genera Véktor.
        - ``OPAQUE`` se guardaría en ``barcode``, cuya normalización borra letras:
          sería fabricar la colisión que este módulo existe para evitar.
        """
        return self.tipo is TipoDeCodigo.GTIN


def digito_verificador_gs1(cuerpo: str) -> int:
    """Dígito verificador GS1 (módulo 10) para los dígitos SIN el verificador.

    Desde la derecha, pesos alternados 3 y 1. Vale igual para EAN-8, UPC-A,
    EAN-13 y GTIN-14: es lo que permite validar los cuatro con una función.
    """
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(cuerpo)))
    return (10 - total % 10) % 10


def gtin_valido(digitos: str) -> bool:
    if not digitos.isdigit() or len(digitos) not in _LARGOS_GTIN:
        return False
    return digito_verificador_gs1(digitos[:-1]) == int(digitos[-1])


def clasificar(crudo: str) -> CodigoEscaneado:
    """Clasifica lo que mandó el lector. Nunca falla: lo que no es nada es OPAQUE."""
    limpio = crudo.strip(_BORDES)
    mayus = limpio.upper()
    if _INTERNAL_SKU.match(mayus):
        return CodigoEscaneado(TipoDeCodigo.INTERNAL_SKU, mayus)
    if gtin_valido(limpio):
        if len(limpio) == 13 and limpio[0] == "2":
            return CodigoEscaneado(TipoDeCodigo.SCALE, limpio)
        return CodigoEscaneado(TipoDeCodigo.GTIN, limpio)
    return CodigoEscaneado(TipoDeCodigo.OPAQUE, limpio)


def variantes_gtin(digitos: str) -> tuple[str, ...]:
    """Las formas en que el MISMO GTIN puede estar guardado.

    Un UPC-A de 12 dígitos es el EAN-13 con un ``0`` adelante, y según cómo esté
    configurado el lector (o cómo vino el archivo del proveedor) llega o se
    guardó con 12, 13 o 14 dígitos. Para GS1 son el mismo número: se completa con
    ceros a la izquierda. Se devuelven sólo largos válidos de GTIN.

    Ojo: esto aplica SOLO a GTIN. En un ``external_code`` los ceros a la
    izquierda son significativos y no se tocan (ver ``domain/external_code.py``).
    """
    base = digitos.lstrip("0")
    formas = {digitos}
    for largo in sorted(_LARGOS_GTIN):
        if len(base) <= largo:
            formas.add(base.zfill(largo))
    return tuple(sorted(f for f in formas if len(f) in _LARGOS_GTIN))
