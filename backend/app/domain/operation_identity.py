"""Identidad fuerte de una operación importada: cuándo dos filas son *la misma*.

Por qué existe
--------------
La deduplicación de hoy es por ``(archivo, contexto, índice)``: reconoce que una
fila ya entró **desde el mismo archivo**. No ve nada entre archivos. Medido contra
Postgres real: el mismo contenido subido como archivo nuevo duplica todo (3 gastos
→ 6, $12.000 → $24.000, stock 10 → 20). El guard de ``content_hash`` del upload
tampoco lo ve, porque una planilla reexportada desde Excel cambia de hash (el zip
guarda timestamps).

``import_overlap_service`` tapa el agujero **avisando** por fecha + importe. Eso
es todo lo que se puede hacer sin identidad: dos compras iguales el mismo día al
mismo proveedor existen, y son dos compras.

Este módulo define la identidad que convierte ese aviso en una decisión: cuándo se
puede AFIRMAR que dos operaciones son la misma y, por lo tanto, no aplicar la
segunda.

La regla, y por qué es tan exigente
-----------------------------------
Una clave fuerte es una que **no puede repetirse por casualidad**. Todo lo que se
completa con un default, se adivina o se deriva de la posición deja de serlo, y
una clave fuerte falsa es peor que ninguna: hace que Véktor descarte plata real en
silencio. Por eso acá el default de cada duda es NO producir clave — la operación
cae al circuito de candidatos, que sólo mira y avisa.

Concretamente:

* **Sistema de origen declarado.** Un ``external_operation_id`` sin decir de qué
  sistema salió no identifica nada: el "1024" de un sistema y el "1024" de otro
  son dos operaciones distintas. Sin ``external_source`` no hay clave. Y la
  entidad entra en la clave (I6-3): el sistema garantiza el ID único dentro de
  su tabla de ventas o de compras, no entre las dos.
* **Emisor con identificador estable, y el emisor correcto.** En una COMPRA el
  emisor es el proveedor, y se lo identifica por CUIT/CUIL — nunca por nombre
  ("Distribuidora San Juan" y "DISTRIB. SAN JUAN SRL" son la misma empresa escrita
  de dos formas, y dos proveedores distintos pueden llamarse igual). En una VENTA
  propia el emisor es el negocio: **el cliente es el receptor**, no el emisor, así
  que no entra en la clave. Como la tabla de identidades está scopeada por tenant,
  el emisor de una venta propia es constante.
* **Tipo y serie según el formato.** Un número de comprobante suelto se repite
  entre tipos y entre puntos de venta: la factura A 0001-00000123 y el remito
  0001-00000123 comparten número y no son el mismo documento. Qué componentes
  exige cada formato lo dice ``FORMATOS_DE_COMPROBANTE``; un tipo que no está en
  el catálogo no produce clave, porque no sabemos qué lo distingue. La letra
  fiscal también distingue (I6-4): la Factura A y la Factura B del mismo punto
  de venta se numeran por separado, así que una factura sin letra no tiene clave.
* **La línea no se inventa.** Dos renglones del mismo remito pueden traer el mismo
  producto con cantidades, precios o descuentos distintos, y el orden de los
  renglones cambia si alguien reordena la planilla. Derivar la identidad de la
  línea de "producto + ordinal" produce una clave que cambia sola. Por eso: si el
  archivo trae un identificador de línea explícito (``external_line_id``) se usa;
  si no, **la unidad de comparación es el documento completo**, con sus renglones
  repetidos preservados, y cualquier diferencia va a revisión.

Contenido
---------
La identidad dice "es el mismo documento". La huella de contenido dice "dice lo
mismo". Se calcula sobre los **valores efectivos** —los que producen efectos: el
monto resuelto, la fecha interpretada, la cantidad, el precio unitario, los
ajustes de línea y la identidad del producto—, usando la misma interpretación que
el importador, no sobre el texto crudo de las celdas.

Las dos representaciones están **versionadas** (``CLAVE_VERSION`` /
``CONTENIDO_VERSION``): cambiar cómo se arma una clave o una huella hace que las
viejas dejen de matchear, y sin la versión adentro eso se vería como "todo es
nuevo" sin ninguna señal.

Límite declarado, y por qué es seguro
-------------------------------------
La huella se calcula desde la interpretación de la fila, no desde lo que quedó
persistido. Si alguna vez divergieran, el efecto sería que un documento realmente
repetido se vea como **conflicto** y vaya a revisión — nunca que uno distinto se
saltee. El modo de falla es conservador por construcción: ante cualquier duda se
aplica menos automatismo, no más.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

#: Versión de la representación de CLAVE. Va POR FORMATO, no global: subirla
#: invalida el matcheo con las claves ya guardadas de ese formato (a propósito: si
#: la forma cambió, no son comparables).
#:
#: I6-3/I6-4: ``k2`` es la de los formatos que cambiaron — el ID externo lleva la
#: ENTIDAD (``k2:ext:{entity}:{sistema}:{id}``) y el comprobante con letra fiscal
#: lleva la LETRA (``k2:doc:{emisor}:{tipo}:{letra}:{serie}:{numero}``). Los
#: formatos que no cambiaron (remito, ticket, recibo sin letra) siguen en ``k1``
#: byte a byte, así que sus claves guardadas matchean sin ninguna lectura extra.
#: Las ``k1`` viejas de los formatos que sí cambiaron no se migran: el servicio
#: las sigue reconociendo con ``clave_legada_k1`` (ver
#: ``operation_identity_service``), así que reimportar un archivo ya importado no
#: duplica nada.
CLAVE_VERSION = "k2"
#: Versión de los formatos que no cambiaron, y de toda clave guardada antes de
#: I6-3/I6-4.
CLAVE_VERSION_K1 = "k1"
#: Versión de la representación de CONTENIDO. Misma lógica.
CONTENIDO_VERSION = "c1"

Granularidad = Literal["documento", "linea"]


# ── Por qué una fila no tiene clave fuerte ───────────────────────────────────
# Set CERRADO. El texto es lo que ve el usuario; el código es lo que se cuenta y
# se audita. Se enumeran todos los motivos porque "no se pudo" sin decir qué
# faltaba obliga a adivinar qué columna agregar a la planilla.
MOTIVO_SIN_COLUMNAS = "sin_columnas"
MOTIVO_SISTEMA_NO_DECLARADO = "sistema_no_declarado"
MOTIVO_EMISOR_SIN_IDENTIFICADOR = "emisor_sin_identificador"
MOTIVO_TIPO_NO_DECLARADO = "tipo_no_declarado"
MOTIVO_TIPO_DESCONOCIDO = "tipo_desconocido"
MOTIVO_SERIE_FALTANTE = "serie_faltante"
MOTIVO_NUMERO_FALTANTE = "numero_faltante"
MOTIVO_SERIE_CONTRADICTORIA = "serie_contradictoria"
MOTIVO_LETRA_FALTANTE = "letra_faltante"
MOTIVO_LETRA_CONTRADICTORIA = "letra_contradictoria"

TEXTO_DEL_MOTIVO: dict[str, str] = {
    MOTIVO_SIN_COLUMNAS: (
        "el archivo no trae ninguna columna que identifique la operación "
        "(número de comprobante, o el ID de tu sistema)"
    ),
    MOTIVO_SISTEMA_NO_DECLARADO: (
        "hay un ID de operación pero no se declaró de qué sistema salió: el mismo "
        "número puede existir en dos sistemas distintos"
    ),
    MOTIVO_EMISOR_SIN_IDENTIFICADOR: (
        "el comprobante no trae el CUIT/CUIL del proveedor que lo emitió; el nombre "
        "no alcanza porque se escribe de muchas formas"
    ),
    MOTIVO_TIPO_NO_DECLARADO: (
        "no se declaró el tipo de comprobante (factura, remito, nota de crédito…), "
        "y el mismo número existe en tipos distintos"
    ),
    MOTIVO_TIPO_DESCONOCIDO: (
        "el tipo de comprobante no es uno de los que Véktor sabe distinguir"
    ),
    MOTIVO_SERIE_FALTANTE: (
        "falta el punto de venta / serie, y este tipo de comprobante repite el "
        "número en cada punto de venta"
    ),
    MOTIVO_NUMERO_FALTANTE: "falta el número de comprobante",
    MOTIVO_SERIE_CONTRADICTORIA: (
        "el punto de venta de la columna y el que viene adentro del número no "
        "coinciden, así que no se sabe cuál es el comprobante"
    ),
    MOTIVO_LETRA_FALTANTE: (
        "falta la letra del comprobante (A, B, C…): la factura A y la B del mismo "
        "punto de venta se numeran por separado y repiten números"
    ),
    MOTIVO_LETRA_CONTRADICTORIA: (
        "la letra del tipo de comprobante y la que viene adentro del número no "
        "coinciden, así que no se sabe cuál es el comprobante"
    ),
}


@dataclass(frozen=True)
class ClaveDeOperacion:
    """Identidad fuerte: dos operaciones con la misma clave SON la misma."""

    clave: str
    granularidad: Granularidad


@dataclass(frozen=True)
class SinClave:
    """No hay identidad suficiente. ``motivo`` es un código de ``TEXTO_DEL_MOTIVO``."""

    motivo: str

    @property
    def texto(self) -> str:
        return TEXTO_DEL_MOTIVO.get(self.motivo, self.motivo)


Resultado = ClaveDeOperacion | SinClave


# ── Formatos de comprobante ──────────────────────────────────────────────────
@dataclass(frozen=True)
class FormatoDeComprobante:
    """Qué componentes exige un tipo de comprobante para quedar identificado.

    ``exige_serie`` es el punto de venta en los comprobantes fiscales argentinos:
    el número se reinicia por punto de venta, así que sin él "00000123" identifica
    tantas facturas como bocas de facturación tenga el emisor.

    ``usa_letra`` / ``exige_letra`` (I6-4): en AFIP la numeración es correlativa
    por punto de venta **y por tipo de comprobante**, y la letra es parte del tipo
    (Factura A = CbteTipo 1, Factura B = 6: cada una con su secuencia). Un
    Responsable Inscripto que emite A y B desde el mismo punto de venta repite
    números en las dos letras. Por eso la letra entra en la clave cuando el tipo
    la usa, y en factura / nota de crédito / nota de débito, si no viene, no hay
    clave: suponer que la letra no importa es la clave fuerte falsa que este
    módulo existe para no producir. El recibo también se numera por letra, pero
    sin letra sigue con la clave de siempre (es el formato que ya estaba
    guardado, y sin letra no hay de dónde sacar una).
    """

    codigo: str
    etiqueta: str
    exige_serie: bool
    usa_letra: bool = False
    exige_letra: bool = False


#: Catálogo CERRADO. Un tipo que no está acá no produce clave fuerte
#: (``MOTIVO_TIPO_DESCONOCIDO``): no sabemos qué componentes lo distinguen, y
#: suponer que el número alcanza es exactamente el error que este módulo evita.
FORMATOS_DE_COMPROBANTE: dict[str, FormatoDeComprobante] = {
    "factura": FormatoDeComprobante(
        "factura", "Factura", exige_serie=True, usa_letra=True, exige_letra=True
    ),
    "nota_credito": FormatoDeComprobante(
        "nota_credito", "Nota de crédito", exige_serie=True, usa_letra=True, exige_letra=True
    ),
    "nota_debito": FormatoDeComprobante(
        "nota_debito", "Nota de débito", exige_serie=True, usa_letra=True, exige_letra=True
    ),
    "recibo": FormatoDeComprobante("recibo", "Recibo", exige_serie=True, usa_letra=True),
    "remito": FormatoDeComprobante("remito", "Remito", exige_serie=True),
    "ticket": FormatoDeComprobante("ticket", "Ticket", exige_serie=True),
}

#: Letras de los comprobantes fiscales argentinos (A, B, C, M, E de exportación,
#: T de turismo). Una letra fuera de este set no se lee como letra.
LETRAS_FISCALES: frozenset[str] = frozenset({"a", "b", "c", "e", "m", "t"})

#: Cómo se escribe cada tipo en una planilla real. Se normaliza el texto (sin
#: tildes, sin puntuación) antes de buscar acá. El segundo elemento es la letra
#: fiscal cuando el alias la trae pegada ("fa" = factura A); ``None`` si no.
#: "fc" es FACTURA sin letra (así lo escriben Tango y la mayoría: "FC A 0001-…"),
#: no "factura C": leerla como C sería inventar una letra.
_ALIAS_DE_TIPO: dict[str, tuple[str, str | None]] = {
    "factura": ("factura", None),
    "fc": ("factura", None),
    "fac": ("factura", None),
    "f": ("factura", None),
    "fa": ("factura", "a"),
    "fb": ("factura", "b"),
    "nota de credito": ("nota_credito", None),
    "nota credito": ("nota_credito", None),
    "nc": ("nota_credito", None),
    "notacredito": ("nota_credito", None),
    "nota de debito": ("nota_debito", None),
    "nota debito": ("nota_debito", None),
    "nd": ("nota_debito", None),
    "recibo": ("recibo", None),
    "rec": ("recibo", None),
    "rx": ("recibo", None),
    "remito": ("remito", None),
    "rem": ("remito", None),
    "r": ("remito", None),
    "ticket": ("ticket", None),
    "tique": ("ticket", None),
    "tk": ("ticket", None),
    "t": ("ticket", None),
}

#: Abreviaturas a las que se les pega la letra sin espacio ("FCA", "NCB"). Sólo
#: las de dos letras o más: "fe" no es "factura E" (suele ser "factura
#: electrónica"), y adivinar ahí es adivinar identidad.
_BASES_CON_LETRA_PEGADA: frozenset[str] = frozenset({"fc", "nc", "nd"})


def leer_tipo_de_comprobante(valor: Any) -> tuple[str | None, str | None]:
    """Texto de la celda → ``(código del catálogo, letra fiscal)``.

    ``(None, None)`` si el tipo no se reconoce; la letra es ``None`` si no viene.
    "Factura A", "FC B", "FCA" y "FA" traen la letra; "Factura" y "FC", no.
    """
    texto = _normalizar(valor)
    if not texto:
        return None, None
    if texto in _ALIAS_DE_TIPO:
        return _ALIAS_DE_TIPO[texto]
    # "factura a" / "fc b" / "nc c": la letra fiscal final, separada.
    partes = texto.rsplit(" ", 1)
    if len(partes) == 2 and partes[1] in LETRAS_FISCALES and partes[0] in _ALIAS_DE_TIPO:
        tipo, letra = _ALIAS_DE_TIPO[partes[0]]
        if letra is None:
            return tipo, partes[1]
    # "fca" / "ncb": la letra pegada a la abreviatura.
    if len(texto) >= 3 and texto[:-1] in _BASES_CON_LETRA_PEGADA and texto[-1] in LETRAS_FISCALES:
        return _ALIAS_DE_TIPO[texto[:-1]][0], texto[-1]
    return None, None


def normalizar_tipo_de_comprobante(valor: Any) -> str | None:
    """Texto de la celda → código del catálogo, o ``None`` si no se reconoce.

    Tolera la letra fiscal pegada ("Factura A", "FC-B"), pero acá sólo devuelve
    el tipo: la letra la lee ``leer_tipo_de_comprobante``, y SÍ distingue el
    documento (I6-4: cada letra tiene su propia numeración).
    """
    return leer_tipo_de_comprobante(valor)[0]


def leer_letra_del_numero(valor: Any) -> str | None | Literal[False]:
    """La letra fiscal escrita adentro del número ("A 0001-00000123",
    "FC B 0001-…", "A0001-…"), ``None`` si no hay, o ``False`` si hay dos letras
    distintas (no se sabe cuál es el comprobante).

    Leerla no es adivinar: está en el dato, igual que el punto de venta del
    formato ``PPPP-NNNNNNNN``. Un número sin dígitos (identificador alfanumérico
    raro) no tiene letra: sus letras SON el número.
    """
    texto = _normalizar(valor)
    if not texto or not any(c.isdigit() for c in texto):
        return None
    letras: set[str] = set()
    for token in re.findall(r"[a-z]+", texto):
        if token in LETRAS_FISCALES and token not in _ALIAS_DE_TIPO:
            letras.add(token)
        elif token in _ALIAS_DE_TIPO:
            letra = _ALIAS_DE_TIPO[token][1]
            if letra is not None:
                letras.add(letra)
        elif (
            len(token) >= 3
            and token[:-1] in _BASES_CON_LETRA_PEGADA
            and token[-1] in LETRAS_FISCALES
        ):
            letras.add(token[-1])
    if len(letras) > 1:
        return False
    return next(iter(letras), None)


# ── Normalización ────────────────────────────────────────────────────────────
def _normalizar(valor: Any) -> str:
    """Minúsculas, sin tildes, sin espacios repetidos. Vacío → ``""``."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def _solo_digitos(valor: Any) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def leer_numero_de_comprobante(valor: Any) -> tuple[str, str]:
    """``"0001-00000123"`` → ``("1", "123")``: punto de venta y número, separados.

    Existe porque el mismo comprobante se escribe de las dos formas: algunos
    exportadores ponen ``0001-00000123`` en una sola columna y otros parten el
    punto de venta en una columna propia. Si esas dos formas produjeran claves
    distintas, el archivo B no reconocería nada de lo que trajo el archivo A —
    la deduplicación existiría y no serviría para nada.

    Leer el formato **no es adivinar**: ``PPPP-NNNNNNNN`` es cómo se escribe un
    comprobante fiscal argentino, y el separador está en el dato. Lo que sí sería
    adivinar —y no se hace— es suponer un punto de venta cuando no viene por
    ningún lado.

    Los ceros a la izquierda se recortan de los DOS componentes: una vez separado
    el punto de venta del número, los dos son enteros que cada sistema escribe con
    el padding que se le ocurre (``0001`` / ``1``, ``00000123`` / ``123``), y
    tratarlos distinto rompería la clave entre dos exportaciones del mismo
    documento. Se conserva la separación, que es lo que evita que ``0001-00000123``
    y ``0012-00000123`` colisionen.

    Devuelve ``("", "")`` si no hay nada legible.
    """
    texto = _normalizar(valor)
    if not texto:
        return "", ""
    # Se conservan sólo dígitos y separadores; la letra fiscal ("A 0001-123") la
    # lee aparte `leer_letra_del_numero` (I6-4: distingue el documento), y el
    # prefijo alfabético de algunos sistemas ("FC0001-123") no aporta identidad
    # que no esté en el tipo.
    partes = [p for p in re.split(r"[^0-9]+", texto) if p]
    if not partes:
        # Identificador alfanumérico raro: se conserva normalizado (sigue siendo
        # estable) y sin punto de venta.
        return "", texto.replace(" ", "")
    if len(partes) == 1:
        return "", _sin_ceros(partes[0])
    # Con más de dos grupos manda el último como número y el anteúltimo como
    # punto de venta: es la forma de los sistemas que anteponen el tipo
    # ("001-0001-00000123").
    return _sin_ceros(partes[-2]), _sin_ceros(partes[-1])


