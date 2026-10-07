"""Huella de la fila original de un registro importado (B·nuevo, 2026-10-07).

Una edición a mano se preserva en la relectura por su ``source_row_ref``, que es
la POSICIÓN de su fila en el archivo. Si las filas se corren, esa posición ya no
es su fila. La huella dice qué fila era, por su contenido: el importador la
escribe en el mismo INSERT que crea el registro, y la edición manual no la toca.

Se compara la salida del parser con la salida del parser, sin interpretar nada:
ni mayúsculas, ni ``007`` contra ``7``, ni fechas. Si el parser cambia, la huella
no casa y la relectura se comporta como antes de esto.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

PREFIJO = "f1:"


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    return unicodedata.normalize("NFC", str(valor)).strip()


def huella_de_fila(
    fila: dict[str, Any], tipo: str, originales: dict[str, Any] | None = None
) -> str:
    """``"f1:<sha256>"`` de las celdas de la fila más el tipo de registro.

    Entran todas las claves que no empiezan con ``__`` (las del sistema), vacías
    incluidas. ``originales``: el valor tal como venía en el archivo para las
    celdas que el importador ya interpretó (``__originales__``), así la huella es
    la del archivo y no la de la interpretación.
    """
    orig = originales or {}
    pares = sorted(
        (str(k), _texto(orig.get(k, v))) for k, v in fila.items() if not str(k).startswith("__")
    )
    crudo = json.dumps([tipo, pares], ensure_ascii=False, separators=(",", ":"))
    return PREFIJO + hashlib.sha256(crudo.encode()).hexdigest()
