# Architectural and functional plan
## Quality Management System (QMS) for UK food production

> **Domain:** Bakery / food production
> **Regulatory region:** United Kingdom (FSA, SALSA, HACCP)
> **Tech stack:** Flask + UV (Python), HTML/CSS/JS, PostgreSQL, Redis, MQTT
> **Operating mode:** Multiuser, multilingual (PL/EN)
> **Document version:** 1.0
> **Date:** 2026-04-28

---

## 0. Executive summary

The QMS for a bakery is a web platform that records, classifies and processes **quality nonconformities** (so-called *tickets*) on the production line. Every ticket runs through a **configurable pipeline of stages** (from detection, through analysis, corrective action, verification, to closure). The system integrates with IoT devices (oven temperature sensors, scales, humidity meters) and lets operators raise incidents manually from a shop-floor tablet.

Key business value:

| Value | Mechanism |
|---|---|
| **FSA and SALSA compliance** | Full audit trail, HACCP-conformant CCP documentation, 1-click reports |
| **Reduced raw-material waste** | Early anomaly detection (sensor triggers → automatic batch hold) |
| **Faster reaction time** | Responders firing actions (SMS / e-mail notification, line pause) |
| **Process measurability** | KPIs: First Pass Yield, NCR rate, MTTR, Cost of Poor Quality |
| **Multinational workforce** | PL/EN UI — typical UK bakery team |

Key architectural decisions (justified in section 1):

- **Flask + UV** — lightweight, mature Python framework; UV for deterministic builds and fast developer onboarding.
- **PostgreSQL 16** — JSONB for flexible pipeline fields, partitioning of `audit_log` by date, transactional integrity critical for CCP.
- **Redis + RQ** — asynchronous responders, trigger queue, sessions.
- **MQTT (Mosquitto)** — de-facto standard for IoT in food production, low-bandwidth, QoS.
- **HTMX + Vanilla JS + Web Components** — no heavy bundlers; fast loading on the lower-end shop-floor hardware.

---

## 1. System architecture

### 1.1. Layered model

The system follows a classic three-tier architecture with an additional integration layer for IoT:

```
┌──────────────────────────────────────────────────────────────┐
│  PRESENTATION LAYER                                           │
│  • Web frontend (HTML5 + CSS3 + Vanilla JS + HTMX)            │
│  • PWA for tablet operators (offline-first)                   │
│  • Web Components for widgets (timeline, drag-drop pipeline)  │
└──────────────────────────────────────────────────────────────┘
                            ▲ HTTPS / REST + Server-Sent Events
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  BUSINESS LOGIC LAYER (Flask + UV)                            │
│  • Flask App (WSGI: gunicorn with uvicorn workers)            │
│  • Blueprints: auth, tickets, pipeline, triggers, admin, api  │
│  • SQLAlchemy 2.0 ORM + Alembic (migrations)                  │
│  • Flask-Babel (PL/EN i18n)                                   │
│  • Flask-Login + RBAC (custom decorators)                     │
│  • Rule engine (triggers/responders) — custom DSL in JSONB    │
│  • RQ Worker (asynchronous tasks)                             │
└──────────────────────────────────────────────────────────────┘
       ▲                    ▲                    ▲
       │ MQTT              │ SQL               │ Redis
       ▼                    ▼                    ▼
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ Mosquitto   │      │ PostgreSQL  │      │   Redis     │
│ (IoT bridge)│      │     16      │      │ cache+queue │
└─────────────┘      └─────────────┘      └─────────────┘
       ▲
       │ sensors, scales, meters
   ┌───┴────┐
   │  IoT   │
   │ (floor)│
   └────────┘
```

### 1.2. Components and responsibilities

| Component | Technology | Responsibility |
|---|---|---|
| **Reverse proxy** | Nginx | TLS termination, rate-limiting, static assets |
| **Flask application** | Python 3.12 + UV | Business logic, REST API, Jinja2 template rendering |
| **Worker** | RQ (Python) | Asynchronous responders, PDF report generation, notification dispatch |
| **MQTT Bridge** | paho-mqtt + Flask | Subscribes to device topics, normalises payload, enqueues for ticketing |
| **Relational database** | PostgreSQL 16 | Persistent data, transactional integrity, audit log |
| **Cache and queue** | Redis 7 | Sessions, rate-limiting, RQ queue, pub/sub for SSE |
| **File storage** | Local volume / S3 | Ticket attachments (shop-floor photos), PDF reports |

### 1.3. Justification of key choices

**Why Flask, not Django/FastAPI?**
Flask offers minimalism and full control over blueprint structure, which matters when building a custom rule engine and a domain-specific HACCP pipeline. Django is too opinionated (admin), and FastAPI lacks mature support for the server-rendered HTML required by the operator PWA. Flask + Blueprints + SQLAlchemy is a proven stack for compliance applications.

**Why UV?**
UV (Astral) provides 10–100× faster dependency resolution than pip, deterministic `uv.lock`, and easy onboarding (`uv sync`). Critical for CI/CD and installation on production environments with limited bandwidth.

**Why PostgreSQL?**
- JSONB lets us store the pipeline definition as a flexible document, without schema migrations every time a production line changes.
- Declarative partitioning of `audit_log` by `created_at` (monthly) — required for the FSA-mandated 7-year retention.
- Serializable transactions for CCP measurement recording (atomicity guaranteeing measurement and its consequences are consistent).
- Full-text search (`tsvector`) for searching ticket comments.

**Why HTMX instead of React/Vue?**
Operators on the floor use Android tablets that are several years old, often while wearing gloves, in noise. HTMX delivers fast, server-rendered HTML with minimal JS. No bundler = no `node_modules` = lower maintenance bar. Web Components are reserved for stateful widgets (ticket timeline, drag-drop pipeline configurator).

---

## 2. Module specification

### 2.1. `auth` module — Authentication and authorisation

**Responsibility:** Login, sessions, RBAC, password policy, optional 2FA.

