"""TOTP 2FA — enrollment, verification, role-based requirement.

Roles that handle compliance-critical actions are required to have TOTP
enabled before they can sign critical decisions (close ticket, define CCP,
configure system). The check is enforced via `require_totp_for_role()` and
the dedicated `/auth/2fa/*` flows.

We deliberately keep the secret in the same `users` table for simplicity in
this MVP. Production note: store in a separate, encrypted-at-rest table or
KMS, and never expose the secret via any API or template after enrollment.
"""

from __future__ import annotations

import pyotp

from app.models._base import utcnow
from app.models.auth import User, UserRoleEnum

# Roles that MUST have 2FA enabled to perform sensitive actions.
# Operators / line staff aren't in scope: they don't sign compliance docs.
ROLES_REQUIRING_2FA: frozenset[str] = frozenset(
    {
        UserRoleEnum.COMPLIANCE.value,
        UserRoleEnum.ADMIN.value,
    }
)

ISSUER = "QMS-Bakery"


def role_requires_totp(role_code: str | None) -> bool:
    return role_code in ROLES_REQUIRING_2FA


def begin_enrollment(user: User) -> tuple[str, str]:
    """Generate a fresh TOTP secret and a provisioning URI.

    Note: secret is staged on the user but `totp_enrolled_at` stays NULL
    until `complete_enrollment()` confirms the first valid code.
    """
    secret = pyotp.random_base32()
    user.totp_secret = secret
    user.totp_enrolled_at = None
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=ISSUER)
    return secret, uri


def complete_enrollment(user: User, code: str) -> bool:
    if not user.totp_secret:
        return False
    if not _verify(user.totp_secret, code):
        return False
    user.totp_enrolled_at = utcnow()
    return True


def verify_code(user: User, code: str) -> bool:
    if not user.totp_enabled:
        return False
    return _verify(user.totp_secret, code)


def _verify(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code or len(code) != 6 or not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)
