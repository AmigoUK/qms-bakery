# QMS — Quality Management System for UK bakery

> **Status:** Pre-implementation documentation (v1.0)
> **Tech stack:** Python 3.12 + Flask + UV · PostgreSQL 16 · Redis 7 · MQTT (Mosquitto) · HTML/CSS/JS + HTMX
> **Regulatory region:** United Kingdom — compliance with **FSA**, **SALSA**, **HACCP**
> **Operating mode:** Multiuser, multilingual (PL/EN), PWA for shop-floor operators

## What this project is

A Quality Management System (QMS) dedicated to UK food production — bakery in particular. It records, classifies and processes **quality nonconformities** (tickets) from three sources:

1. **Manual** — operators report from a shop-floor tablet
2. **IoT** — automatic tickets from devices (temperature sensors, scales) via MQTT
3. **API** — integrations with ERP, customer systems, complaint portals

Every ticket flows through a **configurable pipeline** of stages (detection → classification → analysis → corrective action → verification → closure). A rule engine (triggers + responders) detects anomalies in real time and dispatches actions (notifications, escalations, line pause). All of it backed by a full audit trail and FSA-compliant reporting.

## Documentation

| # | Document | Description |
|---|---|---|
| 1 | [`01-architectural-functional-plan.md`](./01-architectural-functional-plan.md) | Full system plan — architecture, modules, data model, UX, RBAC, rollout plan, risks |
| 2 | [`02-architecture-diagrams.md`](./02-architecture-diagrams.md) | 5 technical Mermaid diagrams: layers, ticket flow, compliance, permissions, i18n |

## Key features

- ✅ **Full SALSA + HACCP + FSA compliance** — checklists, CCP definitions, regulatory reports
- ✅ **Audit trail with chain-hashing** — immutable 7-year record (partitioned, replicated to WORM)
- ✅ **Configurable pipeline** per production line, versioned
- ✅ **Trigger engine** — custom DSL in JSONB, real-time evaluation off Redis Streams
- ✅ **Multi-source tickets** — manual / IoT / API (HMAC + idempotency)
- ✅ **PWA offline-first** — shop-floor operator keeps working even on flaky Wi-Fi
- ✅ **PL/EN** — UI, reports, e-mails per user; dynamic content stored in JSONB

## High-level overview

```mermaid
graph LR
    SRC["📥 Sources<br/>Manual / IoT / API"]
    BUF[("🔄 Redis Stream<br/>qms:readings")]
    APP["⚙️ Flask + UV<br/>Pipeline + Trigger Worker"]
    DB[("🐘 PostgreSQL<br/>+ audit_log")]
    OUT["📤 Actions<br/>Notify · Pause · Report"]
    REP["📊 Reports<br/>HACCP · SALSA · FSA"]

    SRC --> BUF
    BUF --> APP
    APP --> DB
    APP --> OUT
    DB --> REP
```

For the full picture see documents `01-` and `02-`.

## Implementation status (Phase 1 — MVP)

✅ **Already working** (runnable):