**Components:**
- `UserModel` (SQLAlchemy) — `id`, `email`, `password_hash` (bcrypt cost 12), `role_id`, `language`, `is_active`, `last_login_at`, `failed_attempts`.
- `RoleModel` + `PermissionModel` — many-to-many relation.
- `@require_permission('tickets.create')` decorator on Flask views.
- `Flask-Login` for sessions + `Flask-WTF` with CSRF.
- Policy: lockout after 5 failed attempts for 15 min, forced password change every 90 days (SALSA requirement for accounts with CCP access).

**Endpoints:**
- `POST /auth/login` / `POST /auth/logout`
- `POST /auth/2fa/enroll` / `POST /auth/2fa/verify` (TOTP)
- `POST /auth/password/change`

### 2.2. `tickets` module — Quality nonconformities

**Responsibility:** Ticket lifecycle (report → analysis → action → verification → closure), classification, assignment, attachments.

**Ticket states (state machine):**
```
NEW → ASSIGNED → IN_PROGRESS → AWAITING_VERIFICATION → CLOSED
                      ↓                                    ↑
                  ESCALATED ──────────────────────────────┘
                      ↓
                  REJECTED (with justification)
```

Every state transition writes a row into `ticket_events` (who, when, from, to, comment).

**Ticket fields:**
- `id` (UUID), `production_line_id`, `pipeline_id`, `current_stage_id`
- `source` (enum: `manual`, `iot`, `api`)
- `severity` (enum: `low`, `medium`, `high`, `critical`)
- `category` (configurable enum: `temperature_deviation`, `weight_out_of_spec`, `foreign_body`, `allergen_cross_contact`, `hygiene`, `other`)
- `title`, `description` (i18n: original-language flag + raw text)
- `assigned_to_user_id`, `created_by_user_id`
- `created_at`, `updated_at`, `closed_at`
- `metadata` (JSONB) — e.g. sensor readings, batch_id, lot_number
- `is_ccp_related` (boolean) — flag indicating link to a critical control point

**Key views:**
- List with filters (line, status, severity, date, assignee)
- Detail (timeline + attachments + comments + actions)
- Quick-report form (mobile, 3 taps)

### 2.3. `pipeline` module — Configurable stages

**Responsibility:** Define the sequence of stages per production line; enforce ordering; validate transitions.

**Model:**
- `Pipeline` — definition per `production_line_id`, versioned (subsequent versions on configuration change, old ones retained for historical tickets).
- `PipelineStage` — `name`, `order_index`, `required_role_id`, `sla_minutes` (a trigger fires when exceeded), `required_fields` (JSONB: list of field names required to advance), `is_ccp_checkpoint`.

**Configurator (UI):**
- Drag-and-drop list of stages (Web Component built on the HTML5 Drag API).
- Each stage: editable title (PL/EN), required role, SLA, list of required fields.
- Versioning: editing creates `version+1`; tickets retain `pipeline_version_id`.

**Default pipeline for a bakery** (example):
1. **Detection** (shop-floor operator) — description + photo mandatory
2. **Classification** (QA specialist) — assign category and severity
3. **Root-cause analysis** (QA specialist) — 5 Whys / Ishikawa, optional
4. **Corrective action** (line manager) — action description, batch hold/release
5. **Verification** (QA specialist) — confirm effectiveness
6. **Closure** (line manager) — digital signature

### 2.4. `triggers` module — Rule engine

**Responsibility:** Detect conditions (e.g. "temperature > 220°C for > 30s") and emit internal events.

**Trigger definition (JSONB):**
```json
{
  "name": "Oven 1 overheating",
  "scope": "production_line:LINE_A",
  "condition": {
    "metric": "temperature",
    "operator": ">",
    "value": 220,
    "duration_seconds": 30
  },
  "severity": "high",
  "create_ticket": true,
  "ticket_template": {
    "category": "temperature_deviation",
    "title_pl": "Przegrzanie pieca {{device_id}}",
    "title_en": "Oven {{device_id}} overheating"
  },
  "responders": ["resp_notify_qa", "resp_pause_line"]
}
```

**Implementation:** the evaluator runs in the context of an IoT reading stream. Each MQTT reading lands on a Redis Stream; a worker subscribes and runs `evaluate_triggers(reading)`. Time-windowed state (e.g. "for 30s") is held in Redis with TTL.

### 2.5. `responders` module — Reactive actions

**Responsibility:** Execute scheduled actions in response to a trigger or a manual decision.

**Responder types:**
| Type | Action |
|---|---|
| `notify_email` | Send e-mail to recipient list (with i18n template) |
| `notify_sms` | SMS via Twilio / local provider |
| `notify_in_app` | In-app push notification + chime |
| `create_ticket` | Create a new ticket |
| `pause_line` | Send MQTT command to a device (line halt) |
| `escalate` | Escalate ticket to a higher role |
| `webhook` | POST to external URL (e.g. ERP) |

Each responder execution is recorded in `trigger_executions` (audit) — when, which trigger, which responder, status (success/failed), payload.

### 2.6. `haccp` module — HACCP and Critical Control Points

**Responsibility:** CCP definitions, measurement recording, alerting on deviation, corrective-action documentation.

**Model:**
- `CCPDefinition` — `name`, `production_line_id`, `parameter` (e.g. "internal bread temperature"), `critical_limit_min`, `critical_limit_max`, `unit`, `monitoring_frequency_minutes`, `corrective_action_template`.
- `CCPMeasurement` — `ccp_definition_id`, `measured_value`, `measured_at`, `measured_by_user_id`, `device_id` (if IoT), `is_within_limits`, `linked_ticket_id` (if deviation).

**Workflow:**
1. CCP defined by the Compliance Officer in the admin panel.
2. The system enforces measurement at the configured frequency (operator notifications).
3. Out-of-limit reading → automatic high-severity ticket with **Corrective action** stage required.
4. HACCP report generated as a monthly PDF listing all measurements + deviations + corrective actions.

### 2.7. `salsa` module — SALSA checklists

**Responsibility:** Recurring checklists conformant to the SALSA (Safe And Local Supplier Approval) standard.

