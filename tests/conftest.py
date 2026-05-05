from __future__ import annotations

import fakeredis
import pytest

from app import create_app
from app.extensions import db
from app.seeds import seed_initial


@pytest.fixture()
def redis_server():
    """Single FakeServer per test, shared by text + binary clients."""
    return fakeredis.FakeServer()


@pytest.fixture()
def redis_client(redis_server):
    """Text-mode (`decode_responses=True`) client for the stream service."""
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture()
def redis_binary_client(redis_server):
    """Binary client for RQ — RQ stores job data as raw bytes and cannot
    be paired with `decode_responses=True`."""
    return fakeredis.FakeRedis(server=redis_server)


@pytest.fixture()
def app(redis_client, redis_binary_client):
    from cryptography.fernet import Fernet

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "BCRYPT_LOG_ROUNDS": 4,
            "AUTO_CREATE_TABLES": True,
            "REDIS_CLIENT": redis_client,
            "REDIS_BINARY_CLIENT": redis_binary_client,
            # Default off so existing tests aren't tripped by the
            # token bucket. Tests that exercise rate limiting flip it
            # on explicitly via app.config[...]= True.
            "RATELIMIT_ENABLED": False,
            # Per-test Fernet key — TOTP enrolment paths exercise the
            # encryption helper.
            "TOTP_ENC_KEY": Fernet.generate_key().decode("ascii"),
            # Off by default so the bulk of test fixtures (which use the
            # seeded admin without TOTP) aren't redirected to enrolment.
            # The dedicated TOTP-enforcement test flips this on per-app.
            "ENFORCE_TOTP_FOR_ROLES": False,
        }
    )
    with application.app_context():
        seed_initial(admin_email="admin@test", admin_password="Admin123!")
        yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_session(app):
    yield db.session


@pytest.fixture()
def login_admin(client):
    def _do_login(email: str = "admin@test", password: str = "Admin123!"):
        return client.post(
            "/auth/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )

    return _do_login
