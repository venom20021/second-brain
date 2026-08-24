"""
🔐 Encryption — Secure backup file encryption using Fernet (AES-128-CBC + HMAC).

Uses PBKDF2-HMAC-SHA256 for key derivation (100,000 iterations).
Each file gets a unique 16-byte salt stored in the file header.

Format of encrypted file:
  [16 bytes salt][Fernet token (32+16+16+32+padding bytes)]

The password is never stored — only a PBKDF2 hash for verification.
"""

import os
import hashlib
import hmac
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# ─── Key Derivation ─────────────────────────────────────────────────────────

SALT_SIZE = 16
PBKDF2_ITERATIONS = 480_000  # OWASP 2023 recommendation


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet key from a password and salt using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    key = kdf.derive(password.encode("utf-8"))
    # Fernet requires a URL-safe base64-encoded 32-byte key
    import base64
    return base64.urlsafe_b64encode(key)


def hash_password(password: str, salt: bytes = None) -> tuple:
    """
    Hash a password for verification (NOT for encryption).
    Returns (hash_hex, salt_hex) — store these to verify the password later.
    """
    if salt is None:
        salt = os.urandom(SALT_SIZE)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    key = kdf.derive(password.encode("utf-8"))
    return key.hex(), salt.hex()


def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    """Verify a password against a stored hash and salt."""
    salt = bytes.fromhex(stored_salt)
    computed_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(computed_hash, stored_hash)


# ─── Encrypt / Decrypt ─────────────────────────────────────────────────────

def encrypt_data(plaintext: str, password: str) -> bytes:
    """
    Encrypt plaintext with a password.
    Returns: [16-byte salt][Fernet encrypted token]
    """
    salt = os.urandom(SALT_SIZE)
    key = derive_key(password, salt)
    f = Fernet(key)
    encrypted = f.encrypt(plaintext.encode("utf-8"))
    return salt + encrypted


def decrypt_data(data: bytes, password: str) -> str:
    """
    Decrypt data that was encrypted with encrypt_data().
    Raises ValueError if password is wrong or data is corrupted.
    """
    if len(data) < SALT_SIZE + 65:  # Fernet token is at least 65 bytes
        raise ValueError("Invalid encrypted data: too short")

    salt = data[:SALT_SIZE]
    token = data[SALT_SIZE:]
    key = derive_key(password, salt)
    f = Fernet(key)
    decrypted = f.decrypt(token)
    return decrypted.decode("utf-8")


def is_encrypted(data: bytes) -> bool:
    """Check if data looks like an encrypted file (has salt prefix)."""
    # Fernet tokens start with 'gAAAAA' when base64-decoded
    # Our format: 16-byte salt + Fernet token
    if len(data) < SALT_SIZE + 65:
        return False
    # Check if the Fernet token portion starts with the expected base64 prefix
    token_part = data[SALT_SIZE:]
    return token_part[:6] == b'gAAAAA'