**Checklist scope:**
- **Personal hygiene** — daily (gloves, masks, jewellery, health check)
- **Machine hygiene** — before each shift (ATP swab optional)
- **Goods inwards** — at every raw-material delivery (temperatures, packaging, paperwork)
- **Allergen control** — when switching production line between recipes
- **Pest control** — weekly
- **Traceability** — at every batch (lot/batch)

**Model:**
- `SalsaChecklist` — template: `name`, `frequency` (`daily`/`shift`/`weekly`/`per_event`), `items` (JSONB list of questions).
- `SalsaResponse` — submission: `checklist_id`, `responded_by`, `responded_at`, `answers` (JSONB), `nonconformities_count`, `signature_hash`.

Checklists are part of the shift workflow — without a completed shift-open checklist, the operator cannot record CCP measurements.

### 2.8. `audit` module — Audit trail

**Responsibility:** Immutable record of every action in the system.

**`audit_log` model:**
```sql
id BIGSERIAL PRIMARY KEY,
occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
user_id UUID,                  -- NULL for system events
session_id UUID,
entity_type VARCHAR(50),       -- e.g. 'ticket', 'ccp_measurement'
entity_id UUID,
action VARCHAR(50),            -- 'create', 'update', 'delete', 'state_change', 'view'
diff JSONB,                    -- before/after
ip_address INET,
user_agent TEXT,
checksum CHAR(64)              -- SHA-256 of previous record (chain hashing)
```

**Safeguards:**
- `INSERT`-only table (PostgreSQL trigger blocks UPDATE/DELETE).
- Chain hashing — each record contains the SHA-256 of the previous one (tamper-evidence).
- Daily replication to WORM storage (e.g. AWS S3 Object Lock in compliance mode).
- Monthly partitioning — 7-year retention conformant with FSA.

### 2.9. `reporting` module — Reporting

**Responsibility:** Generate reports for the FSA, internal audits, and management.

**Report types:**
| Report | Frequency | Format | Recipient |
|---|---|---|---|
| HACCP Monitoring Report | Monthly | PDF | Compliance Officer / FSA |
| SALSA Compliance Report | Quarterly | PDF | SALSA auditor |
| NCR Report (Non-Conformity) | Weekly | PDF + CSV | QA Manager |
| Production Quality KPI Dashboard | Live | HTML | Plant Manager |
| Traceability Report (per batch) | Ad-hoc | PDF | FSA / customer |
| Audit Trail Export | Ad-hoc | CSV / PDF (signed) | External auditor |

**Implementation:** WeasyPrint (HTML→PDF) for reports; Jinja2 templates with full i18n; digital signature on the report (PDF with embedded certificate).

### 2.10. `admin` module — Administration panel

**Responsibility:** System configuration without developer involvement.

**Features:**
- CRUD for production lines
- Pipeline configurator (drag-drop)
- CCP definitions
- SALSA checklist templates
- Trigger definitions (JSON-builder form)
- User and role management
- Notification configuration (channels, recipients)
- UI translations (message-catalog editor)
- Integration health checks (MQTT status, RQ queue)

### 2.11. `i18n` module — Multilingual support

**Responsibility:** Full PL/EN localisation.

**Implementation:**
- Flask-Babel + `.po` / `.mo` files in `app/translations/{pl,en}/LC_MESSAGES/`.
- Language detection: cookie → user preference → `Accept-Language` → fallback `en`.
- Dynamic content (e.g. pipeline stage titles) stored in JSONB as `{"pl": "...", "en": "..."}`.
- `gettext_dynamic(field, lang)` function in Jinja2.
- Reports exported in the recipient's language (`?lang=en` parameter).

See: document **02-architecture-diagrams.md**, Diagram 5.

### 2.12. `integrations` module — External integrations

**Responsibility:** Communication with devices and external systems.

**MQTT Bridge:**
- Subscribes to topics `factory/{line}/{device}/{metric}`.
- Payload normalisation (different vendors — different formats: JSON, CSV, binary) via an adapter layer (`adapters/oven_xyz.py`).
- Offline buffering (Redis Stream, max 100k readings / line, FIFO drop).

**REST API:**
- `/api/v1/tickets` (POST) — accepts submissions from external systems (e.g. ERP, complaint portal).
- `/api/v1/measurements` (POST) — measurements from devices that don't support MQTT.
- API-key + HMAC in the `X-Signature` header for authorisation.
- Rate-limiting: 100 req/min per key.

**Outbound webhooks:**
- POST to a configured URL on event (`ticket.created`, `ccp.violated`).
- Retry with exponential backoff (3, 9, 27 minutes), DLQ in Redis once exhausted.

---

## 3. Data flows and integrations

### 3.1. Three ticket sources

#### Source 1: Manual entry (shop floor)
1. Operator taps "New report" on the tablet (PWA).
2. Form: line (auto-detected from the logged-in device), category, severity, description, camera photo.
3. Submit → `POST /tickets` → validation → save → emit `ticket.created` event.
4. Rules engine runs `notify_qa` if severity ≥ high.

#### Source 2: Production devices (IoT)
1. Sensor publishes to MQTT topic `factory/line_a/oven_1/temp` every 1s.
2. Mosquitto forwards to the Flask MQTT Bridge.
3. Bridge validates payload → inserts into Redis Stream `metrics:line_a`.
4. Worker `trigger_evaluator` consumes the stream → evaluates active triggers.
5. Trigger satisfied → creates ticket via `TicketService.create_from_trigger()`.

#### Source 3: External API
1. The ERP system POSTs `/api/v1/tickets` with a customer complaint.
2. API-key + HMAC validation.
3. Field mapping (external → internal) via adapter.
4. Ticket created with `source=api` flag and source metadata.

### 3.2. Internal flows

**Trigger → Responder:**
```
IoT reading → Redis Stream → Worker → Trigger Engine
   → match? → emit `trigger.fired` → Responder Dispatcher
   → execute action (notify/create_ticket/pause_line)
   → audit_log INSERT
```

