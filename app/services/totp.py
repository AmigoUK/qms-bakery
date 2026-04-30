"""TOTP 2FA — enrollment, verification, role-based requirement.

Roles that handle compliance-critical actions are required to have TOTP
enabled before they can sign critical decisions (close ticket, define CCP,
configure system). The check is enforced via `require_totp_for_role()` and
the dedicated `/auth/2fa/*` flows.

Secrets in `users.totp_secret` are encrypted at rest via
`app.services.totp_crypto`; a database leak therefore does NOT yield
working TOTP seeds without the env-held `TOTP_ENC_KEY`.
"""

from __future__ import annotations

import pyotp
from flask import current_app

from app.models._base import utcnow
from app.models.auth import User, UserRoleEnum
from app.services import totp_crypto

# Roles that MUST have 2FA enabled to perform sensitive actions.
# Operators / line staff aren't in scope: they don't sign compliance docs.
ROLES_REQUIRING_2FA: frozenset[str] = frozenset(
    {
        UserRoleEnum.COMPLIANCE.value,
        UserRoleEnum.ADMIN.value,
    }
)


def _issuer() -> str:
    """The label that appears in the user's authenticator app. Read from
    config so multi-tenant or rebranded deployments don't need a code
    change (and can keep already-enrolled users working — TOTP itself
    only depends on the secret, not the issuer)."""
    return current_app.config["TOTP_ISSUER"]


def role_requires_totp(role_code: str | None) -> bool:
    return role_code in ROLES_REQUIRING_2FA


def begin_enrollment(user: User) -> tuple[str, str]:
    """Generate a fresh TOTP secret and a provisioning URI.

    The plaintext secret is shown to the user once (via the QR code /
    otpauth URI on the enrolment page); only the encrypted form lands
    in the database. `totp_enrolled_at` stays NULL until
    `complete_enrollment()` confirms the first valid code.
    """
    secret = pyotp.random_base32()
    user.totp_secret = totp_crypto.encrypt(secret)
    user.totp_enrolled_at = None
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=_issuer())
    return secret, uri


def complete_enrollment(user: User, code: str) -> bool:
    if not user.totp_secret:
        return False
    try:
        plaintext = totp_crypto.decrypt(user.totp_secret)
    except totp_crypto.TOTPCryptoError:
        # Legacy or undecryptable secret — refuse rather than treat as
        # enrolled. Force the user back through begin_enrollment().
        return False
    if not _verify(plaintext, code):
        return False
    user.totp_enrolled_at = utcnow()
    return True


def verify_code(user: User, code: str) -> bool:
    if not user.totp_enabled:
        return False
    try:
        plaintext = totp_crypto.decrypt(user.totp_secret)
    except totp_crypto.TOTPCryptoError:
        return False
    return _verify(plaintext, code)


def _verify(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code or len(code) != 6 or not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)
