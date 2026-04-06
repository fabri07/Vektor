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


# ── POST /auth/oauth/google/link-pending ──────────────────────────────────────

class OAuthLinkPendingRequest(BaseModel):
    """Complete a link_required flow after the user authenticates with password."""
    pending_oauth_session_id: str
    email: EmailStr
    password: str = Field(min_length=1)