**CCP measurement:**
```
Operator enters reading → validate against critical_limits
   → if out of limit: create ticket (severity=critical)
                    + alert Compliance Officer
                    + block batch release
   → audit_log INSERT
```

### 3.3. Diagrams

Detailed sequence diagrams and flowcharts live in **02-architecture-diagrams.md**:
- Diagram 1 — Layered architecture
- Diagram 2 — Ticket flow
- Diagram 3 — Compliance module integration

---

## 4. Database model

### 4.1. ERD (logical)

```
                                    ┌──────────────┐
                                    │    users     │
                                    └──────┬───────┘
                                           │ N:1
                          ┌────────────────┴────────────────┐
                          │                                  │
                    ┌─────▼──────┐                    ┌──────▼──────┐
                    │   roles    │                    │ audit_log   │
                    └─────┬──────┘                    └─────────────┘
                          │ M:N                              ▲
                    ┌─────▼──────┐                           │ INSERT
                    │permissions │                           │ on every
                    └────────────┘                           │ action
                                                             │
   ┌──────────────────┐    1:N    ┌──────────────────┐      │
   │ production_lines ├───────────►│   pipelines    │      │
   └──────┬───────────┘            └────────┬─────────┘      │
          │ 1:N                             │ 1:N            │
          │                          ┌──────▼──────────┐     │
          │                          │ pipeline_stages │     │
          │                          └─────────────────┘     │
          │                                                  │
   ┌──────▼─────┐         ┌──────────────┐                   │
   │  tickets   ├─────────► ticket_events├───────────────────┤
   └─────┬──────┘  1:N    └──────────────┘                   │
         │                                                   │
         │ 1:N                                               │
   ┌─────▼─────────────┐                                     │
   │ticket_attachments │                                     │
   └───────────────────┘                                     │
                                                             │
   ┌──────────────────┐    ┌──────────────────┐              │
   │ ccp_definitions  ├───►│ ccp_measurements ├──────────────┤
   └──────────────────┘ 1:N└──────────────────┘              │
                                                             │
   ┌──────────────────┐    ┌──────────────────┐              │
   │ salsa_checklists ├───►│ salsa_responses  ├──────────────┤
   └──────────────────┘ 1:N└──────────────────┘              │
                                                             │
   ┌──────────────────┐    ┌──────────────────────┐          │
   │    triggers      ├───►│ trigger_executions   ├──────────┘
   └──────────┬───────┘ 1:N└──────────────────────┘
              │ M:N
       ┌──────▼──────┐
       │  responders │
       └─────────────┘
```

### 4.2. Key tables

#### `users`
| Field | Type | Index/Constraint |
|---|---|---|
| id | UUID | PK |
| email | VARCHAR(255) | UNIQUE |
| password_hash | VARCHAR(60) | bcrypt |
| role_id | UUID | FK → roles |
| language | CHAR(2) | DEFAULT 'en', CHECK IN ('pl','en') |
| is_active | BOOLEAN | DEFAULT TRUE |
| failed_attempts | INT | DEFAULT 0 |
| last_login_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | DEFAULT now() |

#### `roles`, `permissions`, `role_permissions`
Classic RBAC pattern. `permissions.code` is a string such as `tickets.create`, `pipeline.configure`, `audit.export`.

#### `production_lines`
| Field | Type |
|---|---|
| id | UUID PK |
| name | VARCHAR(100) |
| location | VARCHAR(100) |
| is_active | BOOLEAN |
| metadata | JSONB |

#### `pipelines`
| Field | Type |
|---|---|
| id | UUID PK |
| production_line_id | UUID FK |
| version | INT |
| is_active | BOOLEAN |
| created_at | TIMESTAMPTZ |
| created_by_user_id | UUID FK |

UNIQUE (`production_line_id`, `version`).

#### `pipeline_stages`
| Field | Type |
|---|---|
| id | UUID PK |
| pipeline_id | UUID FK |
| order_index | SMALLINT |
| name | JSONB | (`{"pl": "...", "en": "..."}`) |
| required_role_id | UUID FK |
| sla_minutes | INT |
| required_fields | JSONB |
| is_ccp_checkpoint | BOOLEAN |

INDEX (`pipeline_id`, `order_index`).

#### `tickets`
| Field | Type |
|---|---|
| id | UUID PK |
| ticket_number | VARCHAR(20) UNIQUE | (e.g. `QMS-2026-00042`) |
| production_line_id | UUID FK |
| pipeline_id | UUID FK |
| current_stage_id | UUID FK |
| status | VARCHAR(20) |
| source | VARCHAR(10) | (`manual`/`iot`/`api`) |
| severity | VARCHAR(10) |
| category | VARCHAR(40) |
| title | TEXT |
| description | TEXT |
| description_lang | CHAR(2) |
| created_by_user_id | UUID FK |
| assigned_to_user_id | UUID FK |
| is_ccp_related | BOOLEAN |
| metadata | JSONB |
| created_at | TIMESTAMPTZ |
| updated_at | TIMESTAMPTZ |
| closed_at | TIMESTAMPTZ |

**Indexes:**
- `idx_tickets_status_open` — partial: `WHERE status NOT IN ('CLOSED','REJECTED')`
- `idx_tickets_line_created` — `(production_line_id, created_at DESC)`
- `idx_tickets_severity_created` — `(severity, created_at DESC)`
- `idx_tickets_assignee_status` — `(assigned_to_user_id, status)`
- `idx_tickets_metadata_gin` — GIN on `metadata` (search by batch_id, lot_number)

#### `ticket_events`
| Field | Type |
|---|---|
| id | BIGSERIAL PK |
| ticket_id | UUID FK |
| event_type | VARCHAR(30) | (`status_change`, `comment`, `attachment_added`, `assigned`) |
| from_status | VARCHAR(20) |
| to_status | VARCHAR(20) |
| from_stage_id | UUID |
| to_stage_id | UUID |
| user_id | UUID FK |
| comment | TEXT |
| occurred_at | TIMESTAMPTZ |
| payload | JSONB |

