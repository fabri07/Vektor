"""Credencial de una terminal de caja (B6). Puro: sin sesión ni ORM.

Una terminal es una PC habilitada por el dueño para cobrar. Su credencial
identifica LA PC, no a la persona: nunca reemplaza el login del usuario, así que
un secreto robado por sí solo no da acceso a nada. Sirve para dos cosas: que un
cajero sólo cobre desde una caja habilitada, y que al sincronizar ventas
offline (B7) se pueda rechazar lo que viene de una PC dada de baja.

El secreto se muestra UNA vez al habilitar y se guarda sólo su sha256: igual que
una contraseña, la base no puede devolverlo.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: Header en el que viaja el secreto.
TERMINAL_HEADER = "X-POS-Terminal"


def generar_secreto() -> str:
    """256 bits de azar, url-safe. Adivinarlo no es un ataque práctico."""
    return secrets.token_urlsafe(32)


def hash_de_secreto(secreto: str) -> str:
    return hashlib.sha256(secreto.encode("utf-8")).hexdigest()


def secreto_coincide(secreto: str, hash_guardado: str) -> bool:
    """Comparación en tiempo constante: no filtra cuántos caracteres coinciden."""
    return hmac.compare_digest(hash_de_secreto(secreto), hash_guardado)


#: Lo manda SÓLO el motor de sincronización de la caja: "esto se hizo sin
#: conexión y recién ahora llega". En la venta relaja el invariante de stock
#: (B11); en el aprendizaje de un código sólo queda en la auditoría.
OFFLINE_HEADER = "X-POS-Offline"

#: Lo que cuenta como "sí" en ese header. Un `bool(valor)` pelado tomaría `"0"`
#: como verdadero —toda cadena no vacía lo es—, y ese header relaja el
#: invariante de stock: no puede depender de leer mal un cero.
_OFFLINE_SI = frozenset({"1", "true", "yes"})


def es_offline(valor: str | None) -> bool:
    return valor is not None and valor.strip().lower() in _OFFLINE_SI