def _sin_ceros(digitos: str) -> str:
    return digitos.lstrip("0") or "0"


def normalizar_serie(valor: Any) -> str:
    """Punto de venta: sólo dígitos, sin ceros a la izquierda (``0001`` == ``1``)."""
    digitos = _solo_digitos(valor)
    if digitos:
        return _sin_ceros(digitos)
    return _normalizar(valor).replace(" ", "")


def normalizar_identificador_fiscal(valor: Any) -> str:
    """CUIT/CUIL a sólo dígitos. ``"30-71234567-8"`` → ``"30712345678"``.

    No se valida el dígito verificador acá: un CUIT mal tipeado sigue siendo un
    identificador estable dentro del tenant, y rechazarlo mandaría a candidatos
    un documento que sí se puede deduplicar. Lo que NO se acepta es algo que no
    tenga la longitud de un CUIT/CUIL — ahí no es un identificador fiscal.
    """
    digitos = _solo_digitos(valor)
    return digitos if len(digitos) == 11 else ""


# ── Clave de operación ───────────────────────────────────────────────────────
#: El emisor de una venta propia. Constante porque la tabla de identidades ya
#: está scopeada por tenant: el negocio es el mismo en todas sus filas. Existe
#: como literal para que la clave se lea y para que un futuro comprobante de
#: terceros tenga dónde meterse sin colisionar con lo ya guardado.
EMISOR_PROPIO = "propio"