INDEX `(ticket_id, occurred_at)`.

#### `audit_log`
See section 2.8. Monthly partitioning (`PARTITION BY RANGE (occurred_at)`), 7-year retention.

#### `ccp_definitions`, `ccp_measurements`
See section 2.6. INDEX `(ccp_definition_id, measured_at DESC)` to speed up report generation.

#### `salsa_checklists`, `salsa_responses`
See section 2.7.

#### `triggers`
| Field | Type |
|---|---|
| id | UUID PK |
| name | JSONB |
| scope | VARCHAR(100) |
| condition | JSONB |
| severity | VARCHAR(10) |
| is_active | BOOLEAN |
| created_by_user_id | UUID |

#### `responders`
| Field | Type |
|---|---|
| id | UUID PK |
| name | JSONB |
| type | VARCHAR(30) |
| config | JSONB |
| is_active | BOOLEAN |

#### `trigger_responders` (M:N)
PK (`trigger_id`, `responder_id`, `order_index`).

#### `trigger_executions`
Every responder execution. Index on `(trigger_id, executed_at DESC)`.

#### `translations` (optional — for runtime edits)
Override of Babel `.po` via the admin panel. Key + language + text.

### 4.3. Indexing and performance strategy

- **Partial indexes** for open tickets (the majority of queries are about active ones).
- **GIN** on JSONB where searching by fields is needed (`metadata`, `name`).
- **Partitioning** of `audit_log` and `ccp_measurements` by date (12 months active, older ones read-only).
- **VACUUM/ANALYZE** scheduled nightly.
- **Replication** read-replica for reporting (separated from OLTP).
- **Connection pooling** via PgBouncer in transaction mode.

### 4.4. Scaling

| Scale | Strategy |
|---|---|
| **MVP / 1 plant / ~50 users** | Single-node PostgreSQL, ~100 GB |
| **5 plants / 250 users** | Read-replica + PgBouncer, partitioning |
| **Enterprise / >1000 users** | Sharding by `tenant_id` + Citus / Patroni HA |

---

## 5. UX/UI — wireframes and principles

### 5.1. Design principles

1. **Shop floor ≠ office.** Tablets used with gloves → buttons min 56×56 px, WCAG AAA contrast.
2. **3 taps to a report.** The operator has no time to click through 10 screens.
3. **Offline-first.** PWA caches the most recent data; submissions are queued in IndexedDB.
4. **Language — one tap.** PL/EN switch in the top-right corner, persisted per user.
5. **Dark mode and high contrast.** The shop floor is sometimes dark, sometimes glaring.
6. **No IT jargon.** "Report" instead of "ticket", "stage" instead of "stage" (in Polish: "etap" instead of "stage"), "alarm" instead of "trigger".

### 5.2. Wireframe — Main dashboard

```
┌────────────────────────────────────────────────────────────────────┐
│  QMS — Bakery A                     🇵🇱 PL │ 🇬🇧 EN     [Jan K. ▼] │
├────────────────────────────────────────────────────────────────────┤
│ ▌ MENU       │  ALERTS (3)                                          │
│              │  ┌──────────────────────────────────────────────┐   │
│ ▣ Dashboard  │  │ 🔴 LINE A — oven 1 — temp 232°C   [GO]       │   │
│ ▢ Tickets    │  │ 🟠 LINE B — scale — dev +3.2%     [GO]       │   │
│ ▢ Pipeline   │  │ 🟡 LINE C — SLA exceeded          [GO]       │   │
│ ▢ HACCP/CCP  │  └──────────────────────────────────────────────┘   │
│ ▢ SALSA      │                                                      │
│ ▢ Reports    │  LINE OVERVIEW                                       │
│ ▢ Admin      │  ┌─────────┬─────────┬─────────┐                    │
│              │  │ LINE A  │ LINE B  │ LINE C  │                    │
│              │  │  ✅ OK  │ ⚠️ NCR │  🔴 STOP│                    │
│              │  │  98% FPY│ 92% FPY │  —      │                    │
│              │  │  2 open │ 5 open  │ 12 open │                    │
│              │  └─────────┴─────────┴─────────┘                    │
│              │                                                      │
│              │  KPI (24h)            │  CCP (today)                 │
│              │  • Open: 19           │  ┌──────────────────────┐   │
│              │  • Closed: 47         │  │ ▓▓▓▓▓▓░░░ 7/9 done   │   │
│              │  • MTTR: 42 min       │  │ ❌ 1 deviation        │   │
│              │  • Severity h+: 3     │  └──────────────────────┘   │
└──────────────┴──────────────────────────────────────────────────────┘
```

### 5.3. Wireframe — Ticket list

```
┌────────────────────────────────────────────────────────────────────┐
│  TICKETS                                       [+ NEW TICKET]      │
├────────────────────────────────────────────────────────────────────┤
│ Line: [All ▼]   Status: [Open ▼]   Severity: [All ▼]               │
│ Date from: [____] to: [____]   🔍 Search: [_____________]   [Filter]│
├────────────────────────────────────────────────────────────────────┤
│ # NUMBER      │ LINE  │ CATEGORY    │ SEV │ STATUS  │ SLA  │ ACT.  │
├───────────────┼───────┼─────────────┼─────┼─────────┼──────┼───────┤
│ QMS-2026-0042 │ A     │ Temperature │ 🔴  │ Analysis│ 12m  │ [▶]   │
│ QMS-2026-0041 │ B     │ Weight      │ 🟠  │ Action  │ 1h   │ [▶]   │
│ QMS-2026-0040 │ A     │ Hygiene     │ 🟡  │ Verify  │ 4h   │ [▶]   │
│ QMS-2026-0039 │ C     │ Allergen    │ 🔴  │ Closed  │ —    │ [▶]   │
│ ...                                                                │
├────────────────────────────────────────────────────────────────────┤
│                                  [‹ Prev]   Page 1/12   [Next ›]   │
└────────────────────────────────────────────────────────────────────┘
```

### 5.4. Wireframe — Ticket detail

