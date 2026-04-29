# Architecture diagrams
## Quality Management System (QMS) — UK Bakery

> **Document purpose:** Diagrams ready for direct implementation by the development team. Notation: Mermaid (renders natively in GitHub/GitLab/VS Code) + descriptive tables.
> **Relation:** Companion to `01-architectural-functional-plan.md`.
> **Version:** 1.0 — 2026-04-28

---

## Diagram index

1. [Layered architecture](#diagram-1--layered-architecture)
2. [Ticket data flow](#diagram-2--ticket-data-flow)
3. [Compliance module integration](#diagram-3--compliance-module-integration)
4. [Permissions and roles](#diagram-4--permissions-and-roles)
5. [Multilingual flow (PL/EN)](#diagram-5--multilingual-flow-plen)

---

## Diagram 1 — Layered architecture

**Role:** Shows the three-tier split of the system (presentation / business logic / data) together with the protocols between layers and the integration points with external systems (IoT devices, ERP, e-mail/SMS).
**Relation:** Foundation for every other diagram. Diagram 2 details the data flow inside the business-logic layer, Diagram 3 shows the compliance modules located in the application layer, Diagrams 4 and 5 describe cross-cutting mechanisms (auth, i18n) that touch all three layers.

```mermaid
graph TB
    subgraph EXT["🌍 EXTERNAL ENVIRONMENT"]
        IOT["🌡️ IoT devices<br/>(ovens, scales, sensors)"]
        ERP["🏭 ERP / customer systems"]
        EMAIL["📧 SMTP Gateway"]
        SMS["📱 SMS Gateway (Twilio)"]
        S3["☁️ Object Storage<br/>(WORM Audit)"]
    end

    subgraph PRES["📺 PRESENTATION LAYER"]
        BROWSER["🖥️ Desktop browser<br/>(QA, manager, admin)"]
        PWA["📱 PWA tablet<br/>(shop-floor operator)"]
        STATIC["Static assets: HTML/CSS/JS<br/>HTMX + Web Components<br/>Chart.js"]
    end

    subgraph BIZ["⚙️ BUSINESS LOGIC LAYER"]
        NGINX["🔀 Nginx<br/>TLS, rate-limit, statics"]
        FLASK["🐍 Flask App (gunicorn)<br/>Blueprints: auth, tickets,<br/>pipeline, triggers, admin"]
        WORKER["⚡ RQ Worker<br/>Async jobs<br/>(responders, PDF reports)"]
        MQTTBR["📡 MQTT Bridge<br/>(paho-mqtt)"]
        BABEL["🌐 Flask-Babel<br/>(PL/EN i18n)"]
        AUTH["🔐 Flask-Login + RBAC"]
    end

    subgraph DATA["💾 DATA LAYER"]
        PG[("🐘 PostgreSQL 16<br/>tickets, pipelines,<br/>CCP, SALSA,<br/>audit_log")]
        REDIS[("⚡ Redis 7<br/>sessions, cache,<br/>RQ queue,<br/>IoT streams")]
        MOSQ["🦟 Mosquitto<br/>(MQTT broker)"]
        FILES["📁 Attachments volume<br/>(photos, PDFs)"]
    end

    BROWSER -->|HTTPS<br/>REST + SSE| NGINX
    PWA -->|HTTPS<br/>REST + SSE| NGINX
    STATIC -.served by.- NGINX

    NGINX -->|WSGI<br/>uvicorn| FLASK
    FLASK <--> AUTH
    FLASK <--> BABEL
    FLASK -->|enqueue| REDIS
    REDIS -->|dequeue| WORKER
    WORKER -->|SQLAlchemy| PG
    FLASK -->|SQLAlchemy 2.0<br/>+ Alembic| PG
    FLASK -->|cache + session| REDIS

    IOT -->|MQTT QoS 1| MOSQ
    MOSQ -->|subscribe<br/>factory/+/+/+| MQTTBR
    MQTTBR -->|XADD| REDIS
    REDIS -->|XREAD stream| WORKER
    WORKER -->|trigger fire| FLASK

    ERP <-->|REST API + HMAC| NGINX
    WORKER -->|SMTP| EMAIL
    WORKER -->|HTTP API| SMS
    WORKER -->|"S3 PUT (Object Lock)"| S3
    PG -.audit replication.- S3

    FLASK -->|read/write| FILES
    WORKER -->|PDF report| FILES

    classDef external fill:#fff4e6,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef presentation fill:#e0f2fe,stroke:#0284c7,stroke-width:2px,color:#000
    classDef business fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#000
    classDef data fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000

    class IOT,ERP,EMAIL,SMS,S3 external
    class BROWSER,PWA,STATIC presentation
    class NGINX,FLASK,WORKER,MQTTBR,BABEL,AUTH business
    class PG,REDIS,MOSQ,FILES data
```

### Implementation notes

| Point | Configuration |
|---|---|
| Nginx → Flask | `proxy_pass http://gunicorn:8000;` + `proxy_set_header X-Forwarded-For ...` |
| Gunicorn | 4 workers `uvicorn.workers.UvicornWorker`, timeout 30s, graceful restart |
| SSE | Endpoint `/events/stream` per user, Last-Event-ID for resume |
| MQTT topic schema | `factory/<line_id>/<device_id>/<metric>` (lower_snake) |
| Redis Stream key | `metrics:<line_id>` with MAXLEN ~ 100000 |
| RQ queues | `default`, `notifications`, `reports` (priorities) |

---

## Diagram 2 — Ticket data flow

**Role:** Presents the full path of a single ticket from its source (manual / IoT / API) through the trigger and responder engines to notifications, state changes and audit trail. Shows pipeline decision points and the difference between the normal path (operator-raised ticket) and the alarm path (auto-generated from an IoT anomaly).
**Relation:** Details the business-logic layer of Diagram 1; integrates with Diagram 3 at the "CCP measurement" and "audit log" points; permissions verified at every step per Diagram 4.

### 2.1. Sequence diagram — manual path

```mermaid
sequenceDiagram
    actor OP as Operator (PWA)
    participant API as Flask API<br/>/tickets
    participant AUTH as Auth/RBAC
    participant SVC as TicketService
    participant DB as PostgreSQL
    participant AUD as audit_log
    participant Q as Redis Queue
    participant W as RQ Worker
    participant N as Notifier

    OP->>API: POST /tickets {line, cat, sev, photo}
    API->>AUTH: check permission<br/>tickets.create
    AUTH-->>API: ✅ allowed
    API->>SVC: create_ticket(payload)
    SVC->>DB: INSERT tickets
    SVC->>DB: INSERT ticket_events (NEW)
    SVC->>AUD: append (action=create)
    Note over AUD: chain hash<br/>SHA-256 prev
    SVC-->>API: ticket_id
    API->>Q: enqueue post_create_hooks
    API-->>OP: 201 + ticket_id

    Q->>W: dequeue
    W->>SVC: evaluate_post_create_triggers
    alt severity >= HIGH
        W->>N: notify QA & Line Manager
        N->>AUD: append (action=notify)
    end
    W->>DB: UPDATE tickets.status<br/>= ASSIGNED
    W->>AUD: append (action=auto_assign)
```

### 2.2. Flowchart — multi-source orchestration

```mermaid
flowchart TB
    START([Event occurs])

    subgraph SOURCES["3 SOURCES"]
        S1["👷 Operator<br/>in PWA"]
        S2["🌡️ IoT device<br/>publishes to MQTT"]
        S3["🏭 ERP / API<br/>POST /api/v1/tickets"]
    end

    START --> S1
    START --> S2
    START --> S3

    S1 -->|HTTPS POST<br/>Cookie session| GW["🚪 API Gateway<br/>(Flask + Auth)"]
    S2 -->|MQTT QoS 1<br/>topic factory/+/+/+| MB["📡 MQTT Bridge"]
    S3 -->|HTTPS POST<br/>X-API-Key + HMAC| GW

    MB --> RS["📥 Redis Stream<br/>metrics:line_X"]
    RS --> TE["⚡ Trigger Evaluator<br/>(worker)"]
    TE --> COND{"Condition<br/>met?"}
    COND -->|no| DROP["✅ reading stored,<br/>no action"]
    COND -->|yes| FIRE["🔥 trigger.fired"]
    FIRE --> GW

    GW --> VAL{"Validate<br/>permissions + payload"}
    VAL -->|❌ error| ERR["🚫 4xx<br/>+ audit (denied)"]
    VAL -->|✅ ok| CREATE["📝 TicketService<br/>.create()"]
    CREATE --> DB1[("💾 INSERT<br/>tickets")]
    CREATE --> AUD1[("📜 audit_log<br/>action=create")]
    CREATE --> PIPE["🔄 Pipeline Engine<br/>assign stage"]

    PIPE --> STAGE{"Pipeline<br/>stage"}
    STAGE -->|Detection| ST1["Required: description + photo"]
    STAGE -->|Classification| ST2["Required: category"]
    STAGE -->|Analysis| ST3["Required: root cause"]
    STAGE -->|Corrective action| ST4["Required: action description"]
    STAGE -->|Verification| ST5["Required: effectiveness"]
    STAGE -->|Closure| ST6["Required: signature (TOTP)"]

    ST1 --> RESP
    ST2 --> RESP
    ST3 --> RESP
    ST4 --> RESP
    ST5 --> RESP
    ST6 --> RESP["⚙️ Responder Dispatcher"]

    RESP --> R1["📧 notify_email"]
    RESP --> R2["📱 notify_sms"]
    RESP --> R3["🔔 notify_in_app (SSE)"]
    RESP --> R4["⏸️ pause_line<br/>(MQTT publish)"]
    RESP --> R5["⬆️ escalate"]
    RESP --> R6["🔗 webhook out"]

    R1 --> AUD2[("📜 audit_log<br/>action=respond")]
    R2 --> AUD2
    R3 --> AUD2
    R4 --> AUD2
    R5 --> AUD2
    R6 --> AUD2

    AUD2 --> DONE([Ticket open<br/>in pipeline])

    classDef source fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#000
    classDef gateway fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#000
    classDef storage fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000
    classDef stage fill:#dcfce7,stroke:#16a34a,stroke-width:1px,color:#000
    classDef responder fill:#ede9fe,stroke:#7c3aed,stroke-width:1px,color:#000
    classDef terminal fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#000

    class S1,S2,S3 source
    class GW,MB,TE,RESP,PIPE gateway
    class DB1,AUD1,AUD2,RS storage
    class ST1,ST2,ST3,ST4,ST5,ST6 stage
    class R1,R2,R3,R4,R5,R6 responder
    class ERR,DROP,DONE terminal
```

### 2.3. Path comparison (normal vs anomaly)

| Step | NORMAL path (operator) | ALARM path (IoT/anomaly) |
|---|---|---|
| 1. Trigger | Manual operator click | Trigger engine detects condition (e.g. T > 220°C / 30s) |
| 2. Auth | User session (Flask-Login) | Internal system event (no user, `created_by_system=true`) |
| 3. Classification | Chosen manually | Automatic from trigger definition |
| 4. Severity | Operator picks | From trigger definition |
| 5. Stage start | `Detection` (waits for QA classification) | `Classification` (already classified), notify QA |
| 6. SLA | Standard per stage | Shortened (`fast_track`) when `severity=critical` |
| 7. Responder | Only `notify_in_app` | `notify_sms` + `notify_email` + optionally `pause_line` |
| 8. Audit | `created_by_user_id=<op>` | `created_by_user_id=NULL`, `metadata.trigger_id=<id>` |

---

## Diagram 3 — Compliance module integration

**Role:** Shows the interplay between the SALSA, HACCP, CCP and Audit Trail modules and their integration points with the main ticket pipeline. Every action in these modules generates an audit_log entry; every nonconformity may create a ticket; every corrective action updates CCP/SALSA state.
**Relation:** The modules described here live in the business-logic layer of Diagram 1; tickets flow through them per Diagram 2; permissions (who may define/fill) follow Diagram 4.

```mermaid
graph TB
    subgraph CFG["⚙️ CONFIGURATION<br/>(Compliance Officer)"]
        CCP_DEF[("📋 ccp_definitions<br/>limits, frequency")]
        SALSA_TPL[("📋 salsa_checklists<br/>templates")]
        TRIG_DEF[("📋 triggers<br/>+ responders")]
    end

    subgraph OPS["🏭 OPERATIONS<br/>(operator + QA)"]
        CCP_MEAS[("🌡️ ccp_measurements<br/>live readings")]
        SALSA_RESP[("✅ salsa_responses<br/>submissions")]
        TICKETS[("🎫 tickets<br/>+ ticket_events")]
    end

    subgraph PIPE["🔄 TICKET PIPELINE"]
        STAGE_CCP{"Stage<br/>is_ccp_checkpoint?"}
        ACT_CORR["📝 Corrective action<br/>(CCP template)"]
        VERIFY["✓ Effectiveness<br/>verification"]
        CLOSE["🔒 Closure<br/>+ TOTP signature"]
    end

    subgraph AUD["📜 AUDIT TRAIL<br/>(append-only, chain hash)"]
        AUD_LOG[("audit_log<br/>monthly partitions<br/>7-year retention")]
        WORM["☁️ WORM Storage<br/>S3 Object Lock<br/>(replica)"]
    end

    subgraph REP["📊 REPORTING"]
        R_HACCP["📄 HACCP Report<br/>monthly PDF/A"]
        R_SALSA["📄 SALSA Report<br/>quarterly PDF/A"]
        R_FSA["📄 FSA Traceability<br/>ad-hoc, < 60s"]
        R_AUDIT["📄 Audit Trail Export<br/>signed CSV/PDF"]
    end

    %% Configuration → Operations
    CCP_DEF -->|enforces measurement<br/>on schedule| CCP_MEAS
    SALSA_TPL -->|generates<br/>to fill in| SALSA_RESP
    TRIG_DEF -->|activates<br/>on events| TICKETS

    %% CCP → Tickets
    CCP_MEAS -->|reading outside<br/>critical_limits| TICKETS
    CCP_MEAS -.flag<br/>is_ccp_related=true.-> TICKETS

    %% SALSA → Tickets
    SALSA_RESP -->|nonconformity<br/>detected| TICKETS

    %% Tickets → Pipeline
    TICKETS --> STAGE_CCP
    STAGE_CCP -->|yes| ACT_CORR
    STAGE_CCP -->|no| ACT_CORR
    ACT_CORR --> VERIFY
    VERIFY --> CLOSE
    CLOSE -.update.-> CCP_MEAS
    CLOSE -.update.-> SALSA_RESP

    %% Everything → Audit
    CCP_DEF -.every change.-> AUD_LOG
    SALSA_TPL -.every change.-> AUD_LOG
    TRIG_DEF -.every change.-> AUD_LOG
    CCP_MEAS -.every write.-> AUD_LOG
    SALSA_RESP -.every submission.-> AUD_LOG
    TICKETS -.every event.-> AUD_LOG
    STAGE_CCP -.state transition.-> AUD_LOG
    ACT_CORR -.action.-> AUD_LOG
    VERIFY -.verification.-> AUD_LOG
    CLOSE -.signature.-> AUD_LOG

    AUD_LOG -.daily replication.-> WORM

    %% Audit → Reports
    AUD_LOG -->|source| R_HACCP
    AUD_LOG -->|source| R_SALSA
    AUD_LOG -->|source| R_FSA
    AUD_LOG -->|source| R_AUDIT
    CCP_MEAS -->|data| R_HACCP
    SALSA_RESP -->|data| R_SALSA
    TICKETS -->|data| R_FSA

    classDef cfg fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#000
    classDef ops fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#000
    classDef pipe fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#000
    classDef aud fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000
    classDef rep fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#000

    class CCP_DEF,SALSA_TPL,TRIG_DEF cfg
    class CCP_MEAS,SALSA_RESP,TICKETS ops
    class STAGE_CCP,ACT_CORR,VERIFY,CLOSE pipe
    class AUD_LOG,WORM aud
    class R_HACCP,R_SALSA,R_FSA,R_AUDIT rep
```

### Compliance integration table

| Module | What it records | Triggers a ticket? | Audit log entry? | In which report |
|---|---|---|---|---|
| **HACCP / CCP** | Limit definitions, parameter readings | Yes — on deviation from critical limits | Yes — every definition change + every reading | HACCP Monthly, FSA Traceability |
| **SALSA** | Checklist templates, submitted responses | Yes — when a "nonconformity" is ticked | Yes — every submission + templates | SALSA Quarterly |
| **Triggers** | Automatic rules over metrics | Yes — automatically | Yes — every fire + definition change | Audit Trail Export |
| **Audit Trail** | Every action in the system | No | — (it is the log itself) | Audit Trail Export, all other reports |

### Tamper-evidence mechanism (audit_log)

```mermaid
graph LR
    R0["🔗 Rec #0<br/>checksum=hash('genesis')"]
    R1["🔗 Rec #1<br/>checksum=hash(R0+payload1)"]
    R2["🔗 Rec #2<br/>checksum=hash(R1+payload2)"]
    R3["🔗 Rec #3<br/>checksum=hash(R2+payload3)"]
    R0 --> R1 --> R2 --> R3
    VER["🔍 Verifier<br/>(daily cron + on-demand)"]
    R3 -.verification.-> VER
    VER --> OK{"Chain<br/>intact?"}
    OK -->|yes| GREEN["✅ OK"]
    OK -->|no| RED["🚨 ALERT<br/>to Compliance + Admin"]
```

---

## Diagram 4 — Permissions and roles

**Role:** Defines six functional roles, their capability set, and the access-control checkpoints in the system. Shows where authorisation is enforced (Flask middleware, view decorator, PostgreSQL RLS policy) and how the system resolves concurrent-edit conflicts (optimistic/pessimistic locking).
**Relation:** Enforced on every API call from Diagram 1; checked before each state transition in Diagram 2; special compliance permissions visible in Diagram 3.

### 4.1. Role and capability diagram

```mermaid
graph LR
    subgraph USERS["👥 USER ROLES"]
        OP["👷 Production<br/>Operator"]
        QA["🔬 QA<br/>Specialist"]
        LM["👔 Line<br/>Manager"]
        CO["📋 Compliance<br/>Officer"]
        PM["🏢 Plant<br/>Manager"]
        ADM["⚙️ Administrator"]
    end

    subgraph CAPS["🔑 CAPABILITY SETS"]
        C_CREATE["tickets.create"]
        C_CLASSIFY["tickets.classify"]
        C_ACTION["tickets.corrective_action"]
        C_CLOSE["tickets.close"]
        C_CCP_M["ccp.measure"]
        C_CCP_D["ccp.define"]
        C_SALSA_R["salsa.respond"]
        C_SALSA_D["salsa.define"]
        C_PIPE["pipeline.configure"]
        C_TRIG["triggers.define"]
        C_USER["users.manage"]
        C_AUDIT["audit.export"]
        C_REPORT["reports.generate"]
        C_KPI["dashboard.view"]
        C_SYS["system.configure"]
    end

    OP --> C_CREATE
    OP --> C_CCP_M
    OP --> C_SALSA_R
    OP --> C_KPI

    QA --> C_CREATE
    QA --> C_CLASSIFY
    QA --> C_ACTION
    QA --> C_CCP_M
    QA --> C_SALSA_R
    QA --> C_KPI
    QA --> C_REPORT

    LM --> C_CREATE
    LM --> C_CLASSIFY
    LM --> C_ACTION
    LM --> C_CLOSE
    LM --> C_CCP_M
    LM --> C_SALSA_R
    LM --> C_KPI
    LM --> C_REPORT

    CO --> C_CREATE
    CO --> C_CLASSIFY
    CO --> C_ACTION
    CO --> C_CLOSE
    CO --> C_CCP_M
    CO --> C_CCP_D
    CO --> C_SALSA_R
    CO --> C_SALSA_D
    CO --> C_PIPE
    CO --> C_TRIG
    CO --> C_AUDIT
    CO --> C_REPORT
    CO --> C_KPI

    PM --> C_KPI
    PM --> C_REPORT

    ADM --> C_CREATE
    ADM --> C_CLASSIFY
    ADM --> C_ACTION
    ADM --> C_CLOSE
    ADM --> C_CCP_M
    ADM --> C_CCP_D
    ADM --> C_SALSA_R
    ADM --> C_SALSA_D
    ADM --> C_PIPE
    ADM --> C_TRIG
    ADM --> C_USER
    ADM --> C_AUDIT
    ADM --> C_REPORT
    ADM --> C_KPI
    ADM --> C_SYS

    classDef user fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#000
    classDef cap fill:#fef3c7,stroke:#d97706,stroke-width:1px,color:#000

    class OP,QA,LM,CO,PM,ADM user
    class C_CREATE,C_CLASSIFY,C_ACTION,C_CLOSE,C_CCP_M,C_CCP_D,C_SALSA_R,C_SALSA_D,C_PIPE,C_TRIG,C_USER,C_AUDIT,C_REPORT,C_KPI,C_SYS cap
```

### 4.2. Access-control checkpoints

```mermaid
flowchart TB
    REQ([HTTP Request]) --> NGX["🔀 Nginx<br/>rate-limit per IP"]
    NGX --> CSRF{"CSRF token<br/>OK?"}
    CSRF -->|no| R1["🚫 403"]
    CSRF -->|yes| SESS{"Session<br/>valid?"}
    SESS -->|no| R2["🚫 401 → /login"]
    SESS -->|yes| RBAC{"@require_permission<br/>capability OK?"}
    RBAC -->|no| R3["🚫 403<br/>+ audit log (denied)"]
    RBAC -->|yes| SCOPE{"Scope OK?<br/>(line/plant)"}
    SCOPE -->|no| R3
    SCOPE -->|yes| RLS["💾 PostgreSQL RLS<br/>filter by tenant/line"]
    RLS --> EXEC["✅ Execute<br/>+ audit log"]

    classDef gate fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000
    classDef ok fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#000
    classDef bad fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#000

    class CSRF,SESS,RBAC,SCOPE gate
    class EXEC,RLS ok
    class R1,R2,R3 bad
```

### 4.3. Multi-user — concurrency control

```mermaid
sequenceDiagram
    actor U1 as User A (QA)
    actor U2 as User B (Line Manager)
    participant API as Flask API
    participant DB as PostgreSQL

    Note over U1,U2: Both editing the same ticket #42

    U1->>API: GET /tickets/42
    API->>DB: SELECT (version=7)
    API-->>U1: ticket v7

    U2->>API: GET /tickets/42
    API->>DB: SELECT (version=7)
    API-->>U2: ticket v7

    U1->>API: PATCH /tickets/42<br/>If-Match: 7
    API->>DB: UPDATE WHERE version=7<br/>SET version=8
    DB-->>API: 1 row updated
    API-->>U1: 200 v8

    U2->>API: PATCH /tickets/42<br/>If-Match: 7
    API->>DB: UPDATE WHERE version=7
    DB-->>API: 0 rows (stale)
    API-->>U2: 409 Conflict<br/>Reload required

    Note over U2: UI: "Ticket changed<br/>by User A.<br/>[Reload] [Force]"
```

### 4.4. RBAC summary table

| Capability | Operator | QA | Line Mgr | Compliance | Plant Mgr | Admin |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `tickets.create` | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| `tickets.classify` | — | ✅ | ✅ | ✅ | — | ✅ |
| `tickets.corrective_action` | — | ✅ | ✅ | ✅ | — | ✅ |
| `tickets.close` | — | — | ✅ | ✅ | — | ✅ |
| `ccp.measure` | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| `ccp.define` | — | — | — | ✅ | — | ✅ |
| `salsa.respond` | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| `salsa.define` | — | — | — | ✅ | — | ✅ |
| `pipeline.configure` | — | — | — | ✅ | — | ✅ |
| `triggers.define` | — | — | — | ✅ | — | ✅ |
| `users.manage` | — | — | — | — | — | ✅ |
| `audit.export` | — | — | — | ✅ | — | ✅ |
| `reports.generate` | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| `dashboard.view` | scope=line | scope=line | scope=line | global | global | global |
| `system.configure` | — | — | — | — | — | ✅ |

---

## Diagram 5 — Multilingual flow (PL/EN)

**Role:** Shows the complete language lifecycle — from detecting the user's preference, through loading static translations (Babel) and dynamic ones (JSONB in the database), to UI rendering, PDF report generation, and audit_log entries that retain the original description language.
**Relation:** i18n is a cross-cutting feature — touches every layer of Diagram 1, every action of Diagram 2 (messages), every report of Diagram 3, and every screen seen by roles in Diagram 4.

### 5.1. Language detection and switching

```mermaid
flowchart TB
    REQ([HTTP Request]) --> M1{"User logged in?"}
    M1 -->|yes| U_PREF["🔍 user.language<br/>from DB"]
    M1 -->|no| COOKIE{"Cookie<br/>'lang' set?"}
    COOKIE -->|yes| C_LANG["🍪 cookie value"]
    COOKIE -->|no| HEADER{"Accept-Language<br/>contains 'pl'?"}
    HEADER -->|yes| PL_DEF["pl"]
    HEADER -->|no| EN_DEF["en (fallback)"]

    U_PREF --> CTX["📌 g.lang<br/>in request context"]
    C_LANG --> CTX
    PL_DEF --> CTX
    EN_DEF --> CTX

    CTX --> RENDER["🎨 Render"]

    RENDER --> STATIC["📜 Static strings<br/>from .po/.mo<br/>(Flask-Babel<br/>gettext)"]
    RENDER --> DYNAMIC["💾 Dynamic fields<br/>from JSONB<br/>(name['pl'/'en'])"]
    RENDER --> DT["📅 Dates + numbers<br/>(Babel locale<br/>format)"]

    STATIC --> RESP[/"📤 HTML response<br/>Content-Language: <lang>"/]
    DYNAMIC --> RESP
    DT --> RESP

    SWITCH["👆 PL/EN flag<br/>click"] -.->|POST /lang/<code>| SET["💾 Save:<br/>cookie + user.language"]
    SET -.refresh.-> REQ

    classDef detect fill:#fef3c7,stroke:#d97706,stroke-width:1px,color:#000
    classDef ctx fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#000
    classDef render fill:#dcfce7,stroke:#16a34a,stroke-width:1px,color:#000
    classDef out fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#000

    class M1,COOKIE,HEADER detect
    class CTX,U_PREF,C_LANG,PL_DEF,EN_DEF ctx
    class STATIC,DYNAMIC,DT,RENDER render
    class RESP,SET,SWITCH out
```

### 5.2. Where translations live

```mermaid
graph TB
    subgraph CODE["📦 APPLICATION CODE"]
        PO_PL["📄 app/translations/pl/<br/>LC_MESSAGES/messages.po"]
        PO_EN["📄 app/translations/en/<br/>LC_MESSAGES/messages.po"]
        TPL["🎨 Jinja2 templates<br/>{{ _('Save') }}"]
    end

    subgraph DB_T["💾 DB — dynamic fields"]
        PIPE_NAME["pipeline_stages.name<br/>JSONB:<br/>{pl:'Analiza', en:'Analysis'}"]
        TRIG_NAME["triggers.name<br/>JSONB"]
        CCP_NAME["ccp_definitions.name<br/>JSONB"]
        CAT_NAME["ticket_categories.name<br/>JSONB"]
    end

    subgraph DB_T2["💾 DB — user content"]
        TICKET["tickets.description<br/>+ description_lang<br/>(original language)"]
        COMMENT["ticket_events.comment<br/>+ comment_lang"]
    end

    subgraph ADMIN["⚙️ ADMIN PANEL"]
        EDIT_STATIC["Message-catalog editor<br/>(.po override at runtime<br/>'translations' table)"]
        EDIT_DYN["Forms with PL and EN<br/>fields side by side"]
    end

    subgraph OUT["📤 OUTPUT"]
        UI["🖥️ HTML UI"]
        PDF["📄 PDF reports<br/>?lang=<code>"]
        EMAIL["📧 E-mails<br/>per recipient.language"]
        AUDIT["📜 audit_log<br/>original + lang flag"]
    end

    PO_PL --> TPL
    PO_EN --> TPL
    TPL --> UI

    PIPE_NAME --> UI
    TRIG_NAME --> UI
    CCP_NAME --> UI
    CAT_NAME --> UI

    TICKET -.preserves<br/>original language.- AUDIT
    COMMENT -.preserves<br/>original language.- AUDIT

    EDIT_STATIC -->|override| PO_PL
    EDIT_STATIC -->|override| PO_EN
    EDIT_DYN -->|UPDATE| PIPE_NAME
    EDIT_DYN -->|UPDATE| TRIG_NAME
    EDIT_DYN -->|UPDATE| CCP_NAME

    UI --> PDF
    UI --> EMAIL

    classDef code fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#000
    classDef db fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000
    classDef admin fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#000
    classDef out fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#000

    class PO_PL,PO_EN,TPL code
    class PIPE_NAME,TRIG_NAME,CCP_NAME,CAT_NAME,TICKET,COMMENT db
    class EDIT_STATIC,EDIT_DYN admin
    class UI,PDF,EMAIL,AUDIT out
```

### 5.3. Translations in reports and audit

| Element | Render language | Reasoning |
|---|---|---|
| **User UI** | `g.lang` (preference) | Working comfort |
| **Monthly HACCP report** | `?lang=` query param, EN by default for FSA | FSA prefers EN, PL on request |
| **SALSA report** | EN | The SALSA standard is English-language |
| **E-mail notifications** | `recipient.language` | Per recipient |
| **SMS** | `recipient.language` | Per recipient |
| **audit_log.diff** | Original (input language) + `lang` flag | Immutability outweighs translation; UI may translate on demand via translation API (optional) |
| **Audit trail export** | EN (with PL option) | External audits typically run in EN |
| **Per-batch traceability PDF** | EN (FSA) | Regulatory standard |

### 5.4. Code-level integration points (examples)

```python
# ── Language detection (Flask-Babel hook) ────────────────
@babel.localeselector
def select_locale():
    if current_user.is_authenticated and current_user.language:
        return current_user.language
    if 'lang' in request.cookies:
        return request.cookies['lang']
    return request.accept_languages.best_match(['pl', 'en']) or 'en'

# ── Rendering dynamic fields (Jinja2 filter) ─────────────
@app.template_filter('i18n')
def i18n_filter(jsonb_field):
    lang = g.get('lang', 'en')
    return jsonb_field.get(lang) or jsonb_field.get('en') or '—'

# Used in a template:
#   {{ stage.name | i18n }}

# ── Exporting a report with language parameter ──────────
@reports_bp.route('/haccp/monthly.pdf')
def haccp_monthly_pdf():
    lang = request.args.get('lang', 'en')
    with force_locale(lang):
        html = render_template('reports/haccp_monthly.html', ...)
        return weasyprint.HTML(string=html).write_pdf()
```

---

## Appendix — Mapping diagrams to code locations

| Diagram | Components | Suggested locations |
|---|---|---|
| 1 — Layered | The whole structure | `app/__init__.py`, `app/blueprints/`, `docker-compose.yml`, `nginx/` |
| 2 — Tickets | Pipeline, triggers | `app/services/ticket_service.py`, `app/services/trigger_engine.py`, `app/workers/responder_dispatcher.py` |
| 3 — Compliance | HACCP, SALSA, audit | `app/blueprints/haccp/`, `app/blueprints/salsa/`, `app/services/audit.py` |
| 4 — RBAC | Auth | `app/auth/decorators.py`, `app/auth/permissions.py`, `migrations/versions/xxxx_seed_roles.py` |
| 5 — i18n | Babel + JSONB | `babel.cfg`, `app/translations/`, `app/utils/i18n.py`, `app/templates/_partials/lang_switcher.html` |

---

## Next steps

1. **Diagram validation** with the architect and Compliance Officer before Phase 1 starts.
2. **MQTT scheme agreement** with the device vendor (topic taxonomy + payload schema).
3. **Repository setup** including `pyproject.toml` (UV), `Dockerfile`, `docker-compose.yml`.
4. **First Alembic migration** with the tables from section 4 of `01-architectural-functional-plan.md`.
5. **Flask blueprint skeleton** matching the modules from section 2.

---

*Document prepared by the team: System Architect, Python Developer, Food Compliance Specialist (UK), UX/UI Designer.*
