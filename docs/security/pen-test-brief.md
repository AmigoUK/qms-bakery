# External penetration test — scope brief

A one-page handoff for a third-party pen-tester engaging with a QMS deployment.
This document is intentionally short; for architectural depth see
`docs/01-architectural-functional-plan.md` and the OWASP self-audit at
`docs/security/owasp-self-audit.md`.

---

## What this system is

A Flask + PostgreSQL + Redis QMS for UK food production: HACCP/SALSA/FSA
compliance, ticket workflow, IoT sensor ingestion (MQTT → Redis Stream →
trigger engine), responder dispatch (in-app, e-mail, SMS, webhook), and
worker training & certification via SMS magic links.

Roughly 12 blueprints, ~320 tests, deployed via Docker Compose
(Postgres 16, Redis 7, Mosquitto, app, mqtt-bridge, trigger-worker,
rq-worker, training-scheduler).

## Two identity models — important

1. **Users** (`users` table): authenticated with bcrypt + optional TOTP
   2FA. Roles: `operator`, `qa`, `line_manager`, `compliance`,
   `plant_manager`, `admin`. Compliance and admin must enable TOTP.
2. **Trainees** (`trainees` table): floor workers, **no password**, no
   session beyond the magic-link click. Identity is the phone number;
   access is via an HMAC-SHA256-signed token in the URL.

Anything that hits the trainee surface (`/training/take/<token>`) must
demonstrate a forged or stolen token to be a valid finding — guessing
the URL space alone is not sufficient.

## Critical assets ranked

| Asset | Where stored | Mitigation |
|---|---|---|
| User passwords | `users.password_hash`, bcrypt rounds≥12 | bcrypt + lockout (5 fails / 15 min) |
| TOTP secrets | `users.totp_secret`, Fernet-encrypted | `TOTP_ENC_KEY` env, rotatable (see README) |
| Audit chain | `audit_log`, SHA-256 chain-hash | tamper-evident; `verify_chain()` runs in tests + `/admin/audit` |
| API HMAC keys | `app.config["API_KEYS"]` (env-injected) | per-key shared secret; signed bodies on `/api/*` |
| Webhook signing key | `Responder.config.secret` (DB) | DB-level confidentiality |
| Training magic-link key | `TOTP_ENC_KEY` falls back from `SECRET_KEY`, or `TRAINING_LINK_SIGNING_KEY` | rotatable; rotation invalidates outstanding links |
| Session cookies | Flask signed sessions | `SESSION_COOKIE_SECURE`, `HTTPONLY`, `SAMESITE=Lax` |
| Trainee declaration signatures | `training_declarations.signature_png` | LargeBinary blobs, PII-adjacent |

## In-scope

Everything reachable over HTTP(S) on the deployed instance:

- Public routes: `/auth/login`, `/auth/login/2fa`, `/auth/lang/*`,
  `/healthz`, `/readyz`, `/sw.js`, `/manifest.webmanifest`,
  `/offline`, `/training/take/<token>` and its sub-routes, `/api/*`.
- Authenticated routes: `/dashboard`, `/tickets`, `/haccp`, `/salsa`,
  `/admin/*`, `/admin/training/*`, `/admin/dlq`, `/reports/*`.
- Authentication mechanisms: bcrypt login, TOTP 2FA, magic-link, HMAC
  API.

We have an explicit interest in:

- **Open-redirect** on `?next=` — already filtered (see
  `_safe_next_url`), but worth a fresh look.
- **SSRF** on the webhook responder — outbound destinations are
  filtered (`_validate_outbound_url`), confirm loopback/link-local/
  RFC1918 are rejected.
- **Audit-chain tampering** — can a logged-in user with audit-write
  permission produce a chain that `verify_chain()` accepts but
  contains a forged entry?
- **Magic-link token forgery / replay** — token format is
  `{enrolment_id}.{exp_unix}.{HMAC}`. Try: timing attacks on the
  comparison, swapping enrolment IDs while keeping a valid sig,
  reusing a submitted token after declaration.
- **CSP bypass** — strict policy is `script-src 'self'`; only
  `static/vendor/` is whitelisted by being self-hosted. Try gadgets
  to land an inline-eval anyway.
- **Rate-limit bypass** — `/auth/login` and `/api/v1/measurements`
  are bucketed per identity (XFF-aware). Try inflating the identity
  space to scale a credential-flood.
- **Mass-assignment** on the admin user form (compliance role can
  edit users — can it grant itself admin?).
- **PWA service-worker abuse** — the SW is served at `/sw.js` with
  `Service-Worker-Allowed: /`. Make sure it can't be made to cache a
  poisoned response.

## Out of scope

- ClickSend SMS API itself (third-party service).
- Mosquitto MQTT broker authentication (deployment-specific; default
  config has anonymous on for local dev).
- Postgres / Redis network exposure (assumed gated by Compose
  network or k8s NetworkPolicy in deployment).
- Tailnet / VPN front-door auth (deployment-specific).
- Physical access to the bakery floor (out of system boundary).
- Denial-of-service via raw network flooding.

## Test environment

A non-prod instance should be stood up with:

- Real `SECRET_KEY`, `TOTP_ENC_KEY`, `TRAINING_LINK_SIGNING_KEY`
  (i.e. not the dev defaults), but ClickSend creds may be empty
  (jobs land in DLQ — that's fine for test purposes).
- A seeded admin via `INITIAL_ADMIN_EMAIL` / `_PASSWORD`.
- `flask init-db` then `flask db upgrade` to land the latest migration.
- The pen-tester is given the admin credentials and a known-good
  trainee phone-number-and-magic-link as starting points.

## What we expect back

A written report with, per finding:
- CVSS-style severity (or your equivalent)
- Reproduction steps (curl / browser actions)
- Affected files / endpoints (best-guess)
- Suggested remediation
- Whether the existing CSP / rate-limit / audit chain blunted impact

Findings are tracked in this repo at `docs/security/findings/<date>-<slug>.md`
(create the dir on first finding).

## What NOT to change during the engagement

- Avoid restarting the app mid-test — most session and rate-limit
  state is in-memory.
- Don't rotate `TOTP_ENC_KEY` or `TRAINING_LINK_SIGNING_KEY` mid-test
  unless you're specifically demonstrating a rotation issue.
- Don't drop the `audit_log` rows — chain verification is part of
  what we're trying to validate.

## Recent changes worth knowing

(Summarised from `git log` — pen-tester should also `git log --oneline -50`
before starting.)

- TOTP secrets are now encrypted at rest (Fernet, rotatable).
- Training feature: trainees, magic links, course versioning, signature
  capture — large new attack surface added in the last two weeks.
- Container hardened: non-root user, runtime-only deps, HEALTHCHECK.
- pip-audit runs weekly in CI on every push to main.