```
┌────────────────────────────────────────────────────────────────────┐
│ ← Back   QMS-2026-0042: Oven 1 overheating (LINE A)                │
│            🔴 Severity: HIGH    Status: ANALYSIS   SLA: 12 min ⏱️   │
├────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────┬─────────────────────────────────────┐ │
│ │ TIMELINE                 │ AVAILABLE ACTIONS                   │ │
│ │                          │ [Advance to next stage]             │ │
│ │ 🟢 14:02 Reported        │ [Escalate to manager]               │ │
│ │    auto from sensor      │ [Add comment]                       │ │
│ │                          │ [Attach file]                       │ │
│ │ 🟢 14:03 Classified      │                                     │ │
│ │    Anna K. — cat: temp   │ RELATED MEASUREMENTS                │ │
│ │                          │ • Temp 232°C @ 14:01:32             │ │
│ │ 🔵 14:05 Analysis        │ • Temp 234°C @ 14:01:58             │ │
│ │    In progress (Marek W.)│ • Temp 230°C @ 14:02:15             │ │
│ │                          │                                     │ │
│ │ ⚪ Corrective action     │ LINKED CCP: Oven temp               │ │
│ │ ⚪ Verification          │ Measurement required: YES           │ │
│ │ ⚪ Closure               │ Corrective action: template available│ │
│ └──────────────────────────┴─────────────────────────────────────┘ │
│                                                                    │
│ COMMENTS                                                           │
│ ┌────────────────────────────────────────────────────────────────┐ │
│ │ Anna K. (14:03): Classified as temperature_deviation           │ │
│ │ Marek W. (14:05): Checking sensor and calibration...           │ │
│ └────────────────────────────────────────────────────────────────┘ │
│ [Add comment_________________________________________] [Send]      │
└────────────────────────────────────────────────────────────────────┘
```

### 5.5. Wireframe — Pipeline configuration

```
┌────────────────────────────────────────────────────────────────────┐
│ PIPELINE CONFIGURATION — LINE A          Version: 7 (draft)        │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ 1. DETECTION │→│ 2. CLASSIFY  │→│ 3. ANALYSIS  │→ ...           │
│  │ Role: Op.    │  │ Role: QA     │  │ Role: QA     │              │
│  │ SLA: 5 min   │  │ SLA: 15 min  │  │ SLA: 60 min  │              │
│  │ ☐ CCP        │  │ ☐ CCP        │  │ ☑ CCP        │              │
│  │ [✏ Edit]     │  │ [✏ Edit]     │  │ [✏ Edit]     │              │
│  │ [🗑 Delete]  │  │ [🗑 Delete]  │  │ [🗑 Delete]  │              │
│  └──────────────┘  └──────────────┘  └──────────────┘              │
│        ⇅                ⇅                ⇅                          │
│      drag                                                          │
│                                                                    │
│  [+ Add stage]                                                     │
│                                                                    │
│  ────────────────────────────────────────────────────────────────  │
│  [Cancel]   [Save as draft]   [Publish version 7]                  │
└────────────────────────────────────────────────────────────────────┘
```

### 5.6. Wireframe — Mobile (PWA, shop-floor operator)

```
┌──────────────────────┐
│ 🇬🇧  QMS  LINE A    │
├──────────────────────┤
│                      │
│  +  NEW TICKET       │
│ ┌──────────────────┐ │
│ │  ️⚠️ REPORT      │ │
│ │     A PROBLEM    │ │
│ └──────────────────┘ │
│                      │
│  📋 TODAY'S CHECKLISTS│
│ ┌──────────────────┐ │
│ │ Hygiene   ✅     │ │
│ │ Machines  ⚠️ 1/3 │ │
│ │ Goods in  ⏳     │ │
│ └──────────────────┘ │
│                      │
│  📊 MY TICKETS       │
│ ┌──────────────────┐ │
│ │ 0042 🔴 Analysis │ │
│ │ 0038 🟡 Action   │ │
│ └──────────────────┘ │
│                      │
│  🌡️ CCP MEASUREMENTS │
│ ┌──────────────────┐ │
│ │ Oven 1 temp      │ │
│ │ [____] °C [Save] │ │
│ └──────────────────┘ │
└──────────────────────┘
```

### 5.7. Wireframe — Admin panel (trigger definition)

```
┌────────────────────────────────────────────────────────────────────┐
│ NEW TRIGGER — Definition                                           │
├────────────────────────────────────────────────────────────────────┤
│ Name (PL): [Przegrzanie pieca 1                  ]                 │
│ Name (EN): [Oven 1 overheating                   ]                 │
│                                                                    │
│ Scope: [Line A ▼]                                                  │
│                                                                    │
│ CONDITION                                                          │
│ Metric:    [temperature ▼]                                         │
│ Operator:  [>           ▼]                                         │
│ Value:     [220     ] °C                                           │
│ Duration:  [30   ] seconds                                         │
│                                                                    │
│ ON MATCH                                                           │
│ ☑ Create ticket  (severity: [HIGH ▼], category: [Temp ▼])          │
│ ☑ Notify: [QA Manager, Line Manager A      ] (email + SMS)         │
│ ☐ Pause line                                                       │
│                                                                    │
│ [Cancel]                            [Save draft]   [Activate]      │
└────────────────────────────────────────────────────────────────────┘
```

---

## 6. Permissions and roles

### 6.1. Roles

| Role | Code | Description |
|---|---|---|
| **Production operator** | `operator` | Shop-floor worker — reports problems, fills checklists, enters CCP measurements |
| **QA Specialist** | `qa` | Quality specialist — classifies, analyses, verifies corrective actions |
| **Line Manager** | `line_manager` | Line supervisor — approves corrective actions, escalations, batch hold/release |
| **Compliance Officer** | `compliance` | Compliance specialist — defines CCPs, SALSA checklists, exports FSA reports |
| **Plant Manager** | `plant_manager` | Plant lead — reviews KPIs and reports, but does not modify technical configuration |
| **Administrator** | `admin` | System administrator — full configuration, user management, audits |

