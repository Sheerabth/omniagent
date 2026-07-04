"""Encryption for auth_context stored in DB.

Requires OMNIAGENT_ENCRYPTION_KEY env var (Fernet key, generate with:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If key is unset: encrypt() returns JSON string (plaintext), decrypt() parses JSON (backward compat).
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _fernet():
    from omniagent.config import settings

    key = settings.omniagent_encryption_key.strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode())


def encrypt_auth_context(data: Any) -> str | None:
    """Serialize and encrypt auth_context to a TEXT string for DB storage."""
    if data is None:
        return None
    raw = json.dumps(data).encode()
    f = _fernet()
    if f is None:
        logger.warning("OMNIAGENT_ENCRYPTION_KEY not set — auth_context stored in plaintext")
        return raw.decode()
    return f.encrypt(raw).decode()


def decrypt_auth_context(value: str | None) -> Any:
    """Decrypt a value from DB back to a Python object. Handles plaintext for backward compat."""
    if value is None:
        return None
    f = _fernet()
    if f is not None:
        try:
            return json.loads(f.decrypt(value.encode()))
        except Exception:
            pass  # not encrypted — fall through to plaintext parse
    try:
        return json.loads(value)
    except Exception:
        logger.error("auth_context could not be decrypted or parsed")
        return None
