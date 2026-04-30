"""Encryption-at-rest for TOTP secrets, with multi-key rotation support.

Threat model: a database leak (stolen backup, SQL injection, malicious
DBA) gives an attacker every user's TOTP seed and therefore the ability
to derive valid codes forever -- defeating 2FA until each user re-enrols.
With column-level encryption keyed by an env var (`TOTP_ENC_KEY`) held
outside the database, a DB-only compromise no longer yields the seeds.

Format: stored values are `v1:<urlsafe_b64_fernet_token>`. The version
prefix lets us rotate to a new scheme without a one-shot migration --
write `v2:` going forward, accept both during transition, drop `v1:`
once every row has rolled over.

Key rotation
------------
Two-stage rotation:

1. Generate a fresh primary key, move the old one to TOTP_ENC_KEYS_OLD
   (comma-separated for multiple historical keys):

       TOTP_ENC_KEY=<new>            # primary; encrypt always uses this
       TOTP_ENC_KEYS_OLD=<old1>,<old2>  # accepted on decrypt only

   `decrypt()` tries the primary first then each old key in order, so
   pre-rotation rows keep working without immediate re-encryption.

2. Run `flask totp-rotate-keys` to walk every user with a TOTP secret,
   decrypt under the multi-key set, and re-encrypt under the primary
   alone. After this completes, the entries in TOTP_ENC_KEYS_OLD can
   be retired from the env.

Key generation:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app


class TOTPCryptoError(Exception):
    """Encryption helper refuses to operate — missing key or bad payload."""


_ENC_PREFIX = "v1:"


def _normalise_key(k: str | bytes) -> bytes:
    return k.encode("ascii") if isinstance(k, str) else k


def _primary_fernet() -> Fernet:
    """Encryption-only Fernet keyed by the current primary."""
    key = current_app.config.get("TOTP_ENC_KEY")
    if not key:
        raise TOTPCryptoError(
            "TOTP_ENC_KEY is required to read or write TOTP secrets. "
            "Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(_normalise_key(key))


def _multi_fernet() -> MultiFernet:
    """Decrypt-capable Fernet that tries the primary key first then any
    keys configured in TOTP_ENC_KEYS_OLD. Encrypt via MultiFernet always
    uses the FIRST key, which is our primary — that's the property that
    makes rotation gradual rather than big-bang."""
    cfg = current_app.config
    primary = cfg.get("TOTP_ENC_KEY")
    if not primary:
        raise TOTPCryptoError("TOTP_ENC_KEY is required")
    old_raw = cfg.get("TOTP_ENC_KEYS_OLD") or ""
    if isinstance(old_raw, (list, tuple)):
        old_keys = list(old_raw)
    else:
        old_keys = [k.strip() for k in str(old_raw).split(",") if k.strip()]
    fernets = [Fernet(_normalise_key(k)) for k in [primary, *old_keys]]
    return MultiFernet(fernets)


def encrypt(plaintext_secret: str) -> str:
    if not plaintext_secret:
        raise TOTPCryptoError("plaintext is empty")
    # Use the primary directly so the ciphertext is always written under
    # the current key, regardless of any historical keys configured.
    token = _primary_fernet().encrypt(plaintext_secret.encode("utf-8")).decode("ascii")
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
        return _multi_fernet().decrypt(payload).decode("utf-8")
    except InvalidToken as exc:
        raise TOTPCryptoError(
            "TOTP secret decryption failed (no configured key matches)"
        ) from exc


def is_on_primary_key(stored: str) -> bool:
    """True if the stored ciphertext can be decrypted under the primary
    key alone — i.e. it doesn't need rotation. Returns False for
    legacy-plaintext or values that only decrypt under TOTP_ENC_KEYS_OLD."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return False
    payload = stored[len(_ENC_PREFIX):].encode("ascii")
    try:
        _primary_fernet().decrypt(payload)
    except InvalidToken:
        return False
    return True


def is_encrypted(stored: str | None) -> bool:
    """True if the value has been written through `encrypt()`."""
    return bool(stored) and stored.startswith(_ENC_PREFIX)