def clave_de_operacion(
    fila: dict[str, Any],
    cols: dict[str, str],
    entity: str,
) -> Resultado:
    """Identidad fuerte de la fila, o el motivo por el que no la tiene.

    ``cols`` es el mapeo EFECTIVO ``{campo canónico: nombre de columna}`` — el
    mismo que usa el importador. Una columna no mapeada no se busca por
    heurística: adivinar cuál es el número de comprobante es adivinar identidad.
    """

    def celda(campo: str) -> Any:
        col = cols.get(campo)
        return fila.get(col) if col else None

    # 1) ID de la operación en el sistema de origen. Gana sobre el comprobante:
    #    es el identificador que ese sistema garantiza único, mientras que el
    #    comprobante se reconstruye de tres columnas.
    #    I6-3: la ENTIDAD entra en la clave. El sistema garantiza el ID único
    #    dentro de SU tabla, no entre ventas y compras: muchos numeran cada tabla
    #    por separado, y una tabla mixta (Libro Diario) produce venta y gasto desde
    #    las mismas filas. Sin la entidad, la venta reclamaba la clave del gasto y
    #    todos los gastos se omitían como «ya aplicados».
    id_externo = _normalizar(celda("external_operation_id"))
    if id_externo:
        sistema = _normalizar(celda("external_source"))
        if not sistema:
            return SinClave(MOTIVO_SISTEMA_NO_DECLARADO)
        base_ext = f"{CLAVE_VERSION}:ext:{entity}:{sistema}:{id_externo}"
        linea = _normalizar(celda("external_line_id"))
        if linea:
            return ClaveDeOperacion(f"{base_ext}#{linea}", "linea")
        return ClaveDeOperacion(base_ext, "documento")

    # 2) Identidad de comprobante.
    numero_crudo = celda("invoice_number")
    tipo_crudo = celda("document_type")
    if not _normalizar(numero_crudo) and not _normalizar(tipo_crudo):
        return SinClave(MOTIVO_SIN_COLUMNAS)

    serie_del_numero, numero = leer_numero_de_comprobante(numero_crudo)
    if not numero:
        return SinClave(MOTIVO_NUMERO_FALTANTE)

    if not _normalizar(tipo_crudo):
        return SinClave(MOTIVO_TIPO_NO_DECLARADO)
    tipo, letra_del_tipo = leer_tipo_de_comprobante(tipo_crudo)
    if tipo is None:
        return SinClave(MOTIVO_TIPO_DESCONOCIDO)
    formato = FORMATOS_DE_COMPROBANTE[tipo]

    serie_de_columna = normalizar_serie(celda("document_series"))
    if serie_de_columna and serie_del_numero and serie_de_columna != serie_del_numero:
        # Las dos fuentes dicen cosas distintas. Elegir una sería inventar cuál
        # de los dos comprobantes es: va a candidatos.
        return SinClave(MOTIVO_SERIE_CONTRADICTORIA)
    serie = serie_de_columna or serie_del_numero
    if formato.exige_serie and not serie:
        return SinClave(MOTIVO_SERIE_FALTANTE)

    # I6-4: la letra fiscal, de la columna de tipo o de adentro del número. Si las
    # dos fuentes la traen y no coinciden, mismo criterio que con la serie.
    letra: str | None = None
    if formato.usa_letra:
        letra_del_numero = leer_letra_del_numero(numero_crudo)
        if letra_del_numero is False or (
            letra_del_tipo and letra_del_numero and letra_del_tipo != letra_del_numero
        ):
            return SinClave(MOTIVO_LETRA_CONTRADICTORIA)
        letra = letra_del_tipo or letra_del_numero or None
        if formato.exige_letra and not letra:
            return SinClave(MOTIVO_LETRA_FALTANTE)

    emisor = _emisor(celda, entity)
    if emisor is None:
        return SinClave(MOTIVO_EMISOR_SIN_IDENTIFICADOR)

    if letra:
        base = f"{CLAVE_VERSION}:doc:{emisor}:{tipo}:{letra}:{serie}:{numero}"
    else:
        # Formato sin letra (remito, ticket, recibo sin letra): la clave de
        # siempre, byte a byte, para que lo ya guardado siga matcheando.
        base = f"{CLAVE_VERSION_K1}:doc:{emisor}:{tipo}:{serie}:{numero}"
    linea = _normalizar(celda("external_line_id"))
    if linea:
        return ClaveDeOperacion(f"{base}#{linea}", "linea")
    return ClaveDeOperacion(base, "documento")


