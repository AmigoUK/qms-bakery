"""ProxyFix is wired in only when TRUSTED_PROXY_HOPS > 0.

Without TRUSTED_PROXY_HOPS, an attacker spoofing X-Forwarded-For could
otherwise bucket every login attempt under a fresh identity and bypass
the rate-limiter. ProxyFix lets us trust XFF up to a configured depth
(matching the actual reverse-proxy chain) and ignore it otherwise.
"""

from __future__ import annotations

import fakeredis
from cryptography.fernet import Fernet

from app import create_app


def _make_app(*, hops: int):
    server = fakeredis.FakeServer()
    return create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "BCRYPT_LOG_ROUNDS": 4,
            "AUTO_CREATE_TABLES": True,
            "REDIS_CLIENT": fakeredis.FakeRedis(server=server, decode_responses=True),
            "REDIS_BINARY_CLIENT": fakeredis.FakeRedis(server=server),
            "RATELIMIT_ENABLED": False,
            "TOTP_ENC_KEY": Fernet.generate_key().decode("ascii"),
            "ENFORCE_TOTP_FOR_ROLES": False,
            "TRUSTED_PROXY_HOPS": hops,
        }
    )


def test_xff_ignored_when_no_proxy_hops():
    app = _make_app(hops=0)

    @app.route("/_test_remote")
    def _r():
        from flask import request
        return request.remote_addr or ""

    client = app.test_client()
    resp = client.get(
        "/_test_remote",
        headers={"X-Forwarded-For": "1.2.3.4"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
    )
    # Without ProxyFix the spoofed XFF is ignored; the raw socket peer wins.
    assert resp.get_data(as_text=True) == "10.0.0.1"


def test_xff_honoured_when_one_proxy_hop():
    app = _make_app(hops=1)

    @app.route("/_test_remote")
    def _r():
        from flask import request
        return request.remote_addr or ""

    client = app.test_client()
    resp = client.get(
        "/_test_remote",
        headers={"X-Forwarded-For": "1.2.3.4"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
    )
    # With one proxy hop, the rightmost untrusted XFF entry becomes
    # remote_addr — letting the rate-limiter bucket the real client.
    assert resp.get_data(as_text=True) == "1.2.3.4"
