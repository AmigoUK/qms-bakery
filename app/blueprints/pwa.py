"""PWA shell: service worker, manifest, offline fallback.

Service workers can only intercept requests within their *scope*, which
defaults to the directory the SW file is served from. Flask's static
serves at `/static/sw.js`, which would scope the SW to `/static/*`
only — useless for an app shell. So we expose `/sw.js` and
`/manifest.webmanifest` from the root URL via `send_from_directory`.

Both files are short-cached (`Cache-Control: no-cache`) so a refreshed
SW (bumped `CACHE_VERSION`) propagates to clients on next page load
without a hard refresh.
"""

from __future__ import annotations

import os

from flask import Blueprint, Response, current_app, render_template, send_from_directory

bp = Blueprint("pwa", __name__)


def _static_dir() -> str:
    return os.path.join(current_app.root_path, "static")


@bp.route("/sw.js")
def service_worker() -> Response:
    response = send_from_directory(
        _static_dir(), "sw.js", mimetype="application/javascript"
    )
    # `no-cache` (revalidate every load) keeps SW updates near-instant
    # without serving stale code on every navigation.
    response.headers["Cache-Control"] = "no-cache"
    # Allow the SW to control the whole site, not just /static/*.
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@bp.route("/manifest.webmanifest")
def manifest() -> Response:
    response = send_from_directory(
        _static_dir(), "manifest.webmanifest", mimetype="application/manifest+json"
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/offline")
def offline() -> str:
    """Fallback page served by the SW when navigation requests fail.

    Pre-cached on SW install, so the browser has it ready before any
    network drop. Authenticated state is not required — the service
    worker hands the response back from cache without going through
    Flask-Login.
    """
    return render_template("offline.html")
