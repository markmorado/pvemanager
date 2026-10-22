"""
Field-level encryption for sensitive database columns.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
Configure via FERNET_KEY environment variable.

Key generation:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
from functools import lru_cache
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import TypeDecorator, String

logger = logging.getLogger(__name__)

# Warn once if FERNET_KEY is not configured
_warned_no_key = False


def _get_fernet():
    global _warned_no_key
    from .config import settings
    key = settings.FERNET_KEY
    if not key:
        if not _warned_no_key:
            logger.warning(
                "FERNET_KEY is not set — sensitive fields (passwords) are stored "
                "as plaintext. Set FERNET_KEY in your environment for production."
            )
            _warned_no_key = True
        return None
    return _fernet_for(key.encode() if isinstance(key, str) else key)


@lru_cache(maxsize=4)
def _fernet_for(key: bytes) -> Fernet:
    # Building a Fernet decodes/validates the key; every encrypted column read
    # went through this, so cache the instance per key.
    return Fernet(key)


def encrypt_value(value: str) -> str:
    """Encrypt a string value. Returns plaintext if FERNET_KEY is not configured."""
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    """Decrypt a Fernet-encrypted string.

    Falls back to returning the value as-is if decryption fails
    (handles legacy plaintext rows during migration).
    """
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        # Value is likely plaintext (pre-encryption legacy row) or was
        # encrypted with a different FERNET_KEY. Return as-is for backward
        # compatibility, but warn so key-rotation mistakes are visible.
        logger.warning(
            "Fernet decryption failed (InvalidToken). Value returned as-is — "
            "this is expected for legacy plaintext rows, but check FERNET_KEY "
            "if previously-encrypted data is now unreadable."
        )
        return value


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts/decrypts values via Fernet.

    Usage::

        from .crypto import EncryptedString
        password = Column(EncryptedString(255), nullable=True)

    The underlying storage type is String, so no schema change is required.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Called when writing to the database."""
        return encrypt_value(value) if value else value

    def process_result_value(self, value, dialect):
        """Called when reading from the database."""
        return decrypt_value(value) if value else value
