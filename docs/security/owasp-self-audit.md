# OWASP Top 10 self-audit

**Date:** 2026-04-29
**Scope:** QMS application (Flask + Postgres + Redis + RQ + MQTT bridge)
at the close of Phase 3.
**Approach:** category-by-category code review. Findings are listed
with severity, location, and disposition (fixed in this pass / accepted
risk / out-of-scope). Regression tests for fixed items live in
`tests/test_security_audit.py`.

This is a self-audit, not a third-party pen-test. It substitutes for an
external review only inasmuch as it forces a structured walk through
the codebase; the next milestone before production rollout is a real
external test.

---

## Summary

| # | Category | Findings | Status |
|---|---|---|---|
| A01 | Broken Access Control | 1 documented (intentional) | Accepted |
| A02 | Cryptographic Failures | 1 fixed (cookie flags) | Fixed |
| A03 | Injection | 0 | Clean |
| A04 | Insecure Design | 0 | Clean |
| A05 | Security Misconfiguration | 0 | Clean |
| A06 | Vulnerable Components | 1 recommendation | Backlog |
| A07 | ID & Auth Failures | 1 fixed (open redirect) | Fixed |
| A08 | Software & Data Integrity | 0 | Clean |
| A09 | Logging & Monitoring | 0 | Clean |
| A10 | SSRF | 1 fixed (webhook URL guard) | Fixed |

Three concrete vulnerabilities were patched in this pass; one
documented design tradeoff was confirmed; one backlog item (CVE
scanning in CI) was added.

---

## A01 — Broken Access Control

**What we checked:** every blueprint route for an explicit auth
decorator; permission checks beyond `@login_required`; IDOR on
parameterised resources.

**Findings:**

- All non-public routes carry `@login_required` plus a
  `@require_permission("…")` decorator that abort(403)s after auditing
  the denial. See `app/auth.py:require_permission`.
- `/healthz` and `/readyz` are intentionally unauthenticated (Compose
  healthcheck, Kubernetes liveness/readiness).
- `/api/v1/*` is protected by HMAC + rate-limit, no Flask-Login
  session — see A07 for details.
- Ticket detail (`tickets.detail`) gates on the global
  `tickets.view` permission rather than per-line ownership. **By
  design** for QMS: compliance and quality roles need cross-line
  visibility to investigate incidents. Documented here so future
  reviewers don't flag it as IDOR.
- Session is hardened by Flask-Login `session_protection="strong"` —
  rotates the session cookie on remote-addr / user-agent change.

**Disposition:** clean. The cross-line visibility for quality roles is
an accepted trade-off; an external auditor should confirm this matches
the customer's data-segregation expectations.

## A02 — Cryptographic Failures

**What we checked:** password hashing rounds; HMAC compare; TLS
enforcement; cookie flags; secret material handling.

**Findings:**

- Passwords: bcrypt with `BCRYPT_LOG_ROUNDS=12` (production), 4 in
  tests. `app/auth.py:hash_password`.
- HMAC verify uses `hmac.compare_digest` (constant-time). Both inbound
  `/api/v1/measurements` and outbound webhook signing use
  SHA-256.
- HSTS (`Strict-Transport-Security: max-age=31536000; includeSubDomains`)
  is set when `request.is_secure`. Behind a reverse proxy, `is_secure`
  reflects the proxy-forwarded scheme — verify the deployment sets
  `X-Forwarded-Proto` and trusts the proxy.
- TOTP secrets: stored in the same `users` table (MVP). The module
  comment in `app/services/totp.py` explicitly notes this is not
  production-grade and recommends a separate encrypted store.
- **FIXED — session cookies missing security flags.** Flask defaults
  set `HttpOnly=True` only; `SameSite` is unset (browsers default to
  `Lax` but explicit is safer) and `Secure` is unset (cookies sent
  over plain HTTP). Same applies to `REMEMBER_COOKIE_*`. We now set
  all three explicitly in `app/__init__.py`:
  - `SESSION_COOKIE_HTTPONLY=True`
  - `SESSION_COOKIE_SAMESITE="Lax"`
  - `SESSION_COOKIE_SECURE=True` (env-overridable to False for local
    HTTP dev only)
  - `REMEMBER_COOKIE_*` mirroring the same.
  Regression: `tests/test_security_audit.py::test_session_cookie_*`.

**Disposition:** fixed.

## A03 — Injection

**What we checked:** SQL access patterns; template auto-escape; format
strings; subprocess use; user-controlled deserialization.

**Findings:**

