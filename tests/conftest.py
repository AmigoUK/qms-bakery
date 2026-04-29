from __future__ import annotations

import fakeredis
import pytest

from app import create_app
from app.extensions import db
from app.seeds import seed_initial


@pytest.fixture()
def redis_client():
    """Fresh in-memory Redis per test (so streams/groups don't leak)."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def app(redis_client):
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "BCRYPT_LOG_ROUNDS": 4,
            "AUTO_CREATE_TABLES": True,
            "REDIS_CLIENT": redis_client,
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
