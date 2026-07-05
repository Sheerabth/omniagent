"""Secrets: argon2 hashing for client/service API keys."""

import secrets as _secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from omniagent.config import settings

_ph = PasswordHasher()

# ponytail: dict cache, per-process. TTL and max entries in config.py.
# Key revocation takes effect within cache TTL. Hard cap prevents DoS via
# key spam (attacker with known prefix floods cache with random keys).
_verify_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def hash_key(key: str) -> str:
    return _ph.hash(key)


def verify_key(key: str, stored_hash: str) -> bool:
    cache_key = (key, stored_hash)
    now = time.monotonic()
    cached = _verify_cache.get(cache_key)
    if cached is not None and now - cached[0] < settings.verify_cache_ttl_seconds:
        return cached[1]
    try:
        result = _ph.verify(stored_hash, key)
    except Argon2Error:
        result = False
    _verify_cache[cache_key] = (now, result)
    if len(_verify_cache) > settings.verify_cache_max_entries:
        _verify_cache.clear()
    return result


def generate_key() -> str:
    return _secrets.token_hex(32)
