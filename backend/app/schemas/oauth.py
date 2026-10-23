"""Pydantic schemas for OAuth endpoints (Sprint 2: Google Login)."""

from pydantic import BaseModel, EmailStr, Field

# ── POST /auth/oauth/google/start ─────────────────────────────────────────────


class OAuthStartResponse(BaseModel):
    """Authorization URL to redirect the user to Google."""

    authorization_url: str


# ── GET /auth/oauth/google/callback ───────────────────────────────────────────
# (browser-facing redirect — returns 302 to frontend, no JSON schema needed)


# ── POST /auth/oauth/google/exchange ─────────────────────────────────────────


class OAuthExchangeRequest(BaseModel):
    """Short-lived session ID from the callback redirect."""

    session_id: str


class OAuthLinkRequiredResponse(BaseModel):
    """Returned when the Google email matches an existing local account.

    The frontend must:
    1. Show the login form with email pre-filled.
    2. POST /auth/oauth/google/link-pending with pending_oauth_session_id + credentials.
    """

    status: str = "link_required"
    pending_oauth_session_id: str
    email: str
    provider: str = "google"


class OAuthAccessRequestRequiredResponse(BaseModel):
    """El email de Google no tiene cuenta: hay que pedir acceso, no se crea nada.

    Véktor cerró el registro abierto, así que entrar con Google con un email
    desconocido NO acuña un tenant. El frontend lleva al visitante a
    `/solicitar-acceso?prefill=<prefill_token>`; ese token se canjea en
    `GET /access-requests/prefill/{token}` para prellenar el formulario y se
    devuelve en el POST de la solicitud, que es donde se resuelve el
    `google_subject`.

    `full_name` es opcional: Google no siempre manda el claim `name`, y
    derivarlo del email sería inventar el nombre del solicitante.
    """

    status: str = "access_request_required"
    prefill_token: str
    email: str
    full_name: str | None = None
    provider: str = "google"


# ── GET /access-requests/prefill/{token} ──────────────────────────────────────


class GooglePrefillResponse(BaseModel):
    """Lo que el formulario público muestra de una identidad de Google.

    **No expone `provider_subject`**: el identificador de la identidad no tiene
    por qué viajar al browser. El token opaco es la única referencia que el
    frontend maneja, y el subject se resuelve del lado del servidor al canjearlo.
    """

    email: str
    full_name: str | None = None
    provider: str = "google"


# ── POST /auth/oauth/google/link-pending ──────────────────────────────────────


class OAuthLinkPendingRequest(BaseModel):
    """Complete a link_required flow after the user authenticates with password."""

    pending_oauth_session_id: str
    email: EmailStr
    password: str = Field(min_length=1)