- Flask app factory + configuration (UV, `pyproject.toml`)
- SQLAlchemy 2.0 models: User, Role, Permission, ProductionLine, Pipeline, PipelineStage, Ticket, TicketEvent, AuditLog, CCPDefinition, CCPMeasurement, SalsaChecklist, SalsaResponse, Trigger, Responder, TriggerExecution, InAppNotification
- Auth + RBAC (bcrypt, lockout, `@require_permission` decorator)
- **2FA TOTP** (pyotp) — required for `admin` and `compliance` roles
- Tickets: CRUD, state machine, comments, audited transitions
- **HACCP/CCP** — definitions, measurements, automatic tickets on out-of-limit values, per-line scoping
- **SALSA checklists** — bilingual templates, responses, automatic ticket on nonconformity
- **Trigger/responder engine** — JSONB-condition rule engine, responder dispatch (notify_in_app, create_ticket, escalate, webhook), `dry_run` mode
- **REST API** `/api/v1/measurements` with HMAC-SHA256 for IoT/ERP integrations
- **MQTT bridge** (paho-mqtt) — subscribes to `factory/+/+/+`, parses readings, publishes to a Redis Stream; `flask mqtt-bridge` CLI + dedicated Compose service
- **Redis-Stream buffer + trigger worker** — bridge `XADD`s parsed readings to `qms:readings` (bounded MAXLEN 100k, FIFO drop on overflow); a separate `flask trigger-worker` process consumes via `XREADGROUP qms-workers` and feeds the trigger engine. At-least-once delivery, scales horizontally, decouples MQTT from DB latency.
- **Duration-window triggers** — a trigger condition with `duration_seconds: 30` only fires after the metric has continuously breached the threshold for 30s. First-true timestamps are kept in Redis (`trigger_state:<id>:<scope>:first_true`, TTL 3×duration), reset when the condition flips back to safe. Per-scope isolation, no spurious tickets from a single 1-second spike.
- **PDF reports (WeasyPrint)** — `/reports/haccp/monthly?year=&month=&line_id=` renders a print-ready HACCP monthly report (CCP measurements, deviations, within-spec %, signature lines); `/reports/fsa/traceability?from=&to=` exports the chain-verified audit trail for a date range. Both return `application/pdf`, gated by `reports.generate`.
- **RQ worker for async responders** — WEBHOOK responder now enqueues an outbound HTTP POST job onto `qms:webhooks` with HMAC-SHA256 signing (`X-QMS-Signature: sha256=...`), 3/9/27-minute retry backoff, and DLQ via RQ's failed-job registry. `flask rq-worker` CLI + dedicated Compose service.
- **Admin panel** — KPI overview, user CRUD, trigger toggle, audit_log viewer with chain-integrity verification
- **Alembic migrations** (Flask-Migrate) — versioned schema, `flask db upgrade`/`downgrade`, baseline in `migrations/versions/`
- Audit trail with SHA-256 chain-hashing + chain verification (tamper evidence)
- PL/EN i18n via JSON message catalogs
- HTML/CSS/JS frontend (Jinja2) — login (with 2FA), dashboard, tickets, HACCP, SALSA, admin
- Seed data: 6 roles, 17 permissions, demo line with pipeline + 2 CCPs + 2 SALSA + trigger
- **130 pytest tests**, all green
- Docker Compose (Postgres 16 + Redis + Mosquitto + app + mqtt-bridge + trigger-worker + rq-worker)

⏳ **Planned for the next phases** (see `01-architectural-functional-plan.md` section 8):

