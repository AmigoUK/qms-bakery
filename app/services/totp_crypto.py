"""Encryption-at-rest for TOTP secrets.

Threat model: a database leak (stolen backup, SQL injection, malicious
DBA) gives an attacker every user's TOTP seed and therefore the ability
to derive valid codes forever — defeating 2FA until each user re-enrols.
With column-level encryption keyed by an env var (`TOTP_ENC_KEY`) held
outside the database, a DB-only compromise no longer yields the seeds.

Format: stored values are `v1:<urlsafe_b64_fernet_token>`. The version
prefix lets us rotate to a new scheme without a one-shot migration —
write `v2:` going forward, accept both during transition, drop `v1:`
once every row has rolled over.

Key generation:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Key rotation: bump the env to a new key, prepend the old key to
`TOTP_ENC_KEYS_OLD` (comma-separated), and the next read of an old-key
row will decrypt under the legacy key while writes use the new one.
The MVP only implements single-key mode — multi-key rotation can land
when there's a second key to rotate to.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class TOTPCryptoError(Exception):
    """Encryption helper refuses to operate — missing key or bad payload."""


_ENC_PREFIX = "v1:"


def _fernet() -> Fernet:
    key = current_app.config.get("TOTP_ENC_KEY")
    if not key:
        raise TOTPCryptoError(
            "TOTP_ENC_KEY is required to read or write TOTP secrets. "
            "Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode("ascii") if isinstance(key, str) else key)


def encrypt(plaintext_secret: str) -> str:
    if not plaintext_secret:
        raise TOTPCryptoError("plaintext is empty")
    token = _fernet().encrypt(plaintext_secret.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt(stored: str) -> str:
    if not stored:
        raise TOTPCryptoError("stored value is empty")
    if not stored.startswith(_ENC_PREFIX):
        # Legacy plaintext from before this feature landed, or an unknown
        # future prefix. Refuse rather than silently fall back — every
        # legitimate TOTP secret must have been written through encrypt().
        raise TOTPCryptoError(
            "stored TOTP secret has no recognised version prefix; "
            "the user must re-enrol"
        )
    payload = stored[len(_ENC_PREFIX):].encode("ascii")
    try:
        return _fernet().decrypt(payload).decode("utf-8")
    except InvalidToken as exc:
        raise TOTPCryptoError(
            "TOTP secret decryption failed (wrong key or tampered ciphertext)"
        ) from exc


def is_encrypted(stored: str | None) -> bool:
    """True if the value has been written through `encrypt()`."""
    return bool(stored) and stored.startswith(_ENC_PREFIX)
