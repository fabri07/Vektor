"""Schemas del formulario público de solicitud de acceso y de su revisión.

Véktor cerró el registro abierto: el visitante ya no crea una cuenta, manda una
**solicitud** que el dueño revisa a mano. Estos schemas son el contrato HTTP de
ese trámite.

Tres reglas que este archivo sostiene y que un cambio no puede aflojar:

1. **`extra="forbid"` en el payload de creación.** Sin eso, un bundle viejo del
   frontend que siga mandando `password` lo vería ignorado en silencio y creería
   que dio de alta una cuenta. El punto entero de la feature es que este endpoint
   NO crea cuentas: mandar `password` tiene que ser un 422 ruidoso.
2. **`vertical_other_text` se valida NO-VACÍO, no solo no-nulo.** El CHECK de la
   base (`requested_vertical <> 'otros' OR vertical_other_text IS NOT NULL`) deja
   pasar el string vacío: rubro "Otros" con descripción `""` satisface el CHECK y
   deja la solicitud sin la única información que justifica esa opción. Se strippea
   y se exige largo mínimo ACÁ; el CHECK queda de backstop.
3. **`assigned_vertical` al aprobar es `Vertical`, no `RequestedVertical`.** Así
   `"otros"` es un 422 antes de tocar el servicio: `'otros'` nunca es un vertical
   operativo. El CHECK `ck_access_requests_assigned_vertical_code` es el backstop.

Nada de acá calcula nada: `review_priority` es un campo DERIVADO de
`requested_plan` (no hay columna `is_priority`, a propósito — ver el docstring del
modelo).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.domain.access_request import (
    AccessRequestStatus,
    CanShareFiles,
    HistoryDepth,
    RecordsFormat,
    RequestedPlan,
    RevenueBand,
    StaffSize,
    YearsOperating,
)
from app.domain.contact_lead import normalize_email
from app.domain.verticals import RequestedVertical, Vertical
from app.schemas.onboarding import MAIN_CONCERN_PATTERN

#: Largo mínimo (ya strippeado) del "contanos de qué es tu negocio" que acompaña
#: al rubro "Otros". Es la ÚNICA información que justifica esa opción: un texto
#: vacío o de una letra la vuelve inútil para revisar.
MIN_VERTICAL_OTHER_TEXT = 3

#: Cuántos dígitos mínimos tiene que traer un teléfono, si se informa. Mismo
#: criterio que el formulario de contacto (`CreateLeadRequest.celular`).
_MIN_PHONE_DIGITS = 6

_MainConcern = Annotated[str, Field(pattern=MAIN_CONCERN_PATTERN)]


def _strip_to_none(value: str | None) -> str | None:
    """Normaliza un texto libre opcional: recorta y colapsa el vacío a ``None``.

    Guardar `""` o `"   "` en una columna nullable es peor que guardar `NULL`:
    parece un dato contestado y no lo es.
    """
    if value is None:
        return None
    limpio = value.strip()
    return limpio or None


# ── Formulario público ────────────────────────────────────────────────────────


class CreateAccessRequestRequest(BaseModel):
    """Payload del formulario público 'Pedir acceso'.

    **No tiene campo `password`, ni puede tenerlo**: este endpoint no crea
    usuarios. Con `extra="forbid"`, mandarlo es 422.
    """

    # `extra="forbid"` NO es una preferencia de estilo: es lo que convierte un
    # bundle viejo del frontend (que manda `password`, `vertical_code`, etc.) en
    # un error visible en vez de un alta silenciosamente incompleta.
    model_config = ConfigDict(extra="forbid")

    # ── Contacto ──────────────────────────────────────────────────────────────
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    business_name: str = Field(min_length=2, max_length=200)

    # ── Rubro declarado ───────────────────────────────────────────────────────
    requested_vertical: RequestedVertical
    vertical_other_text: str | None = Field(default=None, max_length=2000)

    # ── Intención de plan ─────────────────────────────────────────────────────
    # REQUERIDO y sin default: elegir con qué cuenta arrancar es una decisión del
    # solicitante. Un default silencioso (`free`) inventaría una respuesta que
    # nadie dio y ensuciaría la señal comercial que este campo existe para medir.
    requested_plan: RequestedPlan

    # ── Screening del negocio ─────────────────────────────────────────────────
    years_operating: YearsOperating
    staff_size: StaffSize
    monthly_revenue_band: RevenueBand
    main_concern: _MainConcern
    records_format: RecordsFormat
    history_depth: HistoryDepth
    can_share_files: CanShareFiles
    records_notes: str | None = Field(default=None, max_length=2000)
    applicant_notes: str | None = Field(default=None, max_length=2000)

    # ── Consentimiento (Ley 25.326) ───────────────────────────────────────────
    # `Literal[True]` ⇒ mandar False u omitirlo produce un error de validación
    # built-in y serializable, sin ValueError custom (mismo patrón que
    # `CreateLeadRequest.consent`). El modelo persiste `consent_accepted_at` NOT
    # NULL: sin esta casilla estaríamos sellando un consentimiento que nadie dio.
    consent: Literal[True]
    # Versión que el front declara haber mostrado. Solo para auditar el contrato:
    # el backend persiste SIEMPRE la suya (`domain.CONSENT_VERSION`).
    consent_version: str | None = Field(default=None, max_length=20)

    # ── Trazabilidad ──────────────────────────────────────────────────────────
    cta_source: str | None = Field(default=None, max_length=60)
    #: Token opaco del prefill de "Continuar con Google". El formulario lo
    #: devuelve tal cual lo recibió y el router lo canjea contra Redis
    #: (`oauth:prefill:{id}`) para poblar `google_subject`: **este POST es la
    #: única toma del token**, el `GET /access-requests/prefill/{token}` que lo
    #: precede solo lee. Un token vencido no invalida la solicitud (se manda sin
    #: ligar); uno de otro email es 403.
    google_prefill_token: str | None = Field(default=None, max_length=100)

    # ── Anti-bot ──────────────────────────────────────────────────────────────
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

    @field_validator("full_name", "business_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("phone", "records_notes", "applicant_notes", "vertical_other_text")
    @classmethod
    def _strip_optional(cls, v: str | None) -> str | None:
        return _strip_to_none(v)

    @field_validator("phone")
    @classmethod
    def _phone_has_digits(cls, v: str | None) -> str | None:
        """Un teléfono informado tiene que ser un teléfono; omitirlo es válido."""
        if v is not None and sum(c.isdigit() for c in v) < _MIN_PHONE_DIGITS:
            raise ValueError(
                f"El teléfono tiene que tener al menos {_MIN_PHONE_DIGITS} dígitos "
                "(o podés dejarlo vacío)."
            )
        return v

    @model_validator(mode="after")
    def _validate_vertical_other_text(self) -> Self:
        """Requerido si y solo si el rubro es 'otros'.

        El `_strip_optional` de arriba ya colapsó `""`/`"   "` a `None`, así que
        acá el chequeo de no-nulo alcanza para cubrir también el string vacío —
        que es justo lo que el CHECK de la base deja pasar.
        """
        es_otros = self.requested_vertical is RequestedVertical.OTROS
        if es_otros:
            if self.vertical_other_text is None:
                raise ValueError(
                    "Elegiste 'Otro': contanos de qué es tu negocio."
                )
            if len(self.vertical_other_text) < MIN_VERTICAL_OTHER_TEXT:
                raise ValueError(
                    "Contanos de qué es tu negocio con un poco más de detalle "
                    f"(mínimo {MIN_VERTICAL_OTHER_TEXT} caracteres)."
                )
        elif self.vertical_other_text is not None:
            raise ValueError(
                "vertical_other_text solo corresponde cuando el rubro es 'otros'."
            )
        return self


class AccessRequestAcceptedResponse(BaseModel):
    """Respuesta ÚNICA del alta pública.

    Idéntica en los cinco desenlaces posibles (creada, token reemitido, trámite
    ya abierto, el email ya tiene cuenta, bot descartado). Distinguirlos sería
    exactamente el oráculo de enumeración de cuentas que este flujo elimina — el
    `POST /auth/register` viejo respondía 409 "An account with this email already
    exists". No agregar campos que dependan del desenlace.
    """

    status: str = "ok"
    message: str = (
        "¡Gracias! Te mandamos un correo para que confirmes tu email. "
        "Después revisamos tu solicitud y te escribimos."
    )


class VerifyAccessRequestRequest(BaseModel):
    """Token del mail de doble opt-in.

    Va por POST y no por GET a propósito: los escáneres de mail corporativos y
    los prefetchers de links hacen GET, y consumirían el token antes que el
    usuario.
    """

    token: str = Field(min_length=1, max_length=64)


class VerifiedAccessRequestResponse(BaseModel):
    """Confirmación del doble opt-in.

    Expone `requested_plan` porque quien llega acá tiene el token del mail: es su
    propia solicitud, no hay filtración. La página de estado lo usa para el copy
    de Premium. NO expone la posición en la cola ni nada del criterio de revisión.
    """

    status: str = "ok"
    message: str = "¡Listo! Confirmamos tu email. Ahora revisamos tu solicitud."
    requested_plan: RequestedPlan


class ResendAccessRequestRequest(BaseModel):
    """Reenvío del mail de verificación. Responde 200 genérico siempre."""

    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(str(v))


# ── Revisión (SUPERADMIN) ─────────────────────────────────────────────────────


class AccessRequestAdminItem(BaseModel):
    """Ficha completa de una solicitud, para el dueño que la revisa.

    No expone `ip_hash`: se guarda solo para detección de abuso y no aporta nada
    a la decisión.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime

    full_name: str
    email: str
    phone: str | None
    business_name: str

    requested_vertical: RequestedVertical
    vertical_other_text: str | None
    requested_plan: RequestedPlan

    years_operating: str
    staff_size: str
    monthly_revenue_band: str
    main_concern: str
    records_format: str
    history_depth: str
    can_share_files: str
    records_notes: str | None
    applicant_notes: str | None

    status: AccessRequestStatus
    email_verified_at: datetime | None

    reviewed_at: datetime | None
    reviewed_via: str | None
    review_notes: str | None
    rejection_reason: str | None

    assigned_vertical_code: Vertical | None
    approved_tenant_id: uuid.UUID | None
    approved_user_id: uuid.UUID | None

    cta_source: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def review_priority(self) -> Literal["high", "normal"]:
        """Prioridad DERIVADA de `requested_plan`, no una columna.

        No existe `is_priority` en la tabla a propósito: un booleano redundante
        solo habilitaría estados incoherentes (`requested_plan='free'` con
        `is_priority=true`). Se calcula acá y en el `ORDER BY` del servicio,
        desde el mismo dato.
        """
        return "high" if self.requested_plan is RequestedPlan.PREMIUM else "normal"


