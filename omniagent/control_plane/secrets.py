"""Secrets: argon2 hashing for client/service API keys."""

import secrets as _secrets

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

_ph = PasswordHasher()


def hash_key(key: str) -> str:
    return _ph.hash(key)


def verify_key(key: str, stored_hash: str) -> bool:
    try:
        return _ph.verify(stored_hash, key)
    except Argon2Error:
        return False


def generate_key() -> str:
    return _secrets.token_hex(32)
