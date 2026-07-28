"""Dominio de leads del formulario público de contacto.

Enums canónicos, normalización de datos (email/teléfono AR) y la versión vigente
del texto de consentimiento de privacidad. Sin dependencias de infraestructura.
"""

from __future__ import annotations

import re
from enum import StrEnum

# Versión del texto de política de privacidad que el visitante acepta al enviar
# el formulario. Se persiste junto al lead. Subir esta constante cuando cambie el
# contenido legal de /privacidad.
CONSENT_VERSION = "2026-07-v1"

# Anti-spam: mínimo tiempo (ms) entre que se renderiza el form y se envía. Un
# envío más rápido que esto casi siempre es un bot.
MIN_SUBMIT_ELAPSED_MS = 1500

# Ventana (segundos) para considerar un lead como duplicado reciente (doble clic
# o reintento del navegador con los mismos datos).
DEDUP_WINDOW_SECONDS = 600


class LeadStatus(StrEnum):
    """Estado operativo del lead en el pipeline comercial."""

    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    DISCARDED = "discarded"


class EmailNotificationStatus(StrEnum):
    """Estado del envío de la notificación por email al equipo de Véktor."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class LeadRubro(StrEnum):
    """Rubros/verticales soportados (espeja los verticales del producto)."""

    KIOSCO_ALMACEN = "kiosco_almacen"
    LIMPIEZA = "limpieza"
    DECORACION_HOGAR = "decoracion_hogar"
    OTRO = "otro"


class LeadCantidadUsuarios(StrEnum):
    """Tramos de cantidad de usuarios que van a usar el sistema (como adhoc)."""

    R1_5 = "1_5"
    R6_10 = "6_10"
    R11_20 = "11_20"
    R21_30 = "21_30"
    R31_40 = "31_40"
    R41_50 = "41_50"
    R51_80 = "51_80"
    R80_PLUS = "80_plus"


def looks_like_bot(*, website: str | None, elapsed_ms: int | None) -> bool:
    """Honeypot completado o envío sospechosamente rápido ⇒ bot.

    La regla vive en el dominio (y no en el router de contacto) porque la usan los
    DOS formularios públicos anónimos del sistema —contacto y solicitud de acceso—
    y una segunda copia se desincroniza. ``api/v1/contact.py:_looks_like_bot``
    delega acá sin cambiar su firma ni su comportamiento.
    """
    if website:  # el honeypot debe venir vacío
        return True
    return elapsed_ms is not None and elapsed_ms < MIN_SUBMIT_ELAPSED_MS


def normalize_email(raw: str) -> str:
    """Normaliza un email: trim + lowercase. La validez de formato la garantiza
    Pydantic (EmailStr) antes de llamar acá."""
    return raw.strip().lower()


def normalize_ar_phone(raw: str) -> str:
    """Normaliza un teléfono argentino a un formato compacto.

    - Conserva un único '+' inicial si viene con código de país.
    - Elimina espacios, guiones, paréntesis y puntos.
    - No inventa código de área; solo limpia. Si queda vacío tras limpiar,
      devuelve cadena vacía (el validador de longitud lo rechazará arriba).
    """
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    return f"+{digits}" if has_plus and digits else digits
