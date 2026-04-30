"""Encryption-at-rest for TOTP secrets."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.services import totp_crypto


def test_round_trip(app):
    with app.app_context():
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        assert token.startswith("v1:")
        assert totp_crypto.decrypt(token) == "JBSWY3DPEHPK3PXP"


def test_ciphertext_does_not_leak_plaintext(app):
    """Beyond a sanity check — the stored value mustn't contain any
    obvious base32 substring of the secret."""
    with app.app_context():
        secret = "JBSWY3DPEHPK3PXP"
        token = totp_crypto.encrypt(secret)
    assert secret not in token
    # Different invocations produce different ciphertexts (Fernet uses
    # a random IV) — so a DB-leak attacker can't even tell two users
    # share the same secret.
    with app.app_context():
        token2 = totp_crypto.encrypt(secret)
    assert token != token2


def test_decrypt_with_wrong_key_fails(app):
    """A token encrypted under one key must not decrypt under another."""
    with app.app_context():
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        # Rotate the key
        app.config["TOTP_ENC_KEY"] = Fernet.generate_key().decode("ascii")
        with pytest.raises(totp_crypto.TOTPCryptoError, match="decryption failed"):
            totp_crypto.decrypt(token)


def test_decrypt_legacy_plaintext_refuses(app):
    """A pre-encryption plaintext row must NOT be silently accepted."""
    with app.app_context():
        with pytest.raises(totp_crypto.TOTPCryptoError, match="recognised version"):
            totp_crypto.decrypt("JBSWY3DPEHPK3PXP")  # bare base32, no v1: prefix


def test_decrypt_corrupted_ciphertext_fails(app):
    with app.app_context():
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        # Flip a byte in the middle of the payload
        bad = token[:-2] + "AA"
        with pytest.raises(totp_crypto.TOTPCryptoError):
            totp_crypto.decrypt(bad)


def test_missing_key_raises(app):
    with app.app_context():
        app.config["TOTP_ENC_KEY"] = None
        with pytest.raises(totp_crypto.TOTPCryptoError, match="TOTP_ENC_KEY is required"):
            totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        with pytest.raises(totp_crypto.TOTPCryptoError, match="TOTP_ENC_KEY is required"):
            totp_crypto.decrypt("v1:gAAAAA")


def test_is_encrypted():
    assert totp_crypto.is_encrypted("v1:foo") is True
    assert totp_crypto.is_encrypted("plain") is False
    assert totp_crypto.is_encrypted(None) is False
    assert totp_crypto.is_encrypted("") is False


def test_user_row_holds_ciphertext_only(app):
    """End-to-end: a TOTP-enrolled user's row contains the v1: token,
    not the plaintext base32 secret."""
    from app.models import User
    from app.services import totp as totp_service

    with app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        secret, _uri = totp_service.begin_enrollment(user)
        from app.extensions import db

        db.session.commit()
        # Round-trip via the DB
        db.session.refresh(user)
        assert user.totp_secret.startswith("v1:")
        assert secret not in user.totp_secret
        # And a verify cycle still works
        import pyotp

        live = pyotp.TOTP(secret).now()
        assert totp_service.complete_enrollment(user, live) is True
