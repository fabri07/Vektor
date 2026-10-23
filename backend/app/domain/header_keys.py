"""Clave de matching heurístico para headers de planillas.

Función pura, sin dependencias: la comparten el mapeo de columnas
(``column_mapping_service``) y el clasificador de hojas (``file_parsing``), que
tienen el mismo problema — "Precio de compra" y "Precio compra" son el mismo
header para una heurística, y declarar las dos variantes en cada set de keywords
es inmantenible.

Lo que NO hay que hacer con esto: aplicarlo a un valor que se persista. La forma
normalizada que se guarda en ``tenant_column_mappings.source_column`` es la de
``_normalize_col``; colapsar preposiciones ahí dejaría huérfano el historial de
alias aprendidos por cada tenant.
"""

from __future__ import annotations

import re

from app.domain.text_norm import normalize_text, repair_mojibake

# Preposiciones y artículos que no aportan al matching.
STOPWORDS: frozenset[str] = frozenset({"de", "del", "la", "el", "los", "las", "por"})

#: Tope de la clave. La columna es ``String(80)``; se corta antes para dejar
#: lugar al sufijo de desambiguación (``_2``, ``_10``) sin volver a pasarse.
_SLUG_MAX = 72

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def fold_header(raw: str) -> str:
    """Encabezado → forma comparable contra keywords: minúsculas, SIN acentos ni
    ñ, con ``_`` de separador.

    Existe porque en los archivos reales la misma columna viene escrita de las
    dos maneras — ``Descripción`` y ``Descripcion``, ``Año`` y ``Ano``,
    ``Cumpleaños`` y ``Cumpleanos``— y hasta acá el matching era sensible al
    acento: cada keyword tenía que declarar sus dos formas a mano, cosa que sólo
    funciona mientras alguien se acuerde de escribir la segunda.

    Pliega también NFC vs NFD: Excel y macOS exportan la "ñ" tanto como un solo
    carácter (U+00F1) como ``n`` + tilde combinante (U+006E U+0303), que son
    indistinguibles en pantalla y distintos byte a byte.

    Los acentos del castellano son length-preserving bajo esta cadena (NFKD +
    descartar diacríticos): ``comisión`` y ``comision`` miden 8. Importa porque
    ``_heuristic_match`` desempata por longitud del keyword — plegar no le mueve
    ningún desempate.

    Antes de plegar repara el mojibake (``DescripciÃ³n`` → ``Descripción``): un
    encabezado doble-codificado no se parece a ninguna palabra del castellano y
    hoy no matchea nada, así que la columna cae a "sin mapear" aunque el archivo
    tenga la columna de siempre. Se repara sólo para COMPARAR — el encabezado
    crudo se conserva tal cual, que es con el que se lee la celda de cada fila.
    """
    return normalize_text(repair_mojibake(raw)).replace(" ", "_")


def match_key(normalized: str) -> str:
    """El header ya normalizado, sin preposiciones ni acentos.

    Espera la salida de una normalización previa (lowercase + underscores), no un
    header crudo. Vuelve a plegar por ``fold_header`` de todos modos: la
    normalización que la alimenta (``_normalize_col``) NO puede tocar acentos
    —es la forma que se persiste en ``tenant_column_mappings.source_column``— así
    que la clave de matching es el único lugar donde se pueden sacar.
    """
    folded = fold_header(normalized)
    parts = [p for p in folded.split("_") if p and p not in STOPWORDS]
    # Un header que sea SOLO stopwords ("de") dejaría la clave vacía; se devuelve
    # el original antes que una cadena vacía.
    return "_".join(parts) or folded


def custom_field_slug(header: str | None) -> str | None:
    """Encabezado crudo → clave de campo propio, o ``None`` si no queda nada.

    F-A. Es un IDENTIFICADOR que se persiste en
    ``tenant_custom_field_definitions.field_key`` (``String(80)``), no una clave
    de matching: **no confundir con `_normalize_col`**, que alimenta
    ``tenant_column_mappings.source_column`` y no se puede tocar sin migrar los
    alias aprendidos por cada tenant. `_normalize_col` además no sirve acá —
    deja acentos y puntos (``"P. Venta"`` → ``"p._venta"``).

    La forma que produce es la que ``POST /fields`` ya exige a mano
    (``^[a-z][a-z0-9_]*$``, 2–80). Hasta acá la ingesta no validaba nada y podía
    escribir claves que su propia API rechazaba.

    Devuelve ``None`` —y no ``""``— cuando el encabezado no aporta ni un
    carácter usable (vacío, sólo espacios, sólo puntuación): el caller tiene que
    decidir qué hacer con una columna sin nombre, no recibir una clave vacía que
    después colisione con la de al lado.
    """
    if header is None:
        return None
    # Acentos por el normalizador canónico del proyecto: escribir otra cadena
    # NFKD acá es justo lo que `text_norm` pide no hacer. La reparación de
    # mojibake va primero porque acá el resultado se PERSISTE como `field_key`:
    # sin ella "DescripciÃ³n" quedaría clavado como `descripcia3n`.
    base = normalize_text(repair_mojibake(header))
    slug = _NON_ALNUM.sub("_", base).strip("_")
    if not slug:
        return None
    # Un identificador no puede arrancar con dígito ("2024" → "c_2024") ni medir
    # menos de dos caracteres ("A" → "c_a"): son las dos reglas que el schema de
    # `/fields` ya exige (`^[a-z][a-z0-9_]*$`, min_length=2). El mismo prefijo
    # resuelve las dos, así que una columna "1" y una columna "A" quedan igual de
    # válidas sin inventar un segundo mecanismo.
    if slug[0].isdigit() or len(slug) < 2:
        slug = f"c_{slug}"
    # Cortar por el tope y volver a limpiar: truncar puede dejar un `_` colgando.
    return slug[:_SLUG_MAX].rstrip("_") or None
