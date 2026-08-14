"""Pydantic schemas for customer endpoints."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator
from pydantic_core import PydanticCustomError

from app.schemas._ar_fiscal import validate_cuit, validate_dni, validate_iva_condition

# Valores canónicos. La obligatoriedad (qué campos exigir) la aplica el endpoint
# manual vía ``require_complete``; el schema solo valida formato/enum siempre, para
# no romper los caminos internos (sentinela, import, reclasificación de "Otros").
CUSTOMER_TYPES = frozenset({"person", "company"})
DOC_TYPES = frozenset({"dni", "cuit"})


def _check_customer_type(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if v not in CUSTOMER_TYPES:
        raise PydanticCustomError(
            "customer_type_invalid", "customer_type debe ser 'person' o 'company'."
        )
    return v


def _check_doc_type(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if v not in DOC_TYPES:
        raise PydanticCustomError("doc_type_invalid", "doc_type debe ser 'dni' o 'cuit'.")
    return v


#: Alias local: la implementación se movió a `_ar_fiscal` cuando proveedores
#: también pasó a tener condición de IVA — dos copias del catálogo es como se
#: separan sin que nadie lo note.
_check_iva = validate_iva_condition


class CustomerResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    customer_type: str | None = None
    last_name: str | None = None
    doc_type: str | None = None
    dni: str | None = None
    cuit: str | None = None
    iva_condition: str | None = None
    email: str | None
    phone: str | None
    telegram_username: str | None
    address: str | None = None
    locality: str | None = None
    province: str | None = None
    postal_code: str | None = None
    birthday: date | None = None
    notes: str | None
    custom_fields: dict[str, Any] = {}
    credit_limit: Decimal | None = None
    created_at: datetime
    deactivated_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_sentinel(self) -> bool:
        return (self.custom_fields or {}).get("_sentinel") in ("true", True)


class CreateCustomerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    customer_type: str | None = Field(default=None, max_length=10)
    last_name: str | None = Field(default=None, max_length=200)
    doc_type: str | None = Field(default=None, max_length=10)
    dni: str | None = Field(default=None, max_length=15)
    cuit: str | None = Field(default=None, max_length=13)
    iva_condition: str | None = Field(default=None, max_length=25)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    telegram_username: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=2000)
    locality: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=12)
    birthday: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    credit_limit: Decimal | None = None

    @field_validator("customer_type")
    @classmethod
    def _v_customer_type(cls, v: str | None) -> str | None:
        return _check_customer_type(v)

    @field_validator("doc_type")
    @classmethod
    def _v_doc_type(cls, v: str | None) -> str | None:
        return _check_doc_type(v)

    @field_validator("iva_condition")
    @classmethod
    def _v_iva(cls, v: str | None) -> str | None:
        return _check_iva(v)

    @field_validator("cuit")
    @classmethod
    def _v_cuit(cls, v: str | None) -> str | None:
        return validate_cuit(v)

    @field_validator("dni")
    @classmethod
    def _v_dni(cls, v: str | None) -> str | None:
        return validate_dni(v)

    def missing_required_fields(self) -> list[str]:
        """Campos obligatorios faltantes para alta manual de un cliente REAL.

        Reglas (AFIP + comerciales): identidad (razón social para empresa; nombre +
        apellido para persona), un documento (DNI o CUIT) y celular. El resto es
        opcional. Devuelve la lista de faltantes — vacía si está completo. NO se
        aplica al sentinela "Local" ni a import/reclasificación (caminos internos
        que construyen el registro sin pasar por este guard).
        """
        missing: list[str] = []
        if not self.name or not self.name.strip():
            missing.append("name")
        # Empresa: la razón social va en ``name`` (ya exigido). Persona / sin tipo:
        # se exige apellido.
        if self.customer_type != "company" and not (self.last_name or "").strip():
            missing.append("last_name")
        if not (self.dni or "").strip() and not (self.cuit or "").strip():
            missing.append("dni_or_cuit")
        if not (self.phone or "").strip():
            missing.append("phone")
        return missing


# ── Carga por archivo (Fase B): extracción de ficha individual + import masivo ──


class CustomerExtractionResponse(BaseModel):
    """Datos SUGERIDOS al leer la ficha de un cliente (foto/PDF/planilla de 1 fila).

    NO persiste nada: prellena el formulario (human-in-the-loop). Los campos van sin
    validar formato (la validación dura corre al confirmar el alta). ``confidence`` /
    ``warnings`` guían la revisión. ``source_upload_id`` se setea si el archivo se guardó.
    """

    customer_type: str | None = None
    name: str | None = None
    last_name: str | None = None
    doc_type: str | None = None
    dni: str | None = None
    cuit: str | None = None
    iva_condition: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    locality: str | None = None
    province: str | None = None
    postal_code: str | None = None
    birthday: date | None = None
    confidence: str
    warnings: list[str] = Field(default_factory=list)
    source_upload_id: UUID | None = None


class CustomerImportRow(BaseModel):
    """Una fila parseada del import masivo (payload del cliente, todo opcional).

    Se usa tanto en el preview (lo detectado por fila) como en el confirm (lo que el
    usuario aplica). Sin validación de formato acá: el servicio re-valida y clasifica.
    """

    customer_type: str | None = None
    name: str | None = None
    last_name: str | None = None
    doc_type: str | None = None
    dni: str | None = None
    cuit: str | None = None
    iva_condition: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    locality: str | None = None
    province: str | None = None
    postal_code: str | None = None
    birthday: date | None = None


class CustomerImportPreviewItem(BaseModel):
    row_index: int
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review"
    customer: CustomerImportRow
    existing_id: UUID | None = None
    existing_name: str | None = None
    issues: list[str] = Field(default_factory=list)


class CustomerImportPreviewResponse(BaseModel):
    items: list[CustomerImportPreviewItem]
    to_create: int
    to_update: int
    invalid: int
    duplicates: int
    # F7b: filas con nombre pero sin ninguna clave fuerte (documento/email/teléfono) —
    # no matchean ni crean, requieren revisión manual. Default 0 = backward-compat.
    needs_review: int = 0
    warnings: list[str] = Field(default_factory=list)
    source_upload_id: UUID | None = None


class CustomerImportConfirmRequest(BaseModel):
    rows: list[CustomerImportRow] = Field(default_factory=list)


class CustomerImportConfirmResponse(BaseModel):
    created: int
    updated: int
    skipped: int
    created_ids: list[UUID] = Field(default_factory=list)
    updated_ids: list[UUID] = Field(default_factory=list)


class UpdateCustomerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    customer_type: str | None = Field(default=None, max_length=10)
    last_name: str | None = Field(default=None, max_length=200)
    doc_type: str | None = Field(default=None, max_length=10)
    dni: str | None = Field(default=None, max_length=15)
    cuit: str | None = Field(default=None, max_length=13)
    iva_condition: str | None = Field(default=None, max_length=25)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    telegram_username: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=2000)
    locality: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=12)
    birthday: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] | None = None
    credit_limit: Decimal | None = None

    @field_validator("customer_type")
    @classmethod
    def _v_customer_type(cls, v: str | None) -> str | None:
        return _check_customer_type(v)

    @field_validator("doc_type")
    @classmethod
    def _v_doc_type(cls, v: str | None) -> str | None:
        return _check_doc_type(v)

    @field_validator("iva_condition")
    @classmethod
    def _v_iva(cls, v: str | None) -> str | None:
        return _check_iva(v)

    @field_validator("cuit")
    @classmethod
    def _v_cuit(cls, v: str | None) -> str | None:
        return validate_cuit(v)

    @field_validator("dni")
    @classmethod
    def _v_dni(cls, v: str | None) -> str | None:
        return validate_dni(v)


# ── Saldo neto por cliente (Fase 2 — cobro→cliente) ──────────────────────────


class CustomerBalanceResponse(BaseModel):
    """Saldo neto del cliente: fiado acumulado menos cobros registrados.

    ``over_limit``: True si hay ``credit_limit`` configurado y ``balance``
    lo supera. False en todos los demás casos (sin límite, o balance <= límite).
    """

    customer_id: UUID
    total_account: float
    total_paid: float
    balance: float
    credit_limit: Decimal | None = None
    over_limit: bool
