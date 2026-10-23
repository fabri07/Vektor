"""Schemas del formulario público de contacto."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.domain.contact_lead import (
    LeadCantidadUsuarios,
    LeadRubro,
    normalize_ar_phone,
    normalize_email,
)


class CreateLeadRequest(BaseModel):
    """Payload del formulario 'Contactanos'.

    Normaliza/valida en backend: email lowercase, teléfono AR compacto, límites
    de longitud, enums para rubro y cantidad de usuarios, consentimiento
    obligatorio. Incluye campos anti-spam (honeypot + tiempo de envío).
    """

    nombre: str = Field(min_length=2, max_length=120)
    # pattern exige al menos 6 dígitos (built-in ⇒ error 422 serializable).
    celular: str = Field(min_length=6, max_length=40, pattern=r"(?:\D*\d){6,}")
    email: EmailStr
    empresa: str = Field(min_length=1, max_length=160)
    rubro: LeadRubro
    cantidad_usuarios: LeadCantidadUsuarios
    gestion_actual: str | None = Field(default=None, max_length=2000)

    # Consentimiento de privacidad OBLIGATORIO: Literal[True] ⇒ enviar False/omitir
    # produce un error de validación built-in (serializable), sin ValueError custom.
    consent: Literal[True]
    # Versión del consentimiento que el front declara haber mostrado. Opcional y
    # solo para auditar el contrato: el backend usa SIEMPRE su propia versión
    # autoritativa (`domain.CONSENT_VERSION`) al persistir.
    consent_version: str | None = Field(default=None, max_length=20)

    # Trazabilidad opcional.
    cta_source: str | None = Field(default=None, max_length=60)

    # Anti-spam.
    website: str | None = Field(  # honeypot: debe venir vacío
        default=None, description="No completar (campo trampa)"
    )
    elapsed_ms: int | None = Field(
        default=None, description="ms entre render y envío del formulario"
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(str(v))

    @field_validator("celular")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        # El pattern del Field ya garantizó ≥6 dígitos; acá solo compactamos.
        return normalize_ar_phone(v)

    @field_validator("nombre", "empresa")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class LeadResponse(BaseModel):
    """Respuesta genérica del endpoint público (no filtra datos internos)."""

    status: str = "ok"
    message: str = "¡Gracias! Te vamos a contactar a la brevedad."