- All DB access goes through SQLAlchemy ORM or Core
  `select()`/`insert()` constructs. No raw `db.session.execute("SELECT
  ... " + ...)` strings.
- Jinja2 auto-escape is on (Flask default for `.html` templates). Spot
  checks of admin templates (which render JSON-stored bilingual names)
  show no `|safe` escapes on user-controlled fields.
- `app/services/triggers.py:_interpolate` uses `template.format(**ctx)`
  with admin-defined templates — admin can already see anything they
  could exfiltrate via `{x.__class__}`, so blast radius is bounded.
  Templates are not exposed to operator/sensor input.
- No `subprocess`, `os.system`, `eval`, `exec` calls in `app/`.
- WeasyPrint PDF generation reads HTML rendered by Jinja2 — no XML
  external entity exposure.

**Disposition:** clean.

## A04 — Insecure Design

**What we checked:** authentication flows; CSRF; rate limiting; lockout;
2FA enforcement; idempotency; immutable history.

**Findings:**

- CSRF: Flask-WTF `CSRFProtect` is initialised app-wide and exempted
  only on the `/api` blueprint (which authenticates by HMAC instead).
- Rate limits: `/auth/login` (10/min), `/api/v1/measurements`
  (600/min, evaluated *before* signature check so credential floods
  are throttled). See `app/services/ratelimit.py`,
  `tests/test_ratelimit.py`.
- Account lockout: 5 failed attempts → 15 min lock, configurable.
- TOTP 2FA enforced for `compliance` and `admin` roles
  (`app/services/totp.py:ROLES_REQUIRING_2FA`).
- Audit log: chain-hashed (SHA-256 over each row, `prev_checksum` + new
  checksum), append-only. Verifiable end-to-end via
  `audit.verify_chain()`.
- Pipeline edits: never mutate existing rows — saving creates a new
  Pipeline version with `is_active=True` and deactivates the old one.
  Existing tickets keep their `pipeline_id` FK to the old version.

**Disposition:** clean.

## A05 — Security Misconfiguration

**What we checked:** debug mode; default credentials; verbose errors;
security headers; CORS; container hardening.

**Findings:**

- `FLASK_DEBUG` defaults to off (Flask 3 default).
- `SECRET_KEY` is read from env; the dev fallback (`"dev-secret-change-me"`)
  is documented in `_default_config()` and the README quick-start
  prints a fresh value via `secrets.token_hex(32)`.
- Security headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options,
  Referrer-Policy, Permissions-Policy) are wired via
  `app/security_headers.py`. CSP is `'self'` + jsdelivr only, no
  `unsafe-inline` or `unsafe-eval`.
- No CORS middleware: the API is HMAC-authenticated and meant for
  server-to-server / IoT use, not browser clients. If a SPA is added
  later, CORS will need explicit config rather than wildcard.
- Compose healthcheck: `/healthz` over HTTP inside the container.
- Container image (`Dockerfile`): not reviewed in detail in this pass —
  recommend a follow-up to confirm the image runs as a non-root user
  and ships a minimal base image.

**Disposition:** clean for the application; container-image review
deferred to a follow-up.

## A06 — Vulnerable and Outdated Components

**What we checked:** `pyproject.toml` pinning, known-CVE scanning.

**Findings:**

- All deps are floor-pinned (`>=`); recent versions of Flask, SQLAlchemy,
  `requests`, `bcrypt`, `pyotp`, `paho-mqtt`. No deprecated frameworks.
- **Recommendation:** add a CVE-scan step to CI, e.g.:
  ```bash
  uv pip install pip-audit
  uv pip audit
  ```
  Tracked in the README Phase 3 backlog under "OWASP pen-test pass".
  Not a vulnerability in itself, but the absence of automated
  monitoring means a future CVE in `requests`, `flask`, etc. won't
  surface until someone manually checks.

**Disposition:** backlog. No known vulnerable component as of
2026-04-29.

## A07 — Identification and Authentication Failures

**What we checked:** credential storage; lockout; session fixation;
2FA; remember-me; logout; redirect handling.

**Findings:**

- Bcrypt hashing, lockout, TOTP — covered in A02 / A04.
- Flask-Login `session_protection="strong"` rotates session ID on
  login/logout and on remote-IP/UA change.
- Logout invalidates the session via `logout_user()` and is
  POST-only (CSRF-protected), so a `<img src="/auth/logout">`
  embedded by an attacker can't force log-out.
- "Remember me": not used. The login form has only email + password +
  CSRF token; no `remember` checkbox.