### 6.2. Permissions matrix (RBAC matrix)

Legend: ✅ full access | 👁️ read-only | ✍️ limited write | ❌ no access

| Function                       | Operator | QA  | Line Mgr | Compl. | Plant Mgr | Admin |
|---|---|---|---|---|---|---|
| Create tickets                 | ✅       | ✅  | ✅       | ✅     | ✅        | ✅    |
| Classify tickets               | ❌       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Corrective action              | ❌       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Approve closure                | ❌       | ❌  | ✅       | ✅     | ❌        | ✅    |
| Enter CCP measurements         | ✅       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Define CCPs                    | ❌       | ❌  | ❌       | ✅     | ❌        | ✅    |
| Fill SALSA checklists          | ✅       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Define SALSA checklists        | ❌       | ❌  | ❌       | ✅     | ❌        | ✅    |
| Configure pipeline             | ❌       | ❌  | ✍️*      | ✅     | ❌        | ✅    |
| Define triggers                | ❌       | ✍️* | ✍️*      | ✅     | ❌        | ✅    |
| User management                | ❌       | ❌  | ❌       | ❌     | ❌        | ✅    |
| Export audit trail             | ❌       | 👁️  | 👁️       | ✅     | 👁️        | ✅    |
| Generate FSA report            | ❌       | ✍️  | ✍️       | ✅     | ✅        | ✅    |
| KPI dashboard                  | 👁️ (line)| 👁️ | 👁️       | 👁️    | 👁️ (plant)| 👁️   |
| System configuration           | ❌       | ❌  | ❌       | ❌     | ❌        | ✅    |
| Browse audit_log               | ❌       | 👁️* | 👁️*      | 👁️    | 👁️        | 👁️   |

\* only own actions / own line.

### 6.3. Concurrency control (multi-user)

- **Optimistic locking** for ticket edits (column `version`, INC on update). Saving a stale version → 409 Conflict + UI asks "take over / refresh".
- **Pessimistic locking** for in-flight CCP measurements (Redis lock with 5-min TTL) — prevents double-recording from two tablets.
- **Server-Sent Events** for real-time ticket-list updates — when QA opens a ticket, the Line Manager's list refreshes automatically.

### 6.4. Access auditing

Every authorisation decision (allow/deny) is recorded in `audit_log` with `entity_type='access'`, `action='check'`. This makes privilege-escalation attempts detectable.

---

## 7. Reporting and analytics

### 7.1. Operational KPIs (live)

| KPI | Definition | Target |
|---|---|---|
| **First Pass Yield (FPY)** | (Batches without NCR / All batches) × 100% | ≥ 98% |
| **NCR Rate** | Tickets count / 1000 batches | ≤ 5 |
| **Mean Time To Resolve (MTTR)** | Average time from `NEW` to `CLOSED` | ≤ 4h for high+ |
| **SLA Compliance** | % tickets closed within SLA | ≥ 95% |
| **CCP Compliance** | % readings within critical limits | ≥ 99.5% |
| **SALSA Checklist Completion** | % checklists submitted on time | 100% |
| **Cost of Poor Quality (CoPQ)** | Σ raw-material loss + downtime | tracked, YoY reduction |

### 7.2. Regulatory reports

#### HACCP report (monthly)
- List of all CCP definitions active during the period
- List of measurements (date, value, in/out of limit, operator)
- Deviations + corrective actions + verifications
- Digital signature by Compliance Officer (TOTP-confirmed)
- Format: PDF/A-2 (long-term archival)

#### SALSA report (quarterly)
- Outcome of every checklist in the period
- Nonconformities + actions
- Trends (% completion, % nonconformities)

#### FSA report on demand
- Per-batch traceability: where the raw material came from, when, who, which measurements, which tickets
- Generated in < 60 seconds (FSA requirement during inspections)

### 7.3. Analytics dashboard

- Chart.js charts — 7/30/90-day trends
- Drill-down: click a bar → list of tickets in the period
- Heatmap of CCP deviations (hour × weekday)
- Pareto of most frequent NCR categories

### 7.4. Exports

| Format | Content |
|---|---|
| CSV | Tickets, CCP measurements, SALSA checklists — raw data for BI |
| PDF | Signed reports |
| JSON | API export for integration with BI/Power BI |
| Audit Trail (CSV/PDF, signed) | For external auditors |

---

## 8. Rollout plan

### 8.1. Phases

#### Phase 0 — Discovery (4 weeks)
- Interviews with operators, QA, Line Managers, Compliance Officer
- IoT device inventory (models, protocols)
- Mapping of existing processes and (paper) documents
- Selection of pilot line
- **Deliverable:** Functional spec v1.1

#### Phase 1 — MVP (8 weeks)
- Infrastructure setup (Docker Compose dev + staging)
- Auth, RBAC, basic ticket CRUD
- Static pipeline (1 line, 5 hardcoded stages)
- Manual ticket entry (PWA)
- Audit trail
- PL/EN i18n
- **Deliverable:** Working system without IoT, ready for internal testing

#### Phase 2 — Pilot on 1 line (6 weeks)
- Configurable pipeline + admin panel
- HACCP/CCP — definitions + manual measurements
- SALSA checklists
- MQTT integration — 2-3 pilot sensors
- Triggers and basic responders (notify_email, notify_in_app)
- HACCP report (PDF)
- **Deliverable:** Pilot on line A; operator training

#### Phase 3 — Compliance validation (4 weeks)
- Internal audit by Compliance Officer
- Pre-audit SALSA — external-audit dry run
- Penetration test (OWASP Top 10)
- Load test (Locust — 200 tickets/min)
- Recovery test (database failover, MQTT restart)
- **Deliverable:** Internal compliance certificate; ready for SALSA audit

#### Phase 4 — Rollout (8-12 weeks)
- Gradual cutover of remaining lines (1 line / 2 weeks)
- Migration of historical data (scans of paper records)
- Training for all shifts (operator + line manager)
- 2-week hypercare after each line cutover
- **Deliverable:** Full plant on the QMS

