"""token_cipher — cifrado Fernet (AES-128-CBC + HMAC-SHA256) para tokens OAuth.

Uso:
    from app.application.security.token_cipher import encrypt_token, decrypt_token

    encrypted = encrypt_token(access_token)
    original  = decrypt_token(encrypted)

La clave se lee de la variable de entorno GOOGLE_TOKEN_CIPHER_KEY en cada llamada.
No se cachea: permite monkeypatch en tests y evita acoplarse al cache de get_settings().

Generación de clave nueva:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class TokenCipherError(Exception):
    """Error de cifrado/descifrado de tokens (clave inválida o datos corruptos)."""


def _get_fernet() -> Fernet:
    """Lee GOOGLE_TOKEN_CIPHER_KEY del entorno y construye un Fernet.

    No se cachea intencionalmente: garantiza que monkeypatch funcione en tests
    y que cualquier rotación de clave se refleje sin reiniciar el proceso.

    Raises EnvironmentError si la clave no está definida.
    Raises TokenCipherError si la clave tiene formato inválido.
    """
    key = os.environ.get("GOOGLE_TOKEN_CIPHER_KEY", "")
    if not key:
        raise EnvironmentError(
            "GOOGLE_TOKEN_CIPHER_KEY no está definida. "
            "Generá una clave con: "
            "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise TokenCipherError(
            f"GOOGLE_TOKEN_CIPHER_KEY es inválida (debe ser URL-safe base64 de 32 bytes): {exc}"
        ) from exc


def encrypt_token(token: str) -> str:
    """Cifra un token OAuth antes de persistirlo en la BD.

    Returns el ciphertext como string URL-safe (puede almacenarse en TEXT).
    Raises EnvironmentError si GOOGLE_TOKEN_CIPHER_KEY no está definida.
    """
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Descifra un token OAuth leído de la BD.

    Raises EnvironmentError si GOOGLE_TOKEN_CIPHER_KEY no está definida.
    Raises TokenCipherError si el token está corrupto o la clave no coincide.
    """
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise TokenCipherError(
            "No se pudo descifrar el token: clave incorrecta o datos corruptos."
        ) from exc