- Pipeline configurator (drag-and-drop UI)
- Trigger form-builder (currently: enable/disable in admin, raw JSON edit in compliance panel)
- DLQ inspection / replay UI (jobs land in RQ's failed registry today; needs a viewer + manual requeue)
- E-mail / SMS responders (Flask-Mail / Twilio) — same RQ infra as webhooks

## Quick start (local, without Docker)

```bash
# 1. Virtualenv + dependencies
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# 2. Configuration
cp .env.example .env
# Generate SECRET_KEY: python -c "import secrets; print(secrets.token_hex(32))"

# 3. Database init + seed (runs `flask db upgrade` and loads seed data)
export FLASK_APP=app:create_app
flask init-db
# or step by step:
#   flask db upgrade   # apply Alembic migrations
#   flask db current   # show current revision

# 4. Run
flask run
# → http://localhost:5000
# Default account: admin@local / ChangeMe123!
```

## Quick start (Docker Compose)

```bash
echo "SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')" > .env
docker compose up -d postgres redis mosquitto
docker compose run --rm app flask init-db
docker compose up app mqtt-bridge trigger-worker
```

To exercise the MQTT path manually after the stack is up:

```bash
# Publish a reading that exceeds the seeded OVEN1_OVERHEAT threshold (>220°C):
docker compose exec mosquitto \
  mosquitto_pub -t "factory/LINE_A/oven_1/temperature" -m '{"value": 232.5}'
# bridge XADDs to `qms:readings` → trigger-worker XREADGROUPs → trigger fires → ticket created.
# Inspect the buffered stream: `docker compose exec redis redis-cli XLEN qms:readings`
```

## Tests

```bash
PYTHONPATH=. python3 -m pytest -v
# 83 passed in ~6s
```

Tests use SQLite in-memory for speed; production runs on PostgreSQL 16 (see `docker-compose.yml`).

## Project structure

```
app/
├── __init__.py            # Flask app factory + blueprint registration
├── extensions.py          # db, login_manager, csrf
├── i18n.py                # PL/EN message catalogs (cookie/header/user-pref)
├── auth.py                # password hashing, RBAC decorator
├── seeds.py               # idempotent seed data
├── models/
│   ├── _base.py           # UUIDPKMixin, TimestampMixin, utcnow
│   ├── auth.py            # User (+ TOTP fields), Role, Permission
│   ├── production.py      # ProductionLine, Pipeline, PipelineStage
│   ├── tickets.py         # Ticket, TicketEvent + state machine
│   ├── haccp.py           # CCPDefinition, CCPMeasurement
│   ├── salsa.py           # SalsaChecklist, SalsaResponse
│   ├── triggers.py        # Trigger, Responder, TriggerExecution, InAppNotification
│   └── audit.py           # AuditLog (chain-hashed, BIGINT PK)
├── services/
│   ├── audit.py           # record(), verify_chain()
│   ├── tickets.py         # create_ticket, transition, list_tickets
│   ├── haccp.py           # record_measurement → auto-ticket on out-of-spec
│   ├── salsa.py           # submit_response → auto-ticket on nonconformity
│   ├── triggers.py        # evaluate(payload) + responder dispatcher (incl. duration_seconds gate)
│   ├── trigger_state.py   # Redis-backed first-true state for duration-window triggers
│   ├── stream.py          # Redis Stream helpers: publish_reading, consume (XREADGROUP+XACK)
│   ├── reports.py         # HACCP-monthly + FSA-traceability PDF generation (WeasyPrint)
│   ├── queue.py           # RQ queue (qms:webhooks) + retry/backoff policy
│   └── totp.py            # TOTP enroll/verify, role requirement matrix
├── jobs/
│   └── webhook.py         # Outbound HMAC-signed HTTP POST (runs on RQ worker)
├── blueprints/
│   ├── auth.py            # /auth/login (+2FA), /auth/logout, /auth/2fa/*, /auth/lang/<code>
│   ├── dashboard.py       # /
│   ├── tickets.py         # /tickets/*
│   ├── haccp.py           # /haccp/*
│   ├── salsa.py           # /salsa/*
│   ├── admin.py           # /admin/* (users, triggers, audit viewer)
│   ├── reports.py         # /reports/haccp/monthly, /reports/fsa/traceability (PDF)
│   └── api.py             # /api/v1/measurements (HMAC), /api/v1/health
├── mqtt/
│   └── bridge.py          # paho-mqtt subscriber → Redis Stream (enqueue_message)
├── workers/
│   ├── trigger_worker.py  # `flask trigger-worker` — consumes qms:readings → trigger engine
│   └── rq_worker.py       # `flask rq-worker` — drains qms:webhooks (and future async queues)
├── templates/             # Jinja2 templates per blueprint (incl. reports/{base,haccp_monthly,fsa_traceability}.html)
├── static/css/app.css     # Hand-written CSS, mobile-first
└── translations/
    ├── pl.json
    └── en.json

tests/                     # pytest (130 tests, SQLite in-memory + fakeredis)
├── test_models.py
├── test_audit.py
├── test_auth.py
├── test_tickets.py
├── test_haccp.py
├── test_salsa.py
├── test_triggers.py       # incl. signed REST API
├── test_admin.py
├── test_totp.py
├── test_i18n.py
├── test_mqtt_bridge.py    # parser + handle_message integration
├── test_stream.py         # Redis Stream publish + XREADGROUP/XACK (fakeredis)
├── test_trigger_worker.py # bridge → stream → worker → trigger fires (fakeredis)
├── test_trigger_duration.py # duration-window gating + reset on flip-false (fakeredis + freezegun)
├── test_reports.py        # HACCP/FSA PDF service + HTTP routes (WeasyPrint)
├── test_webhook_job.py    # post_webhook unit: HMAC signing, raise-on-non-2xx
└── test_responder_webhook.py # WEBHOOK responder enqueues onto qms:webhooks (fakeredis)
```

## Team

Documentation prepared by a multi-role team:

- 🏗️ **Systems architect** — layer design, integrations, scaling
- 🐍 **Python developer** — framework choice, blueprint structure, ORM
- 🔬 **QMS / UK compliance specialist** — mapping SALSA/HACCP/FSA requirements to features
- 🎨 **UX/UI designer** — wireframes, design rules for the production floor

---

*Documentation version: 1.0 — 2026-04-28*