def clave_legada_k1(
    fila: dict[str, Any],
    cols: dict[str, str],
    entity: str,
) -> ClaveDeOperacion | None:
    """La clave que esta fila producía ANTES de I6-3/I6-4, si era distinta de la
    de hoy. ``None`` si no producía clave o si es la misma (el ID externo
    siempre cambió; el comprobante, sólo si es de un tipo con letra).

    Existe por compatibilidad, no como identidad: en producción ya hay
    identidades guardadas con el formato viejo (``k1:ext:{sistema}:{id}`` sin la
    entidad, ``k1:doc:…:factura:…`` sin la letra) y sus vínculos. Si el servicio
    sólo buscara la clave nueva, reimportar un archivo ya importado lo aplicaría
    de nuevo entero. No se migran (eso sería un backfill sobre plata real): se
    las sigue BUSCANDO, y la decisión que se toma con ellas es conservadora (ver
    ``operation_identity_service._decidir_por_legada``).

    Reproduce la regla vieja a propósito, con sus defectos: la letra se
    descarta y el ID externo no lleva la entidad. No se usa para reclamar nunca.
    """

    def celda(campo: str) -> Any:
        col = cols.get(campo)
        return fila.get(col) if col else None

    id_externo = _normalizar(celda("external_operation_id"))
    if id_externo:
        sistema = _normalizar(celda("external_source"))
        if not sistema:
            return None
        linea = _normalizar(celda("external_line_id"))
        vieja = f"{CLAVE_VERSION_K1}:ext:{sistema}:{id_externo}"
        if linea:
            return ClaveDeOperacion(f"{vieja}#{linea}", "linea")
        return ClaveDeOperacion(vieja, "documento")

    numero_crudo = celda("invoice_number")
    tipo_crudo = celda("document_type")
    serie_del_numero, numero = leer_numero_de_comprobante(numero_crudo)
    if not numero or not _normalizar(tipo_crudo):
        return None
    tipo = _tipo_k1(tipo_crudo)
    if tipo is None:
        return None
    serie_de_columna = normalizar_serie(celda("document_series"))
    if serie_de_columna and serie_del_numero and serie_de_columna != serie_del_numero:
        return None
    serie = serie_de_columna or serie_del_numero
    if FORMATOS_DE_COMPROBANTE[tipo].exige_serie and not serie:
        return None
    emisor = _emisor(celda, entity)
    if emisor is None:
        return None
    vieja = f"{CLAVE_VERSION_K1}:doc:{emisor}:{tipo}:{serie}:{numero}"
    linea = _normalizar(celda("external_line_id"))
    legada = (
        ClaveDeOperacion(f"{vieja}#{linea}", "linea")
        if linea
        else ClaveDeOperacion(vieja, "documento")
    )
    actual = clave_de_operacion(fila, cols, entity)
    if isinstance(actual, ClaveDeOperacion) and actual.clave == legada.clave:
        # Formato que no cambió: la clave de hoy ES la guardada.
        return None
    return legada