class ApproveAccessRequest(BaseModel):
    """Decisión de aprobar: el dueño ASIGNA el vertical operativo.

    `assigned_vertical` es `Vertical` (los 3 reales) y no `RequestedVertical`:
    `"otros"` tiene que morir en un 422 acá, antes de llegar al servicio. Es el
    rubro que el DUEÑO decide, no el que declaró el solicitante — esa corrección
    es todo el punto de la revisión manual.
    """

    assigned_vertical: Vertical
    notes: str | None = Field(default=None, max_length=2000)


class RejectAccessRequest(BaseModel):
    """Decisión de rechazar. El motivo es interno y NUNCA se le transcribe al solicitante."""

    reason: str = Field(min_length=3, max_length=2000)
    #: `False` para descartar spam sin escribirle de vuelta.
    notify: bool = True


class WaitlistAccessRequest(BaseModel):
    """Decisión de postergar. No es terminal: después se puede aprobar o rechazar."""

    notes: str | None = Field(default=None, max_length=2000)


class ApproveAccessRequestResponse(BaseModel):
    """Resultado de aprobar.

    `tenant_id`/`user_id` son opcionales porque las FK de la solicitud son
    `ON DELETE SET NULL`: una solicitud aprobada cuyo tenant se borró después
    sigue existiendo, sin puntero. Devolverlos opcionales es honesto.

    **No incluye el token de invitación**: es la credencial con la que el usuario
    define su contraseña y no tiene por qué viajar —ni quedar logueada— en el
    cuerpo de una respuesta HTTP. El link se lo manda el email de decisión.
    """

    request: AccessRequestAdminItem
    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None
    #: `True` si la solicitud YA estaba aprobada: la re-aprobación es idempotente
    #: y no acuñó un segundo tenant.
    already_approved: bool