- **FIXED — open redirect via `?next=`.** The pre-fix `auth.login`
  view redirected to `request.args.get("next")` without validation, so
  `https://qms.local/auth/login?next=https://evil.example/qms` would
  forward to the attacker's site after a successful login (and the
  user would see a `qms.local`-looking URL until the redirect
  completed). Fix in `app/blueprints/auth.py:_safe_next_url`: any
  candidate with a scheme, netloc, or no leading slash falls back to
  the dashboard endpoint. Applied to both
  `auth.login` and `auth.login_2fa`. The `auth.set_language` view
  also normalises `Referer` down to its path before reusing it.
  Regression: `tests/test_security_audit.py::test_safe_next_url_*`
  and `test_login_redirect_strips_external_next`.

**Disposition:** fixed.

## A08 — Software and Data Integrity Failures

**What we checked:** audit chain; FK integrity; data-at-rest hashing;
deserialization risk.

**Findings:**

- Audit log chain-hash (A04) — every state change recorded; tampering
  with any row breaks `verify_chain()`.
- All FKs declared in models; cascade delete used only for
  many-to-many association tables (`role_permissions`,
  `trigger_responders`).
- Untrusted-input deserialization audit: codebase uses `json.loads` and
  SQLAlchemy JSON columns only. No unsafe deserialization libraries
  (Python's pickle, marshal, or `yaml.load` without SafeLoader) appear
  in `app/` — verified by grep.
- No automated package signing / SLSA — out of scope for the
  application; deployment infrastructure decision.

**Disposition:** clean.

## A09 — Security Logging and Monitoring Failures

**What we checked:** audit coverage; sensitive-data redaction; log
retention.

**Findings:**

- Audit recorded for: login success, login failure (via lockout),
  2FA success/failure, every admin mutation, ticket transitions, CCP
  measurements outside limits, trigger fires, and access denials
  (the `audit.record(entity_type="access", action="denied")` line
  in `require_permission`).
- Passwords, TOTP secrets, HMAC keys, session cookies are never
  logged — verified by grep for `logger.info(.*password|secret|token|cookie)`.
- Retention: 7 years (FSA requirement) with WORM replication, per
  README — implemented at the storage layer outside the app.

**Disposition:** clean.

## A10 — Server-Side Request Forgery

**What we checked:** every outbound HTTP call originating from
admin/user-controlled configuration.

**Findings:**

- **FIXED — webhook responder accepted any URL.**
  `responders.config["url"]` is admin-configured and went straight to
  `requests.post(url, …)` in `app/jobs/webhook.py`. A misconfigured
  or compromised admin account could point a webhook at:
  - `http://169.254.169.254/latest/meta-data/` (cloud metadata)
  - `http://localhost:6379/` (Redis)
  - `http://10.0.0.5/admin` (internal services)
  Fix: `app/jobs/webhook.py:_validate_outbound_url` runs before the
  HTTP call. Rejects any URL whose hostname resolves to a loopback,
  link-local, RFC1918 / unique-local, multicast, reserved, or
  unspecified address. Non-`http(s)` schemes also rejected. Raises
  `UnsafeWebhookURL` (a `ValueError` subclass) — permanent error,
  RQ shouldn't retry. Tests pass `allow_private=True` for the cases
  where they're stubbing `requests.post` and want to exercise the
  signing path.
  Regression: `tests/test_security_audit.py::test_ssrf_guard_*`.
- Inbound `/api/v1/measurements` accepts JSON only (no URL fields),
  so no inbound SSRF surface.
- ClickSend SMS responder (`app/jobs/sms.py`) sends to a fixed
  `https://rest.clicksend.com/v3/sms/send` derived from app config —
  not user-controlled.
- SMTP responder posts to `SMTP_HOST` from env, not from
  trigger config.
- WeasyPrint: HTML is rendered locally; if a future template embeds
  user-controlled `<img src>`, WeasyPrint will fetch it. Templates
  reviewed in this pass don't reference user-supplied URLs.

**Disposition:** fixed.

---

## Backlog from this audit

1. CI: add `pip-audit` (or equivalent) to the dependency-update job so
   new CVEs in pinned floors surface automatically. (A06)
2. Container image hardening review: confirm non-root user, minimal
   base image, no shell-history or build-cache leakage. (A05)
3. TOTP secret encryption-at-rest: move out of the `users.totp_secret`
   column into a separate encrypted store before any deployment that
   stores customer compliance data. (A02)
4. External pen-test pass before going live with real production
   data — this self-audit narrows the obvious surface area but does
   not substitute for adversarial review.

## Verifying the fixes

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_security_audit.py -v
# 27 passed
```