def _tipo_k1(valor: Any) -> str | None:
    """El tipo como lo leía la regla vieja: cualquier letra suelta al final se
    descartaba. Copia exacta, para que ``clave_legada_k1`` reproduzca las claves
    guardadas y no otras."""
    texto = _normalizar(valor)
    if not texto:
        return None
    if texto in _ALIAS_DE_TIPO:
        return _ALIAS_DE_TIPO[texto][0]
    if texto in ("factura a", "factura b", "factura c"):
        return "factura"
    partes = texto.rsplit(" ", 1)
    if (
        len(partes) == 2
        and len(partes[1]) == 1
        and partes[1].isalpha()
        and partes[0] in _ALIAS_DE_TIPO
    ):
        return _ALIAS_DE_TIPO[partes[0]][0]
    return None


def _emisor(celda: Callable[[str], Any], entity: str) -> str | None:
    """Quién EMITIÓ el comprobante, con identificador estable.

    Compra (``expense``): lo emitió el proveedor → su CUIT/CUIL. El nombre no
    sirve como identidad (se escribe de mil formas y dos proveedores pueden
    llamarse igual).

    Venta (``sale``): el comprobante lo emitió el propio negocio. El cliente es
    el RECEPTOR — usarlo como emisor mezclaría la identidad del documento con la
    de la contraparte, y dos facturas propias a clientes distintos con el mismo
    número no pueden existir de todos modos.
    """
    if entity == "sale":
        return EMISOR_PROPIO
    if entity == "expense":
        for campo in ("supplier_cuit", "supplier_cuil"):
            fiscal = normalizar_identificador_fiscal(celda(campo))
            if fiscal:
                return fiscal
        return None
    return None


