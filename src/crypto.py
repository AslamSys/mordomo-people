"""
AES-256-GCM encryption for sensitive fields stored in PostgreSQL.

Each encrypted value is stored as: nonce (12 bytes) + ciphertext + tag (16 bytes),
encoded as hex string. The master key never touches the database.
"""
import os
import binascii
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.config import PEOPLE_MASTER_KEY_HEX


def _get_key() -> bytes:
    key = binascii.unhexlify(PEOPLE_MASTER_KEY_HEX)
    if len(key) != 32:
        raise ValueError("PEOPLE_MASTER_KEY must be 32 bytes (64 hex chars)")
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return hex-encoded nonce+ciphertext."""
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return binascii.hexlify(nonce + ciphertext).decode()


def decrypt(encoded: str) -> str:
    """Decrypt a hex-encoded nonce+ciphertext string."""
    key = _get_key()
    raw = binascii.unhexlify(encoded)
    nonce, ciphertext = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()
