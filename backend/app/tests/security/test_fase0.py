"""
Tests de seguridad — Fase 0.

Los tests de RLS y esquema de BD conectan a PostgreSQL real vía DATABASE_URL.
Si DATABASE_URL no está disponible, se saltan con un mensaje claro.
"""

import os
from types import SimpleNamespace

import psycopg2
import pytest

from app.application.security.prompt_defense import is_valid_action_type, wrap_user_input
from app.application.security.token_cipher import decrypt_token, encrypt_token

# ── Helpers de conexión a Neon ────────────────────────────────────────────────

def _get_db_conn():
    """Devuelve una conexión psycopg2 a la BD real. Skipea si no hay URL."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL no definida — test requiere PostgreSQL real")
    # Convertir postgresql+asyncpg:// o postgresql+psycopg2:// → postgresql://
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    return psycopg2.connect(url)


TENANT_TABLES = [
    "sales_entries",
    "expense_entries",
    "products",
    "uploaded_files",
    "health_score_snapshots",
    "decision_audit_log",
    "notifications",
    "user_activity_events",
    "pending_actions",
    # Sprint 1: identidades sociales y tokens Workspace tienen tenant_id + RLS
    "user_auth_identities",
    "user_google_workspace_connections",
]


# ── Tests de RLS ──────────────────────────────────────────────────────────────

def test_rls_enabled_on_sales():
    """La tabla sales_entries debe tener Row Level Security activo."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = %s",
                ("sales_entries",),
            )
            row = cur.fetchone()
        assert row is not None, "Tabla sales_entries no encontrada en pg_class"
        assert row[0] is True, "RLS no está habilitado en sales_entries"
    finally:
        conn.close()


def test_rls_enabled_on_all_tenant_tables():
    """Todas las tablas tenant deben tener RLS habilitado."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname = ANY(%s)",
                (TENANT_TABLES,),
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}

        missing = [t for t in TENANT_TABLES if t not in rows]
        assert not missing, f"Tablas no encontradas en pg_class: {missing}"

        disabled = [t for t, rls in rows.items() if not rls]
        assert not disabled, f"RLS no habilitado en: {disabled}"
    finally:
        conn.close()


# ── Tests de Token Cipher ─────────────────────────────────────────────────────

def test_token_cipher_encrypt_decrypt(monkeypatch):
    """encrypt → decrypt debe devolver el string original."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("GOOGLE_TOKEN_CIPHER_KEY", Fernet.generate_key().decode())

    original = "ya29.A0_google_access_token_example"
    encrypted = encrypt_token(original)

    assert encrypted != original
    assert decrypt_token(encrypted) == original


def test_token_cipher_fails_without_key(monkeypatch):
    """Sin GOOGLE_TOKEN_CIPHER_KEY debe lanzar EnvironmentError."""
    monkeypatch.delenv("GOOGLE_TOKEN_CIPHER_KEY", raising=False)

    with pytest.raises(EnvironmentError, match="GOOGLE_TOKEN_CIPHER_KEY"):
        encrypt_token("cualquier_token")


def test_token_cipher_falls_back_to_settings(monkeypatch):
    """Si la env var no está exportada, debe usar Settings como fallback."""
    from cryptography.fernet import Fernet

    monkeypatch.delenv("GOOGLE_TOKEN_CIPHER_KEY", raising=False)
    key = Fernet.generate_key().decode()

    def _fake_settings():
        return SimpleNamespace(GOOGLE_TOKEN_CIPHER_KEY=key)

    monkeypatch.setattr("app.config.settings.get_settings", _fake_settings)

    original = "ya29.A0_google_access_token_example"
    encrypted = encrypt_token(original)

    assert encrypted != original
    assert decrypt_token(encrypted) == original


# ── Tests de Prompt Defense ───────────────────────────────────────────────────

def test_prompt_defense_wraps_input():
    """wrap_user_input debe envolver el mensaje en <user_message>."""
    result = wrap_user_input("hola")
    assert "<user_message>" in result
    assert "hola" in result
    assert "</user_message>" in result
    assert result == "<user_message>hola</user_message>"


def test_invalid_action_type_rejected():
    """Action types fuera del catálogo deben ser rechazados."""
    assert is_valid_action_type("HACK_DB") is False
    assert is_valid_action_type("DROP_TABLE") is False
    assert is_valid_action_type("") is False
    assert is_valid_action_type("REGISTER_SALE") is True


# ── Tests de esquema BD ───────────────────────────────────────────────────────

def test_pending_actions_table_exists():
    """La tabla pending_actions debe existir con las columnas correctas."""
    required_columns = {
        "id", "tenant_id", "user_id", "action_type",
        "payload", "risk_level", "status",
        "expires_at", "executed_at", "created_at",
    }
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'pending_actions'
                """,
            )
            cols = {r[0] for r in cur.fetchall()}

        assert cols, "Tabla pending_actions no existe o no tiene columnas"
        missing = required_columns - cols
        assert not missing, f"Columnas faltantes en pending_actions: {missing}"
    finally:
        conn.close()


def test_pending_actions_has_expires_at():
    """pending_actions debe tener expires_at para prevenir replay attacks."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'pending_actions'
                  AND column_name = 'expires_at'
                """,
            )
            row = cur.fetchone()
        assert row is not None, "Columna expires_at no encontrada en pending_actions"
        assert "timestamp" in row[1].lower(), (
            f"expires_at debe ser TIMESTAMPTZ, es: {row[1]}"
        )
    finally:
        conn.close()