# ── Huella de contenido ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class LineaEfectiva:
    """Los valores de una línea que PRODUCEN efectos, ya interpretados.

    No es el texto de la celda: es lo que el importador va a persistir. Comparar
    texto crudo diría que ``"1.500"`` y ``"1500"`` son documentos distintos, y
    comparar sólo el monto diría que dos remitos con los mismos totales y
    productos distintos son el mismo.
    """

    monto: Decimal | None
    fecha: date | datetime | None
    cantidad: int | None
    precio_unitario: Decimal | None
    producto: str | None = None
    descuento: Decimal | None = None
    impuestos: Decimal | None = None

    def _canonica(self) -> tuple[str, ...]:
        return (
            _decimal(self.monto),
            _fecha(self.fecha),
            "" if self.cantidad is None else str(self.cantidad),
            _decimal(self.precio_unitario),
            _normalizar(self.producto),
            _decimal(self.descuento),
            _decimal(self.impuestos),
        )


def _decimal(valor: Decimal | None) -> str:
    """Forma canónica de un importe: ``1500``, ``1500.0`` y ``1500.00`` son el mismo.

    Sin normalizar la escala, el mismo documento reexportado con otro formato de
    celda daría otra huella y se vería como conflicto.
    """
    if valor is None:
        return ""
    normalizado = valor.normalize()
    # `normalize()` deja notación científica en los enteros grandes (1E+3).
    if normalizado == normalizado.to_integral_value():
        return str(normalizado.quantize(Decimal(1)))
    return str(normalizado)


