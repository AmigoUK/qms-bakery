"""Public privacy notice — Art. 13 UK GDPR transparency requirement."""

from __future__ import annotations


def test_privacy_route_is_public(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Spot-check that the rendered page has the substantive sections.
    for fragment in (
        "Privacy notice",
        "Data controller",
        "What we hold",
        "Why we hold it",
        "Your rights",
        "Information Commissioner",
    ):
        assert fragment in body, f"missing fragment: {fragment!r}"


def test_login_page_links_privacy(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "/privacy" in resp.get_data(as_text=True)


def test_privacy_renders_polish_when_lang_pl(client):
    client.get("/auth/lang/pl")
    resp = client.get("/privacy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Polityka prywatności" in body
    assert "Information Commissioner" in body  # ICO referenced verbatim
