"""Hand-rolled security-header middleware.

Set as an `@app.after_request` hook so every response carries:

  * Content-Security-Policy — `'self'` for everything. Vendor JS (e.g.
    SortableJS) is self-hosted under /static/vendor/ rather than pulled
    from a CDN, so the policy stays a strict `'self'` allow-list with
    no `'unsafe-inline'` or `'unsafe-eval'`. When a future template
    needs an inline script, prefer extracting it.
  * Strict-Transport-Security — only when the request is HTTPS. Max-age
    is configurable via HSTS_MAX_AGE_SECONDS.
  * X-Frame-Options DENY — defence in depth alongside CSP frame-ancestors;
    clickjacking protection on every page.
  * X-Content-Type-Options nosniff — browsers stop guessing MIME types.
  * Referrer-Policy strict-origin-when-cross-origin — leak no path/query
    cross-origin.
  * Permissions-Policy — explicitly deny powerful APIs we never use.

The middleware is opt-out via `app.config['SECURITY_HEADERS_ENABLED']`
(default True), and respects an extra list of `script-src` hosts via
`app.config['CSP_EXTRA_SCRIPT_SRC']` for future CDN additions.
"""

from __future__ import annotations

from flask import Flask

DEFAULT_SCRIPT_SRC = ("'self'",)
DEFAULT_STYLE_SRC = ("'self'",)
DEFAULT_IMG_SRC = ("'self'", "data:")
DEFAULT_CONNECT_SRC = ("'self'",)
DEFAULT_FONT_SRC = ("'self'",)


def _build_csp(app: Flask) -> str:
    extra_script = tuple(app.config.get("CSP_EXTRA_SCRIPT_SRC", ()))
    script_src = " ".join(DEFAULT_SCRIPT_SRC + extra_script)
    style_src = " ".join(DEFAULT_STYLE_SRC)
    img_src = " ".join(DEFAULT_IMG_SRC)
    connect_src = " ".join(DEFAULT_CONNECT_SRC)
    font_src = " ".join(DEFAULT_FONT_SRC)
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            f"style-src {style_src}",
            f"img-src {img_src}",
            f"connect-src {connect_src}",
            f"font-src {font_src}",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
    )


def init_security_headers(app: Flask) -> None:
    """Wire the after-request hook. No-op when disabled in config."""
    if not app.config.get("SECURITY_HEADERS_ENABLED", True):
        return

    csp = _build_csp(app)

    @app.after_request
    def _set_headers(response):
        # Skip for static-file responses if you ever set a per-route flag;
        # by default we apply to everything since the CSP above is open
        # enough for the actual pages we serve.
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        # HSTS only over HTTPS — sending it on HTTP requests is a no-op
        # per RFC 6797 §7.2 but still confusing in dev logs.
        from flask import request

        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response