def _fecha(valor: date | datetime | None) -> str:
    """Sólo el DÍA. La hora no entra: dos exportaciones del mismo hecho pueden
    traer horas distintas (o ninguna) y siguen siendo el mismo documento."""
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    return valor.isoformat()


def huella_de_contenido(lineas: list[LineaEfectiva]) -> str:
    """Huella del documento COMPLETO, preservando repeticiones.

    Ordenada: reordenar los renglones de una planilla no cambia el documento, y
    una huella sensible al orden convertiría cada reordenamiento en un conflicto.
    Preservando repeticiones: un remito con dos renglones idénticos legítimos no
    es lo mismo que uno con un solo renglón — colapsarlos borraría plata.

    La cantidad de líneas entra explícita para que un documento no pueda
    matchear a un subconjunto de sí mismo.
    """
    canonicas = sorted(linea._canonica() for linea in lineas)
    payload = f"{CONTENIDO_VERSION}|{len(canonicas)}|" + "|".join(
        "\x1f".join(c) for c in canonicas
    )
    return f"{CONTENIDO_VERSION}:{hashlib.sha256(payload.encode()).hexdigest()}"


# ── Campos que participan de la identidad ────────────────────────────────────
#: Los campos canónicos que este módulo lee. Los usa el importador para saber si
#: vale la pena armar la pasada de identidad (si el archivo no mapeó ninguno, no
#: hay nada que calcular y el confirm no paga ni una query).
CAMPOS_DE_IDENTIDAD: frozenset[str] = frozenset(
    {
        "external_operation_id",
        "external_source",
        "external_line_id",
        "invoice_number",
        "document_type",
        "document_series",
        "supplier_cuit",
        "supplier_cuil",
    }
)


def hay_columnas_de_identidad(cols: dict[str, str]) -> bool:
    """¿Este mapeo puede llegar a producir una clave fuerte?

    Basta con que esté mapeado un ANCLA (el ID externo o el número de
    comprobante): los demás campos sólo completan la clave. Sin ancla no hay
    nada que intentar.
    """
    return bool(cols.get("external_operation_id") or cols.get("invoice_number"))
