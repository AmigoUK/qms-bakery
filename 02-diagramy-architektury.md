# Diagramy architektury
## System Zarządzania Jakością (QMS) — Piekarnia UK

> **Cel dokumentu:** Diagramy gotowe do bezpośredniej implementacji przez zespół deweloperski. Notacja: Mermaid (renderowanie natywne w GitHub/GitLab/VS Code) + tabele opisowe.
> **Powiązanie:** Stanowi uzupełnienie dokumentu `01-plan-architektoniczny-funkcjonalny.md`.
> **Wersja:** 1.0 — 2026-04-28

---

## Spis diagramów

1. [Architektura warstwowa](#diagram-1--architektura-warstwowa)
2. [Przepływ danych ticketów](#diagram-2--przepływ-danych-ticketów)
3. [Integracja modułów compliance](#diagram-3--integracja-modułów-compliance)
4. [System uprawnień i ról](#diagram-4--system-uprawnień-i-ról)
5. [Przepływ wielojęzyczności (PL/EN)](#diagram-5--przepływ-wielojęzyczności-plen)

---

## Diagram 1 — Architektura warstwowa

**Rola:** Pokazuje trójwarstwowy podział systemu (prezentacja / logika biznesowa / dane) wraz z protokołami komunikacji między warstwami oraz punktami integracji z systemami zewnętrznymi (urządzenia IoT, ERP, e-mail/SMS).
**Powiązanie:** Stanowi punkt wyjścia dla wszystkich pozostałych diagramów. Diagram 2 detalizuje przepływ danych w obrębie warstwy logiki biznesowej, Diagram 3 pokazuje moduły compliance umieszczone w warstwie aplikacyjnej, Diagramy 4 i 5 opisują przekrojowe mechanizmy (auth, i18n) dotykające wszystkich trzech warstw.

```mermaid
graph TB
    subgraph EXT["🌍 ŚRODOWISKO ZEWNĘTRZNE"]
        IOT["🌡️ Urządzenia IoT<br/>(piece, wagi, czujniki)"]
        ERP["🏭 ERP / Systemy klienta"]
        EMAIL["📧 SMTP Gateway"]
        SMS["📱 SMS Gateway (Twilio)"]
        S3["☁️ Object Storage<br/>(WORM Audit)"]
    end

    subgraph PRES["📺 WARSTWA PREZENTACJI"]
        BROWSER["🖥️ Browser desktop<br/>(QA, manager, admin)"]
        PWA["📱 PWA tablet<br/>(operator hali)"]
        STATIC["Statyki: HTML/CSS/JS<br/>HTMX + Web Components<br/>Chart.js"]
    end

    subgraph BIZ["⚙️ WARSTWA LOGIKI BIZNESOWEJ"]
        NGINX["🔀 Nginx<br/>TLS, rate-limit, statyki"]
        FLASK["🐍 Flask App (gunicorn)<br/>Blueprinty: auth, tickets,<br/>pipeline, triggers, admin"]
        WORKER["⚡ RQ Worker<br/>Async jobs<br/>(respondery, raporty PDF)"]
        MQTTBR["📡 MQTT Bridge<br/>(paho-mqtt)"]
        BABEL["🌐 Flask-Babel<br/>(i18n PL/EN)"]
        AUTH["🔐 Flask-Login + RBAC"]
    end

    subgraph DATA["💾 WARSTWA DANYCH"]
        PG[("🐘 PostgreSQL 16<br/>tickets, pipelines,<br/>CCP, SALSA,<br/>audit_log")]
        REDIS[("⚡ Redis 7<br/>sesje, cache,<br/>kolejka RQ,<br/>streams IoT")]
        MOSQ["🦟 Mosquitto<br/>(MQTT broker)"]
        FILES["📁 Wolumen załączników<br/>(zdjęcia, PDF)"]
    end

    BROWSER -->|HTTPS<br/>REST + SSE| NGINX
    PWA -->|HTTPS<br/>REST + SSE| NGINX
    STATIC -.serwowane przez.- NGINX

    NGINX -->|WSGI<br/>uvicorn| FLASK
    FLASK <--> AUTH
    FLASK <--> BABEL
    FLASK -->|enqueue| REDIS
    REDIS -->|dequeue| WORKER
    WORKER -->|SQLAlchemy| PG
    FLASK -->|SQLAlchemy 2.0<br/>+ Alembic| PG
    FLASK -->|cache + sesja| REDIS

    IOT -->|MQTT QoS 1| MOSQ
    MOSQ -->|subscribe<br/>factory/+/+/+| MQTTBR
    MQTTBR -->|XADD| REDIS
    REDIS -->|XREAD stream| WORKER
    WORKER -->|trigger fire| FLASK

    ERP <-->|REST API + HMAC| NGINX
    WORKER -->|SMTP| EMAIL
    WORKER -->|HTTP API| SMS
    WORKER -->|"S3 PUT (Object Lock)"| S3
    PG -.replikacja audit.- S3

    FLASK -->|read/write| FILES
    WORKER -->|raport PDF| FILES

    classDef external fill:#fff4e6,stroke:#f59e0b,stroke-width:2px,color:#000
    classDef presentation fill:#e0f2fe,stroke:#0284c7,stroke-width:2px,color:#000
    classDef business fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#000
    classDef data fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000

    class IOT,ERP,EMAIL,SMS,S3 external
    class BROWSER,PWA,STATIC presentation
    class NGINX,FLASK,WORKER,MQTTBR,BABEL,AUTH business
    class PG,REDIS,MOSQ,FILES data
```

### Notatki implementacyjne

| Punkt | Konfiguracja |
|---|---|
| Nginx → Flask | `proxy_pass http://gunicorn:8000;` + `proxy_set_header X-Forwarded-For ...` |
| Gunicorn | 4 workery `uvicorn.workers.UvicornWorker`, timeout 30s, graceful restart |
| SSE | Endpoint `/events/stream` per użytkownik, Last-Event-ID dla resume |
| MQTT topic schema | `factory/<line_id>/<device_id>/<metric>` (lower_snake) |
| Redis Stream key | `metrics:<line_id>` z MAXLEN ~ 100000 |
| RQ queues | `default`, `notifications`, `reports` (priorytety) |

---

## Diagram 2 — Przepływ danych ticketów

**Rola:** Prezentuje pełną drogę pojedynczego ticketu od źródła (manualne / IoT / API) przez silnik triggerów i responderów aż po notyfikacje, aktualizacje stanu i audit trail. Pokazuje punkty decyzyjne pipeline'u oraz różnice między ścieżką normalną (ticket od operatora) a ścieżką alarmową (ticket auto-generowany z anomalii IoT).
**Powiązanie:** Detalizuje warstwę logiki biznesowej z Diagramu 1; integruje się z Diagramem 3 w punktach „pomiar CCP" i „audit log"; uprawnienia weryfikowane na każdym kroku zgodnie z Diagramem 4.

### 2.1. Diagram sekwencyjny — ścieżka manualna

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

    OP->>API: POST /tickets {linia, kat, sev, foto}
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
    START([Wystąpienie zdarzenia])

    subgraph SOURCES["3 ŹRÓDŁA"]
        S1["👷 Operator<br/>w PWA"]
        S2["🌡️ Urządzenie IoT<br/>publikuje na MQTT"]
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
    TE --> COND{"Warunek<br/>spełniony?"}
    COND -->|nie| DROP["✅ pomiar zapisany,<br/>brak akcji"]
    COND -->|tak| FIRE["🔥 trigger.fired"]
    FIRE --> GW

    GW --> VAL{"Walidacja<br/>uprawnień + payload"}
    VAL -->|❌ błąd| ERR["🚫 4xx<br/>+ audit (denied)"]
    VAL -->|✅ ok| CREATE["📝 TicketService<br/>.create()"]
    CREATE --> DB1[("💾 INSERT<br/>tickets")]
    CREATE --> AUD1[("📜 audit_log<br/>action=create")]
    CREATE --> PIPE["🔄 Pipeline Engine<br/>assign stage"]

    PIPE --> STAGE{"Etap<br/>pipeline'u"}
    STAGE -->|Wykrycie| ST1["Wymóg: opis + foto"]
    STAGE -->|Klasyfikacja| ST2["Wymóg: kategoria"]
    STAGE -->|Analiza| ST3["Wymóg: root cause"]
    STAGE -->|Akcja korygująca| ST4["Wymóg: opis działania"]
    STAGE -->|Weryfikacja| ST5["Wymóg: skuteczność"]
    STAGE -->|Zamknięcie| ST6["Wymóg: podpis (TOTP)"]

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

    AUD2 --> DONE([Ticket otwarty<br/>w pipeline])

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

### 2.3. Tabela ścieżek (normal vs anomalia)

| Krok | Ścieżka NORMALNA (operator) | Ścieżka ALARMOWA (IoT/anomalia) |
|---|---|---|
| 1. Trigger | Manualne kliknięcie operatora | Trigger engine wykrywa warunek (np. T > 220°C / 30s) |
| 2. Auth | Sesja użytkownika (Flask-Login) | Wewnętrzny system event (no user, `created_by_system=true`) |
| 3. Klasyfikacja | Wybierana ręcznie | Automatyczna z definicji triggera |
| 4. Severity | Operator wybiera | Z definicji triggera |
| 5. Stage start | `Wykrycie` (czeka na klasyfikację QA) | `Klasyfikacja` (już sklasyfikowane), notify QA |
| 6. SLA | Standardowe per stage | Skrócone (`fast_track`) jeśli `severity=critical` |
| 7. Responder | Tylko `notify_in_app` | `notify_sms` + `notify_email` + opcjonalnie `pause_line` |
| 8. Audit | `created_by_user_id=<op>` | `created_by_user_id=NULL`, `metadata.trigger_id=<id>` |

---

## Diagram 3 — Integracja modułów compliance

**Rola:** Pokazuje współzależności między modułami SALSA, HACCP, CCP i Audit Trail oraz ich punkty integracji z głównym pipeline ticketów. Każda akcja w tych modułach generuje wpis w audit_log; każda niezgodność może utworzyć ticket; każda akcja korygująca aktualizuje stan CCP/SALSA.
**Powiązanie:** Moduły opisane tu są realizowane w warstwie logiki biznesowej z Diagramu 1; tickety przepływają przez nie zgodnie z Diagramem 2; uprawnienia (kto może definiować/wypełniać) zgodne z Diagramem 4.

```mermaid
graph TB
    subgraph CFG["⚙️ KONFIGURACJA<br/>(Compliance Officer)"]
        CCP_DEF[("📋 ccp_definitions<br/>limity, częstotliwość")]
        SALSA_TPL[("📋 salsa_checklists<br/>szablony")]
        TRIG_DEF[("📋 triggers<br/>+ responders")]
    end

    subgraph OPS["🏭 OPERACJE<br/>(operator + QA)"]
        CCP_MEAS[("🌡️ ccp_measurements<br/>pomiary live")]
        SALSA_RESP[("✅ salsa_responses<br/>wypełnienia")]
        TICKETS[("🎫 tickets<br/>+ ticket_events")]
    end

    subgraph PIPE["🔄 PIPELINE TICKETU"]
        STAGE_CCP{"Etap<br/>is_ccp_checkpoint?"}
        ACT_CORR["📝 Akcja korygująca<br/>(template z CCP)"]
        VERIFY["✓ Weryfikacja<br/>skuteczności"]
        CLOSE["🔒 Zamknięcie<br/>+ podpis TOTP"]
    end

    subgraph AUD["📜 AUDIT TRAIL<br/>(append-only, chain hash)"]
        AUD_LOG[("audit_log<br/>partycje miesięczne<br/>retencja 7 lat")]
        WORM["☁️ WORM Storage<br/>S3 Object Lock<br/>(replika)"]
    end

    subgraph REP["📊 RAPORTOWANIE"]
        R_HACCP["📄 HACCP Report<br/>miesięczny PDF/A"]
        R_SALSA["📄 SALSA Report<br/>kwartalny PDF/A"]
        R_FSA["📄 FSA Traceability<br/>ad-hoc, < 60s"]
        R_AUDIT["📄 Audit Trail Export<br/>CSV/PDF z podpisem"]
    end

    %% Konfiguracja → Operacje
    CCP_DEF -->|wymusza pomiar<br/>w cyklu| CCP_MEAS
    SALSA_TPL -->|generuje<br/>do wypełnienia| SALSA_RESP
    TRIG_DEF -->|aktywuje<br/>na zdarzenia| TICKETS

    %% CCP → Tickety
    CCP_MEAS -->|wartość poza<br/>critical_limits| TICKETS
    CCP_MEAS -.flag<br/>is_ccp_related=true.-> TICKETS

    %% SALSA → Tickety
    SALSA_RESP -->|wykryta<br/>nieprawidłowość| TICKETS

    %% Tickety → Pipeline
    TICKETS --> STAGE_CCP
    STAGE_CCP -->|tak| ACT_CORR
    STAGE_CCP -->|nie| ACT_CORR
    ACT_CORR --> VERIFY
    VERIFY --> CLOSE
    CLOSE -.update.-> CCP_MEAS
    CLOSE -.update.-> SALSA_RESP

    %% Wszystko → Audit
    CCP_DEF -.każda zmiana.-> AUD_LOG
    SALSA_TPL -.każda zmiana.-> AUD_LOG
    TRIG_DEF -.każda zmiana.-> AUD_LOG
    CCP_MEAS -.każdy zapis.-> AUD_LOG
    SALSA_RESP -.każde wypełnienie.-> AUD_LOG
    TICKETS -.każde zdarzenie.-> AUD_LOG
    STAGE_CCP -.przejście stanu.-> AUD_LOG
    ACT_CORR -.akcja.-> AUD_LOG
    VERIFY -.weryfikacja.-> AUD_LOG
    CLOSE -.podpis.-> AUD_LOG

    AUD_LOG -.codzienna replikacja.-> WORM

    %% Audit → Raporty
    AUD_LOG -->|źródło| R_HACCP
    AUD_LOG -->|źródło| R_SALSA
    AUD_LOG -->|źródło| R_FSA
    AUD_LOG -->|źródło| R_AUDIT
    CCP_MEAS -->|dane| R_HACCP
    SALSA_RESP -->|dane| R_SALSA
    TICKETS -->|dane| R_FSA

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

### Tabela powiązań compliance

| Moduł | Co rejestruje | Wyzwala ticket? | Wpis w audit_log? | W raporcie |
|---|---|---|---|---|
| **HACCP / CCP** | Definicje limitów, pomiary parametrów | Tak — przy odchyleniu od limitów krytycznych | Tak — każda zmiana definicji + każdy pomiar | HACCP Monthly, FSA Traceability |
| **SALSA** | Szablony checklist, odpowiedzi z odpowiedziami | Tak — przy zaznaczeniu „nieprawidłowość" | Tak — każde wypełnienie + szablony | SALSA Quarterly |
| **Triggers** | Reguły automatyczne na metryki | Tak — automatycznie | Tak — każde uruchomienie + zmiana definicji | Audit Trail Export |
| **Audit Trail** | Każda akcja w systemie | Nie | — (sam jest logiem) | Audit Trail Export, wszystkie inne raporty |

### Mechanizm tamper-evidence (audit_log)

```mermaid
graph LR
    R0["🔗 Rec #0<br/>checksum=hash('genesis')"]
    R1["🔗 Rec #1<br/>checksum=hash(R0+payload1)"]
    R2["🔗 Rec #2<br/>checksum=hash(R1+payload2)"]
    R3["🔗 Rec #3<br/>checksum=hash(R2+payload3)"]
    R0 --> R1 --> R2 --> R3
    VER["🔍 Verifier<br/>(cron daily + on-demand)"]
    R3 -.weryfikacja.-> VER
    VER --> OK{"Łańcuch<br/>spójny?"}
    OK -->|tak| GREEN["✅ OK"]
    OK -->|nie| RED["🚨 ALARM<br/>do Compliance + Admin"]
```

---

## Diagram 4 — System uprawnień i ról

**Rola:** Definiuje sześć ról funkcjonalnych, ich uprawnienia (capability set) i punkty kontroli dostępu w systemie. Pokazuje, gdzie autoryzacja jest egzekwowana (middleware Flask, decorator widoku, polityka RLS w PostgreSQL) oraz jak system rozwiązuje konflikty wielu jednoczesnych użytkowników (optimistic/pessimistic locking).
**Powiązanie:** Egzekwowane na każdym wywołaniu API z Diagramu 1; weryfikacja przed każdym przejściem stanu w Diagramie 2; specjalne uprawnienia compliance widoczne w Diagramie 3.

### 4.1. Diagram ról i uprawnień

```mermaid
graph LR
    subgraph USERS["👥 ROLE UŻYTKOWNIKÓW"]
        OP["👷 Operator<br/>produkcji"]
        QA["🔬 QA<br/>Specialist"]
        LM["👔 Line<br/>Manager"]
        CO["📋 Compliance<br/>Officer"]
        PM["🏢 Plant<br/>Manager"]
        ADM["⚙️ Administrator"]
    end

    subgraph CAPS["🔑 ZESTAWY UPRAWNIEŃ"]
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

### 4.2. Punkty kontroli dostępu

```mermaid
flowchart TB
    REQ([HTTP Request]) --> NGX["🔀 Nginx<br/>rate-limit per IP"]
    NGX --> CSRF{"CSRF token<br/>OK?"}
    CSRF -->|nie| R1["🚫 403"]
    CSRF -->|tak| SESS{"Sesja<br/>ważna?"}
    SESS -->|nie| R2["🚫 401 → /login"]
    SESS -->|tak| RBAC{"@require_permission<br/>capability OK?"}
    RBAC -->|nie| R3["🚫 403<br/>+ audit log (denied)"]
    RBAC -->|tak| SCOPE{"Scope OK?<br/>(linia/zakład)"}
    SCOPE -->|nie| R3
    SCOPE -->|tak| RLS["💾 PostgreSQL RLS<br/>filtr po tenant/linia"]
    RLS --> EXEC["✅ Wykonanie<br/>+ audit log"]

    classDef gate fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#000
    classDef ok fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#000
    classDef bad fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#000

    class CSRF,SESS,RBAC,SCOPE gate
    class EXEC,RLS ok
    class R1,R2,R3 bad
```

### 4.3. Multi-user — kontrola konkurencji

```mermaid
sequenceDiagram
    actor U1 as User A (QA)
    actor U2 as User B (Line Manager)
    participant API as Flask API
    participant DB as PostgreSQL

    Note over U1,U2: Obaj edytują ten sam ticket #42

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

    Note over U2: UI: "Ticket zmieniony<br/>przez User A.<br/>[Odśwież] [Wymuś]"
```

### 4.4. Tabela RBAC w skrócie

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
| `dashboard.view` | scope=linia | scope=linia | scope=linia | global | global | global |
| `system.configure` | — | — | — | — | — | ✅ |

---

## Diagram 5 — Przepływ wielojęzyczności (PL/EN)

**Rola:** Pokazuje pełny cykl obsługi języka — od detekcji preferencji użytkownika, przez ładowanie tłumaczeń statycznych (Babel) i dynamicznych (JSONB w bazie), aż po renderowanie UI, generowanie raportów PDF i logowanie zdarzeń w audit_log z zachowaniem oryginalnego języka opisu.
**Powiązanie:** i18n jest cechą przekrojową — dotyka wszystkich warstw z Diagramu 1, każdej akcji z Diagramu 2 (komunikaty), każdego raportu z Diagramu 3 i każdego ekranu z perspektywy ról z Diagramu 4.

### 5.1. Detekcja i przełączanie języka

```mermaid
flowchart TB
    REQ([HTTP Request]) --> M1{"User zalogowany?"}
    M1 -->|tak| U_PREF["🔍 user.language<br/>z bazy"]
    M1 -->|nie| COOKIE{"Cookie<br/>'lang' set?"}
    COOKIE -->|tak| C_LANG["🍪 cookie value"]
    COOKIE -->|nie| HEADER{"Accept-Language<br/>zawiera 'pl'?"}
    HEADER -->|tak| PL_DEF["pl"]
    HEADER -->|nie| EN_DEF["en (fallback)"]

    U_PREF --> CTX["📌 g.lang<br/>w request context"]
    C_LANG --> CTX
    PL_DEF --> CTX
    EN_DEF --> CTX

    CTX --> RENDER["🎨 Render"]

    RENDER --> STATIC["📜 Statyczne stringi<br/>z .po/.mo<br/>(Flask-Babel<br/>gettext)"]
    RENDER --> DYNAMIC["💾 Dynamiczne pola<br/>z JSONB<br/>(name['pl'/'en'])"]
    RENDER --> DT["📅 Daty + liczby<br/>(Babel locale<br/>format)"]

    STATIC --> RESP[/"📤 HTML response<br/>Content-Language: <lang>"/]
    DYNAMIC --> RESP
    DT --> RESP

    SWITCH["👆 Kliknięcie<br/>flagi PL/EN"] -.->|POST /lang/<code>| SET["💾 Zapis:<br/>cookie + user.language"]
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

### 5.2. Lokalizacja danych — gdzie żyją tłumaczenia

```mermaid
graph TB
    subgraph CODE["📦 KOD APLIKACJI"]
        PO_PL["📄 app/translations/pl/<br/>LC_MESSAGES/messages.po"]
        PO_EN["📄 app/translations/en/<br/>LC_MESSAGES/messages.po"]
        TPL["🎨 Templates Jinja2<br/>{{ _('Save') }}"]
    end

    subgraph DB_T["💾 BAZA — pola dynamiczne"]
        PIPE_NAME["pipeline_stages.name<br/>JSONB:<br/>{pl:'Analiza', en:'Analysis'}"]
        TRIG_NAME["triggers.name<br/>JSONB"]
        CCP_NAME["ccp_definitions.name<br/>JSONB"]
        CAT_NAME["ticket_categories.name<br/>JSONB"]
    end

    subgraph DB_T2["💾 BAZA — treść użytkownika"]
        TICKET["tickets.description<br/>+ description_lang<br/>(język oryginalny)"]
        COMMENT["ticket_events.comment<br/>+ comment_lang"]
    end

    subgraph ADMIN["⚙️ PANEL ADMINA"]
        EDIT_STATIC["Edytor message catalog<br/>(override .po w runtime<br/>tabela 'translations')"]
        EDIT_DYN["Formularze z polami<br/>PL i EN obok siebie"]
    end

    subgraph OUT["📤 OUTPUT"]
        UI["🖥️ UI HTML"]
        PDF["📄 Raporty PDF<br/>?lang=<code>"]
        EMAIL["📧 E-maile<br/>per recipient.language"]
        AUDIT["📜 audit_log<br/>oryginał + flag lang"]
    end

    PO_PL --> TPL
    PO_EN --> TPL
    TPL --> UI

    PIPE_NAME --> UI
    TRIG_NAME --> UI
    CCP_NAME --> UI
    CAT_NAME --> UI

    TICKET -.zachowuje<br/>oryginalny język.- AUDIT
    COMMENT -.zachowuje<br/>oryginalny język.- AUDIT

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

### 5.3. Tłumaczenia w raportach i audycie

| Element | Język renderowania | Uzasadnienie |
|---|---|---|
| **UI dla użytkownika** | `g.lang` (preferencja) | Komfort pracy |
| **Raport HACCP miesięczny** | `?lang=` query param, domyślnie EN dla FSA | FSA preferuje EN, ale można generować PL na życzenie |
| **Raport SALSA** | EN | Standard SALSA jest anglojęzyczny |
| **E-mail powiadomienia** | `recipient.language` | Per odbiorca |
| **SMS** | `recipient.language` | Per odbiorca |
| **audit_log.diff** | Oryginał (język wprowadzenia) + flaga `lang` | Niezmienność jest ważniejsza od tłumaczenia; UI w razie potrzeby tłumaczy on-demand przez API tłumaczeń (opcjonalnie) |
| **Eksport audit trail** | EN (z opcją PL) | Audyt zewnętrzny zazwyczaj EN |
| **PDF traceability per batch** | EN (FSA) | Standard regulacyjny |

### 5.4. Implementacja w kodzie (przykładowe punkty integracji)

```python
# ── Detekcja języka (Flask-Babel hook) ───────────────────
@babel.localeselector
def select_locale():
    if current_user.is_authenticated and current_user.language:
        return current_user.language
    if 'lang' in request.cookies:
        return request.cookies['lang']
    return request.accept_languages.best_match(['pl', 'en']) or 'en'

# ── Renderowanie pól dynamicznych (Jinja2 filter) ────────
@app.template_filter('i18n')
def i18n_filter(jsonb_field):
    lang = g.get('lang', 'en')
    return jsonb_field.get(lang) or jsonb_field.get('en') or '—'

# Użycie w szablonie:
#   {{ stage.name | i18n }}

# ── Eksport raportu z parametrem języka ──────────────────
@reports_bp.route('/haccp/monthly.pdf')
def haccp_monthly_pdf():
    lang = request.args.get('lang', 'en')
    with force_locale(lang):
        html = render_template('reports/haccp_monthly.html', ...)
        return weasyprint.HTML(string=html).write_pdf()
```

---

## Załącznik — Mapowanie diagramów na pliki kodu

| Diagram | Komponenty | Sugerowane lokalizacje |
|---|---|---|
| 1 — Warstwowa | Cała struktura | `app/__init__.py`, `app/blueprints/`, `docker-compose.yml`, `nginx/` |
| 2 — Tickety | Pipeline, triggery | `app/services/ticket_service.py`, `app/services/trigger_engine.py`, `app/workers/responder_dispatcher.py` |
| 3 — Compliance | HACCP, SALSA, audit | `app/blueprints/haccp/`, `app/blueprints/salsa/`, `app/services/audit.py` |
| 4 — RBAC | Auth | `app/auth/decorators.py`, `app/auth/permissions.py`, `migrations/versions/xxxx_seed_roles.py` |
| 5 — i18n | Babel + JSONB | `babel.cfg`, `app/translations/`, `app/utils/i18n.py`, `app/templates/_partials/lang_switcher.html` |

---

## Następne kroki

1. **Walidacja diagramów** z architektem i Compliance Officerem przed startem Fazy 1.
2. **Ustalenie schematu MQTT** z dostawcą urządzeń (topic taxonomy + payload schema).
3. **Setup repozytorium** wraz z `pyproject.toml` (UV), `Dockerfile`, `docker-compose.yml`.
4. **Pierwsza migracja Alembic** z tabelami z sekcji 4 dokumentu `01-plan-...`.
5. **Skeleton blueprintów** Flask zgodny z modułami z sekcji 2.

---

*Dokument przygotowany przez zespół: Architekt systemów, Python Developer, Specjalista Compliance Żywności (UK), UX/UI Designer.*
