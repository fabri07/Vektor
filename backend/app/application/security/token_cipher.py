"""token_cipher — cifrado Fernet (AES-128-CBC) para tokens OAuth de Google."""

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("GOOGLE_TOKEN_CIPHER_KEY")
    if not key:
        raise EnvironmentError(
            "GOOGLE_TOKEN_CIPHER_KEY no está definida. "
            "Generá una con: "
            "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_token(token: str) -> str:
    """Cifra un token OAuth antes de persistirlo en la BD."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Descifra un token OAuth leído de la BD."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
