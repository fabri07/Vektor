"""Validadores fiscales argentinos compartidos (proveedores y clientes).

CUIT/CUIL: ``XX-XXXXXXXX-X`` (guiones opcionales). Se valida el formato Y el
dígito verificador (módulo 11) — así no entra un número bien formateado pero
inválido. NULL/vacío se aceptan (el campo es opcional a nivel API; la
obligatoriedad la aplica el formulario, con excepción para sentinela/import).

DNI: 7 u 8 dígitos.
"""

import re

from pydantic_core import PydanticCustomError

# CUIT/CUIL argentino. ``_CUIT_RE`` valida estructura; los pesos calculan el dígito.
_CUIT_RE = re.compile(r"^\d{2}-?\d{8}-?\d$")
_CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_DNI_RE = re.compile(r"^\d{7,8}$")


def cuit_check_digit_ok(digits: str) -> bool:
    """¿El 11º dígito de un CUIT/CUIL (ya sin guiones, 11 dígitos) es el correcto?"""
    acc = sum(int(digits[i]) * _CUIT_WEIGHTS[i] for i in range(10))
    verifier = 11 - (acc % 11)
    if verifier == 11:
        verifier = 0
    elif verifier == 10:
        verifier = 9
    return verifier == int(digits[10])


def validate_cuit(value: str | None) -> str | None:
    """Valida formato + dígito verificador. None/vacío pasan (campo opcional)."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _CUIT_RE.match(cleaned):
        raise PydanticCustomError(
            "cuit_format", "CUIT/CUIL inválido: formato esperado XX-XXXXXXXX-X."
        )
    if not cuit_check_digit_ok(cleaned.replace("-", "")):
        raise PydanticCustomError(
            "cuit_check_digit", "CUIT/CUIL inválido: el dígito verificador no coincide."
        )
    return cleaned


def validate_dni(value: str | None) -> str | None:
    """Valida 7-8 dígitos. None/vacío pasan (campo opcional)."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _DNI_RE.match(cleaned):
        raise PydanticCustomError(
            "dni_format", "DNI inválido: se esperan 7 u 8 dígitos."
        )
    return cleaned


def normalize_phone(value: str | None) -> str | None:
    """Normaliza un teléfono opcional: strip; vacío → None.

    Única normalización de ``phone`` en el backend (registro y PATCH /users/me
    la comparten) — si algún día se valida formato o se quitan separadores,
    se hace acá y aplica a todos los writers.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


#: Condición frente al IVA. Catálogo cerrado, compartido por clientes y
#: proveedores: vivía sólo en ``schemas/customer.py`` y cuando la ficha fiscal
#: llegó a proveedores hubiera quedado una segunda copia — que es como los dos
#: catálogos se van separando sin que nadie lo note.
IVA_CONDITIONS = frozenset(
    {"consumidor_final", "monotributo", "responsable_inscripto", "exento"}
)


def validate_iva_condition(v: str | None) -> str | None:
    """Condición de IVA contra el catálogo cerrado. NULL/vacío → None."""
    if v is None or v == "":
        return None
    if v not in IVA_CONDITIONS:
        raise PydanticCustomError(
            "iva_condition_invalid",
            "iva_condition inválida: consumidor_final | monotributo | "
            "responsable_inscripto | exento.",
        )
    return v
