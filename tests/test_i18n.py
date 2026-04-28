"""Internationalization tests."""

from __future__ import annotations

from flask import g

from app.i18n import gettext, i18n_field


def test_default_language_is_english(app, client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"Sign in" in resp.data


def test_polish_via_cookie(app, client):
    client.set_cookie("lang", "pl", domain="localhost")
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Logowanie".encode() in resp.data


def test_unknown_key_falls_back_to_key(app):
    with app.test_request_context("/"):
        g.lang = "en"
        assert gettext("__missing__.key") == "__missing__.key"


def test_format_substitution(app):
    with app.test_request_context("/"):
        g.lang = "en"
        out = gettext("auth.login.locked", minutes=15)
        assert "15" in out


def test_i18n_field_picks_current_language(app):
    with app.test_request_context("/"):
        g.lang = "pl"
        assert i18n_field({"pl": "Wykrycie", "en": "Detection"}) == "Wykrycie"
        g.lang = "en"
        assert i18n_field({"pl": "Wykrycie", "en": "Detection"}) == "Detection"
        # Fallback to en when target missing.
        g.lang = "pl"
        assert i18n_field({"en": "Only EN"}) == "Only EN"
        # Empty -> empty string.
        assert i18n_field(None) == ""


def test_language_switch_endpoint(app, client, login_admin):
    login_admin()
    resp = client.get("/auth/lang/pl?next=/tickets/", follow_redirects=False)
    assert resp.status_code in (302, 303)
    cookies = resp.headers.getlist("Set-Cookie")
    assert any("lang=pl" in c for c in cookies)
