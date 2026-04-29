"""PWA app shell: manifest, service worker, offline fallback.

Tests the contract that lets a browser register and run our SW:
  * `/manifest.webmanifest` returns valid JSON with the required keys
    and the correct MIME type so browsers actually pick it up.
  * `/sw.js` is reachable from the site root, is served as JavaScript,
    and carries `Service-Worker-Allowed: /` so the SW can control the
    whole site rather than just `/static/`.
  * `/offline` renders without auth — the SW pre-caches it on install
    and serves it back during navigation failures, where the user
    is implicitly logged out from the SW's point of view.
  * The SW pre-cache list matches the URLs the server actually exposes
    — drift here would silently break offline mode.
"""

from __future__ import annotations

import json


def test_manifest_is_valid_json(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    body = json.loads(resp.data)
    for required in ("name", "short_name", "start_url", "display", "icons"):
        assert required in body, f"manifest missing {required}"
    assert body["start_url"] == "/"
    assert body["scope"] == "/"
    assert body["display"] == "standalone"
    assert isinstance(body["icons"], list) and body["icons"], "manifest needs at least one icon"


def test_manifest_no_cache_headers(client):
    resp = client.get("/manifest.webmanifest")
    assert "no-cache" in resp.headers.get("Cache-Control", "").lower()


def test_service_worker_served_at_root(client):
    """SW must be at /sw.js (not /static/sw.js) so its scope covers the
    whole site. Browsers refuse to register a SW outside its source dir
    unless `Service-Worker-Allowed` says otherwise."""
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.mimetype in ("application/javascript", "text/javascript")
    body = resp.data.decode("utf-8")
    assert "addEventListener('install'" in body
    assert "addEventListener('fetch'" in body


def test_service_worker_allowed_root_scope(client):
    resp = client.get("/sw.js")
    assert resp.headers.get("Service-Worker-Allowed") == "/"


def test_service_worker_no_cache(client):
    resp = client.get("/sw.js")
    assert "no-cache" in resp.headers.get("Cache-Control", "").lower()


def test_offline_page_renders_without_auth(client):
    """The offline page is served from the SW's cache during a network
    drop, with no Flask-Login state. It must therefore render without
    @login_required redirecting to /auth/login."""
    resp = client.get("/offline")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Bilingual labels — check the English string lands by default.
    assert "offline" in body.lower()


def test_sw_register_script_present_in_base_template(client):
    """The base template loads sw-register.js with `defer`. Without
    this the SW never installs."""
    # Use any authenticated-or-public page — login renders base.html.
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "/static/js/sw-register.js" in body
    assert "manifest.webmanifest" in body


def test_sw_pre_cache_list_matches_exposed_routes(client):
    """If sw.js promises to pre-cache a URL the server doesn't actually
    serve, the install event fails and the SW never activates. Walk the
    APP_SHELL list and hit each URL."""
    sw = client.get("/sw.js").data.decode("utf-8")
    # Extract the array literal — small custom parser to avoid
    # pulling in a JS dependency for one test.
    start = sw.find("APP_SHELL = [")
    assert start != -1, "could not find APP_SHELL declaration"
    end = sw.find("]", start)
    chunk = sw[start:end]
    urls = [
        line.strip().strip(",").strip("'").strip('"')
        for line in chunk.splitlines()
        if line.strip().startswith(("'", '"'))
    ]
    assert urls, "APP_SHELL is empty — SW would cache nothing"
    for url in urls:
        resp = client.get(url)
        assert resp.status_code == 200, f"SW pre-cache target {url} returned {resp.status_code}"
