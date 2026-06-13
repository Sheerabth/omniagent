"""Secrets: AES-256-GCM for LLM API keys, argon2 for client/service keys."""
import os
import secrets as _secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ph = PasswordHasher()


def _aes_key() -> bytes:
    raw = os.environ["OMNIAGENT_SECRET_KEY"]
    key = bytes.fromhex(raw)
    if len(key) != 32:
        raise ValueError("OMNIAGENT_SECRET_KEY must be 32 bytes (64 hex chars)")
    return key


def encrypt_llm_key(plaintext: str) -> bytes:
    aes = AESGCM(_aes_key())
    nonce = _secrets.token_bytes(12)
    ct = aes.encrypt(nonce, plaintext.encode(), None)
    return nonce + ct


def decrypt_llm_key(blob: bytes) -> str:
    aes = AESGCM(_aes_key())
    nonce, ct = blob[:12], blob[12:]
    return aes.decrypt(nonce, ct, None).decode()


def hash_key(key: str) -> str:
    return _ph.hash(key)


def verify_key(key: str, stored_hash: str) -> bool:
    try:
        return _ph.verify(stored_hash, key)
    except VerifyMismatchError:
        return False


def generate_key() -> str:
    return _secrets.token_hex(32)