#### Phase 5 — Optimisation (ongoing)
- Trigger tuning based on 3 months of data
- Add ML for deviation prediction (optional, after stabilisation)
- ERP integration (raw-material orders, traceability)
- Native mobile app (if PWA proves insufficient)

### 8.2. Resourcing

| Role | FTE | Phase |
|---|---|---|
| Product Owner / Business Analyst | 1.0 | 0–5 |
| System architect | 0.5 | 0–2 |
| Backend dev (Python/Flask) | 2.0 | 1–4 |
| Frontend dev (HTML/CSS/JS) | 1.0 | 1–4 |
| DevOps / SRE | 0.5 | 0–5 |
| QA Engineer | 1.0 | 1–4 |
| UX Designer | 0.5 | 0–2 |
| Compliance specialist (in-house) | 0.3 | 0–5 |
| **Total (peak)** | **~6.8 FTE** | Phase 2 |

### 8.3. Timeline (overview)

```
M1   M2   M3   M4   M5   M6   M7   M8   M9   M10  M11  M12
├─P0─┤
       ├──── Phase 1 (MVP) ────┤
                              ├── Phase 2 (Pilot) ──┤
                                                  ├─P3─┤
                                                       ├── Phase 4 (Rollout) ────┤
                                                                                  ├ Phase 5 →
```

### 8.4. Per-phase acceptance criteria (DoD)

- All automated tests green (≥ 80% coverage)
- Pen test with no Critical/High findings
- Audit trail complete for all actions
- User documentation (PL+EN) up to date
- End-user training delivered
- Acceptance test attended by Plant Manager + Compliance Officer

---

## 9. Risks and mitigations

### 9.1. Technical risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Loss of MQTT connectivity** (shop-floor network) | Medium | High | Buffer in Redis Stream + retry; alert after 60s offline; manual fallback measurement |
| **PostgreSQL outage** | Low | Critical | Streaming replication (Patroni HA), backup every 4h, RPO ≤ 4h, RTO ≤ 1h |
| **Audit log volume growth** | Certain | Medium | Monthly partitioning, archival to S3 Object Lock after 12 months |
| **Slow reports** | Medium | Medium | Read-replica, materialized views for KPIs, nightly generation |
| **Inconsistent IoT formats** | High | Medium | Per-vendor adapter layer, schema validation, DLQ for unsupported payloads |
| **Stale PWA cache** | Medium | Low | Service Worker with `network-first` for critical data, force-refresh after deploy |
| **SQL injection / XSS** | Low | Critical | SQLAlchemy ORM (parameterisation), Jinja2 autoescape, CSP headers, regular SAST |
| **Lack of API idempotency** | Medium | Medium | Required `Idempotency-Key` header, 24h response cache |

### 9.2. Regulatory risks

| Risk | Mitigation |
|---|---|
| **Change in FSA requirements** | Modular reporting architecture; FSA newsletter subscription; quarterly compliance review |
| **Failed SALSA audit** | Internal pre-audit before Phase 4; checklists 1:1 aligned with the standard |
| **GDPR — operator personal data** | DPIA before rollout; data minimisation; retention policy; right to erasure (excluding audit trail under legal basis) |
| **No eIDAS-conformant electronic signature** | TOTP + audit trail as a reasonable substitute for internal processes; for FSA reports — option to export and apply qualified signature outside the system |

### 9.3. Operational risks

| Risk | Mitigation |
|---|---|
| **Worker pushback (paper vs system)** | Champions programme — 1 ambassador per shift; native-language training; UX validated with real operators |
| **Language barrier** | Full PL/EN; pictograms + colours for the most common actions; video manual |
| **Operators raising false alerts (gaming KPIs)** | Audit correlation manual vs IoT; mandatory photo + signature on submission; QA review |
| **Misconfigured triggers (false positives)** | "Dry-run" mode on activating a new trigger (logging only, no action, for 7 days); panel showing false-positive rate stats |
| **Single point of failure — admin** | Min. 2 administrators; vendor escalation in SLA; runbooks |

### 9.4. Business risks

| Risk | Mitigation |
|---|---|
| **Budget overrun** | MVP phase with tightly scoped goals; review after each phase; 15% reserve |
| **Vendor lock-in** | Only open-source in the core stack (Flask, PostgreSQL, Redis, Mosquitto); architecture documentation and runbooks |
| **Loss of a key developer** | Pair programming, code review, internal documentation, no "bus factor = 1" |

### 9.5. Business continuity plan (BCP)

- **Backup:** PostgreSQL pg_basebackup every 4h + WAL archiving every 5 min → S3 with 90-day retention.
- **Disaster Recovery:** Restore tested quarterly; RPO 5 min, RTO 1h.
- **Degraded mode:** If the database is unavailable — operator can fill a paper form (template printed from the system); manual import after recovery (with audit trail flagged `recovery=true`).

---

## Appendix A — Glossary

| Term | Definition |
|---|---|
| **CCP** | Critical Control Point under HACCP |
| **HACCP** | Hazard Analysis and Critical Control Points — food safety system |
| **SALSA** | Safe And Local Supplier Approval — UK certification scheme for small food suppliers |
| **FSA** | Food Standards Agency — UK food regulator |
| **NCR** | Non-Conformity Report |
| **FPY** | First Pass Yield — % of batches produced correctly first time |
| **MTTR** | Mean Time To Resolve |
| **SLA** | Service Level Agreement — agreed reaction time |
| **PWA** | Progressive Web App — offline-capable web application |
| **MQTT** | Message Queuing Telemetry Transport — IoT protocol |
| **RBAC** | Role-Based Access Control |
| **Pipeline** | Sequence of stages a ticket passes through |
| **Trigger** | Rule detecting a condition in data |
| **Responder** | Action executed in response to a trigger |

## Appendix B — Related documents

- `02-architecture-diagrams.md` — detailed technical diagrams
- `README.md` — documentation entry point

---

*Document prepared by the team: QMS Specialist, Python Developer, QA Specialist (UK Bakery), UX/UI Designer.*
