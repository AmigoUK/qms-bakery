"""TOTP key rotation: TOTP_ENC_KEYS_OLD + flask totp-rotate-keys."""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.extensions import db
from app.services import totp_crypto


def test_decrypt_with_old_key_in_secondary_list(app):
    """A token encrypted under what is later moved to TOTP_ENC_KEYS_OLD
    must still decrypt — that's the whole point of multi-key rotation."""
    with app.app_context():
        original_key = app.config["TOTP_ENC_KEY"]
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")

        # Promote a new primary; demote the old one.
        new_key = Fernet.generate_key().decode("ascii")
        app.config["TOTP_ENC_KEY"] = new_key
        app.config["TOTP_ENC_KEYS_OLD"] = original_key

        # Old token still decrypts.
        assert totp_crypto.decrypt(token) == "JBSWY3DPEHPK3PXP"
        # And it's correctly flagged as not-on-primary.
        assert totp_crypto.is_on_primary_key(token) is False

        # A freshly-encrypted token uses the new primary.
        new_token = totp_crypto.encrypt("XYZ123")
        assert totp_crypto.is_on_primary_key(new_token) is True
        assert totp_crypto.decrypt(new_token) == "XYZ123"


def test_old_key_not_listed_fails_decryption(app):
    """A token encrypted under a key that is NEITHER primary NOR in OLD
    must fail — confirms we don't have a stray accept-anything fallback."""
    with app.app_context():
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        # Rotate to a fresh key, do NOT carry the old one in OLD.
        app.config["TOTP_ENC_KEY"] = Fernet.generate_key().decode("ascii")
        app.config["TOTP_ENC_KEYS_OLD"] = ""
        try:
            totp_crypto.decrypt(token)
        except totp_crypto.TOTPCryptoError:
            pass
        else:
            raise AssertionError("decrypt should have raised")


def test_encrypt_always_uses_primary_even_with_old_keys(app):
    """The fresh ciphertext must be readable by primary alone (so old
    keys could be retired immediately for new rows)."""
    with app.app_context():
        app.config["TOTP_ENC_KEYS_OLD"] = Fernet.generate_key().decode("ascii")
        token = totp_crypto.encrypt("JBSWY3DPEHPK3PXP")
        assert totp_crypto.is_on_primary_key(token) is True


def test_multiple_old_keys(app):
    """Comma-separated TOTP_ENC_KEYS_OLD covers more than one historical key."""
    with app.app_context():
        # Encrypt under two different old keys
        original = app.config["TOTP_ENC_KEY"]
        token_a = totp_crypto.encrypt("AAAA")
        new_a = Fernet.generate_key().decode("ascii")
        app.config["TOTP_ENC_KEY"] = new_a
        token_b = totp_crypto.encrypt("BBBB")
        new_b = Fernet.generate_key().decode("ascii")
        app.config["TOTP_ENC_KEY"] = new_b
        # Now original and new_a should both be in OLD
        app.config["TOTP_ENC_KEYS_OLD"] = f"{new_a},{original}"
        # All three vintages decrypt.
        assert totp_crypto.decrypt(token_a) == "AAAA"
        assert totp_crypto.decrypt(token_b) == "BBBB"
        token_c = totp_crypto.encrypt("CCCC")
        assert totp_crypto.decrypt(token_c) == "CCCC"


# ─── flask totp-rotate-keys CLI ─────────────────────────────────────


def test_rotate_keys_dry_run_reports_no_writeback(app):
    """The dry-run path must not modify any row."""
    from app.models import User
    from app.services import totp as totp_service

    with app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        totp_service.begin_enrollment(user)
        db.session.commit()
        before = user.totp_secret
        # Rotate keys: move current primary to OLD, generate a new one.
        original = app.config["TOTP_ENC_KEY"]
        app.config["TOTP_ENC_KEY"] = Fernet.generate_key().decode("ascii")
        app.config["TOTP_ENC_KEYS_OLD"] = original

    runner = app.test_cli_runner()
    result = runner.invoke(args=["totp-rotate-keys"])
    assert result.exit_code == 0
    assert "to rotate: 1" in result.output
    assert "DRY RUN" in result.output

    with app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        assert user.totp_secret == before  # unchanged


def test_rotate_keys_apply_re_encrypts(app):
    """--apply re-encrypts every TOTP-enrolled user under the new primary."""
    from app.models import User
    from app.services import totp as totp_service

    with app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        secret, _ = totp_service.begin_enrollment(user)
        db.session.commit()
        original_key = app.config["TOTP_ENC_KEY"]

    # Rotate
    new_key = Fernet.generate_key().decode("ascii")
    with app.app_context():
        app.config["TOTP_ENC_KEY"] = new_key
        app.config["TOTP_ENC_KEYS_OLD"] = original_key

    runner = app.test_cli_runner()
    result = runner.invoke(args=["totp-rotate-keys", "--apply"])
    assert result.exit_code == 0, result.output
    assert "Rotated 1" in result.output

    # The row is now on the new primary alone — drop OLD and verify.
    with app.app_context():
        app.config["TOTP_ENC_KEYS_OLD"] = ""
        user = User.query.filter_by(email="admin@test").first()
        assert totp_crypto.is_on_primary_key(user.totp_secret)
        # And the original plaintext survives the re-encryption.
        assert totp_crypto.decrypt(user.totp_secret) == secret


def test_rotate_keys_noop_when_already_on_primary(app):
    """No work to do → exits cleanly with a "to rotate: 0" line."""
    from app.models import User
    from app.services import totp as totp_service

    with app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        totp_service.begin_enrollment(user)
        db.session.commit()

    runner = app.test_cli_runner()
    result = runner.invoke(args=["totp-rotate-keys", "--apply"])
    assert result.exit_code == 0
    assert "to rotate: 0" in result.output
