# Plan architektoniczno-funkcjonalny
## System Zarządzania Jakością (QMS) dla produkcji żywności w UK

> **Domena:** Piekarnia / produkcja żywności
> **Region regulacyjny:** Wielka Brytania (FSA, SALSA, HACCP)
> **Stos technologiczny:** Flask + UV (Python), HTML/CSS/JS, PostgreSQL, Redis, MQTT
> **Tryb pracy:** Multiuser, wielojęzyczny (PL/EN)
> **Wersja dokumentu:** 1.0
> **Data:** 2026-04-28

---

## 0. Streszczenie wykonawcze

System QMS dla piekarni to platforma webowa rejestrująca, klasyfikująca i obsługująca **niezgodności jakościowe** (tzw. *tickety*) na linii produkcyjnej. Każdy ticket przechodzi przez **konfigurowalny pipeline etapów** (od wykrycia, przez analizę, akcję korygującą, weryfikację, aż po zamknięcie). System integruje się z urządzeniami IoT (czujniki temperatury w piecach, wagi, mierniki wilgotności) oraz pozwala operatorom ręcznie zgłaszać incydenty z poziomu tabletu na hali.

Kluczowe wartości biznesowe:

| Wartość | Mechanizm |
|---|---|
| **Zgodność z FSA i SALSA** | Pełny audit trail, dokumentacja CCP wg HACCP, raporty 1-click |
| **Redukcja strat surowca** | Wczesne wykrywanie anomalii (triggery z czujników → automatyczne wstrzymanie partii) |
| **Skrócony czas reakcji** | Respondery uruchamiające akcje (powiadomienie SMS, e-mail, wstrzymanie linii) |
| **Mierzalność procesu** | KPI: First Pass Yield, NCR rate, MTTR, Cost of Poor Quality |
| **Praca wielonarodowa** | Interfejs PL/EN — typowy zespół piekarni UK |

Kluczowe decyzje architektoniczne (uzasadnione w sekcji 1):

- **Flask + UV** — lekki, dojrzały framework Python; UV dla deterministycznych buildów i szybkiego onboardingu deweloperów.
- **PostgreSQL 16** — JSONB dla elastycznych pól pipeline, partycjonowanie audit_log po dacie, transakcyjność krytyczna dla CCP.
- **Redis + RQ** — asynchroniczne respondery, kolejka triggerów, sesje.
- **MQTT (Mosquitto)** — standard de-facto dla IoT w przemyśle spożywczym, low-bandwidth, QoS.
- **HTMX + Vanilla JS + Web Components** — żadnych ciężkich bundlerów, szybkie ładowanie na słabszym sprzęcie hali produkcyjnej.

---

## 1. Architektura systemu

### 1.1. Model warstwowy

System realizuje klasyczną architekturę trzywarstwową z dodatkową warstwą integracyjną dla IoT:

```
┌──────────────────────────────────────────────────────────────┐
│  WARSTWA PREZENTACJI                                          │
│  • Frontend webowy (HTML5 + CSS3 + Vanilla JS + HTMX)         │
│  • PWA dla operatorów na tablecie (offline-first)             │
│  • Web Components dla widgetów (timeline, drag-drop pipeline) │
└──────────────────────────────────────────────────────────────┘
                            ▲ HTTPS / REST + Server-Sent Events
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  WARSTWA LOGIKI BIZNESOWEJ (Flask + UV)                       │
│  • Flask App (WSGI: gunicorn z uvicorn workerami)             │
│  • Blueprinty: auth, tickets, pipeline, triggers, admin, api  │
│  • SQLAlchemy 2.0 ORM + Alembic (migracje)                    │
│  • Flask-Babel (i18n PL/EN)                                   │
│  • Flask-Login + RBAC (custom decorators)                     │
│  • Silnik reguł (triggers/responders) — własny DSL w JSONB    │
│  • Worker RQ (zadania asynchroniczne)                         │
└──────────────────────────────────────────────────────────────┘
       ▲                    ▲                    ▲
       │ MQTT              │ SQL               │ Redis
       ▼                    ▼                    ▼
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ Mosquitto   │      │ PostgreSQL  │      │   Redis     │
│ (IoT bridge)│      │     16      │      │ cache+queue │
└─────────────┘      └─────────────┘      └─────────────┘
       ▲
       │ czujniki, wagi, mierniki
   ┌───┴────┐
   │  IoT   │
   │ (hala) │
   └────────┘
```

### 1.2. Komponenty i ich odpowiedzialności

| Komponent | Technologia | Odpowiedzialność |
|---|---|---|
| **Reverse proxy** | Nginx | TLS termination, rate-limiting, statyki |
| **Aplikacja Flask** | Python 3.12 + UV | Logika biznesowa, REST API, renderowanie szablonów Jinja2 |
| **Worker** | RQ (Python) | Asynchroniczne respondery, generowanie raportów PDF, wysyłka powiadomień |
| **MQTT Bridge** | paho-mqtt + Flask | Subskrypcja topiców z urządzeń, normalizacja, wstawianie do kolejki ticketów |
| **Baza relacyjna** | PostgreSQL 16 | Trwałe dane, transakcyjność, audit log |
| **Cache i kolejka** | Redis 7 | Sesje, rate-limiting, kolejka RQ, pub/sub dla SSE |
| **Storage plików** | Wolumen lokalny / S3 | Załączniki ticketów (zdjęcia z hali), raporty PDF |

### 1.3. Uzasadnienie kluczowych wyborów

**Dlaczego Flask, a nie Django/FastAPI?**
Flask oferuje minimalizm i pełną kontrolę nad strukturą blueprintów, co jest istotne przy custom silniku reguł i specyficznym pipeline'ie HACCP. Django jest zbyt opiniotwórcze (admin), a FastAPI nie ma dojrzałego wsparcia dla server-rendered HTML, którego wymaga PWA dla operatorów. Flask + Blueprinty + SQLAlchemy to sprawdzony stack dla aplikacji compliance.

**Dlaczego UV?**
UV (Astral) zapewnia 10–100× szybsze rozwiązywanie zależności niż pip, deterministyczne `uv.lock`, oraz łatwy onboarding (`uv sync`). Krytyczne przy CI/CD i instalacji na środowiskach produkcyjnych z ograniczonym łączem.

**Dlaczego PostgreSQL?**
- JSONB pozwala zapisać definicję pipeline jako elastyczny dokument, bez migracji schematu przy każdej zmianie linii produkcyjnej.
- Partycjonowanie deklaratywne `audit_log` po `created_at` (miesięcznie) — niezbędne przy retention 7 lat (wymóg FSA).
- Transakcje serializowalne dla rejestrowania pomiarów CCP (atomicity gwarantująca, że pomiar i jego konsekwencje są spójne).
- Pełnotekstowe wyszukiwanie (`tsvector`) dla przeszukiwania komentarzy ticketów.

**Dlaczego HTMX zamiast React/Vue?**
Operatorzy na hali używają tabletów Android sprzed kilku lat, często w rękawiczkach, w hałasie. HTMX daje szybkie, server-rendered HTML z minimalnym JS. Brak bundlera = brak `node_modules` = niższy próg utrzymaniowy. Web Components dla widgetów wymagających state'u (timeline ticketu, drag-drop konfigurator pipeline'u).

---

## 2. Specyfikacja modułów

### 2.1. Moduł `auth` — Uwierzytelnianie i autoryzacja

**Odpowiedzialność:** Logowanie, sesje, RBAC, polityka haseł, opcjonalne 2FA.

**Komponenty:**
- `UserModel` (SQLAlchemy) — `id`, `email`, `password_hash` (bcrypt cost 12), `role_id`, `language`, `is_active`, `last_login_at`, `failed_attempts`.
- `RoleModel` + `PermissionModel` — relacja many-to-many.
- Dekorator `@require_permission('tickets.create')` na widokach Flask.
- `Flask-Login` dla sesji + `Flask-WTF` z CSRF.
- Polityka: lockout po 5 nieudanych próbach na 15 min, wymuszenie zmiany hasła co 90 dni (wymaganie SALSA dla kont z dostępem do CCP).

**Endpointy:**
- `POST /auth/login` / `POST /auth/logout`
- `POST /auth/2fa/enroll` / `POST /auth/2fa/verify` (TOTP)
- `POST /auth/password/change`

### 2.2. Moduł `tickets` — Zgłoszenia jakościowe

**Odpowiedzialność:** Cykl życia ticketu (zgłoszenie → analiza → akcja → weryfikacja → zamknięcie), klasyfikacja, przypisanie, załączniki.

**Stany ticketu (state machine):**
```
NEW → ASSIGNED → IN_PROGRESS → AWAITING_VERIFICATION → CLOSED
                      ↓                                    ↑
                  ESCALATED ──────────────────────────────┘
                      ↓
                  REJECTED (z uzasadnieniem)
```

Każde przejście stanu zapisuje rekord w `ticket_events` (kto, kiedy, z jakiego stanu, do jakiego, komentarz).

**Pola ticketu:**
- `id` (UUID), `production_line_id`, `pipeline_id`, `current_stage_id`
- `source` (enum: `manual`, `iot`, `api`)
- `severity` (enum: `low`, `medium`, `high`, `critical`)
- `category` (enum konfigurowalny: `temperature_deviation`, `weight_out_of_spec`, `foreign_body`, `allergen_cross_contact`, `hygiene`, `other`)
- `title`, `description` (i18n: zapisywany język + oryginalny tekst)
- `assigned_to_user_id`, `created_by_user_id`
- `created_at`, `updated_at`, `closed_at`
- `metadata` (JSONB) — np. odczyty czujników, batch_id, lot_number
- `is_ccp_related` (boolean) — flaga wskazująca powiązanie z krytycznym punktem kontroli

**Kluczowe widoki:**
- Lista z filtrami (linia, status, severity, data, przypisany)
- Szczegół (timeline + załączniki + komentarze + akcje)
- Formularz szybkiego zgłoszenia (mobilny, 3 kliknięcia)

### 2.3. Moduł `pipeline` — Konfigurowalne etapy

**Odpowiedzialność:** Definiowanie sekwencji etapów per linia produkcyjna; wymuszanie kolejności; walidacja przejść.

**Model:**
- `Pipeline` — definicja per `production_line_id`, wersjonowana (kolejne wersje przy zmianie konfiguracji, stare zachowane dla historycznych ticketów).
- `PipelineStage` — `name`, `order_index`, `required_role_id`, `sla_minutes` (po przekroczeniu odpalany trigger), `required_fields` (JSONB: lista nazw pól wymaganych do przejścia dalej), `is_ccp_checkpoint`.

**Konfigurator (UI):**
- Drag-and-drop listy etapów (Web Component oparty o HTML5 Drag API).
- Każdy etap: edytowalny tytuł (PL/EN), wymagana rola, SLA, lista wymaganych pól.
- Wersjonowanie: edycja tworzy `version+1`, ticket zachowuje `pipeline_version_id`.

**Domyślny pipeline dla piekarni** (przykład):
1. **Wykrycie** (operator hali) — opis + zdjęcie obowiązkowe
2. **Klasyfikacja** (QA specialist) — nadanie kategorii i severity
3. **Analiza przyczyny** (QA specialist) — 5 Why / Ishikawa, opcjonalnie
4. **Akcja korygująca** (line manager) — opis działania, batch hold/release
5. **Weryfikacja** (QA specialist) — sprawdzenie skuteczności
6. **Zamknięcie** (line manager) — podpis cyfrowy

### 2.4. Moduł `triggers` — Silnik reguł

**Odpowiedzialność:** Wykrywanie warunków (np. „temperatura > 220°C przez > 30s") i emisja zdarzeń wewnętrznych.

**Definicja triggera (JSONB):**
```json
{
  "name": "Pierwsza pieca przegrzanie",
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

**Implementacja:** ewaluator pracuje w kontekście strumienia odczytów IoT. Każdy odczyt z MQTT wpada do Redis Stream, worker subskrybuje i wykonuje `evaluate_triggers(reading)`. Stan czasowy (np. „przez 30s") trzymany w Redis z TTL.

### 2.5. Moduł `responders` — Akcje reaktywne

**Odpowiedzialność:** Wykonanie zaplanowanych akcji w odpowiedzi na trigger lub ręczną decyzję.

**Typy responderów:**
| Typ | Akcja |
|---|---|
| `notify_email` | Wysyłka e-maila do listy odbiorców (z szablonem i18n) |
| `notify_sms` | SMS przez Twilio / lokalnego dostawcę |
| `notify_in_app` | Push-notification w aplikacji + dzwonek |
| `create_ticket` | Utworzenie nowego ticketu |
| `pause_line` | Wysłanie komendy MQTT do urządzenia (wstrzymanie linii) |
| `escalate` | Eskalacja ticketu do wyższej roli |
| `webhook` | POST do zewnętrznego URL (np. ERP) |

Każde wykonanie respondera zapisywane w `trigger_executions` (audit) — kiedy, jaki trigger, jaki responder, status (success/failed), payload.

### 2.6. Moduł `haccp` — HACCP i Critical Control Points

**Odpowiedzialność:** Definicja CCP, rejestracja pomiarów, alarmowanie przy odchyleniach, dokumentacja korekcyjna.

**Model:**
- `CCPDefinition` — `name`, `production_line_id`, `parameter` (np. „temperatura wewnętrzna pieczywa"), `critical_limit_min`, `critical_limit_max`, `unit`, `monitoring_frequency_minutes`, `corrective_action_template`.
- `CCPMeasurement` — `ccp_definition_id`, `measured_value`, `measured_at`, `measured_by_user_id`, `device_id` (jeśli IoT), `is_within_limits`, `linked_ticket_id` (jeśli odchylenie).

**Workflow:**
1. CCP definiowany przez Compliance Officera w panelu admina.
2. System wymusza pomiar w zadanej częstotliwości (powiadomienia operatorów).
3. Pomiar poza granicami → automatyczny ticket o wysokim severity, etap **Akcja korygująca** wymagany.
4. Raport HACCP generowany jako miesięczny PDF z listą wszystkich pomiarów + uchybień + akcji korygujących.

### 2.7. Moduł `salsa` — Listy kontrolne SALSA

**Odpowiedzialność:** Cykliczne checklisty zgodne ze standardem SALSA (Safe And Local Supplier Approval).

**Zakres checklist:**
- **Higiena personelu** — codziennie (rękawice, maski, biżuteria, kontrola zdrowia)
- **Higiena maszyn** — przed każdą zmianą (ATP swab opcjonalnie)
- **Kontrola dostaw** — przy każdej dostawie surowca (temperatury, opakowanie, dokumenty)
- **Kontrola alergenów** — przy zmianie linii produkcyjnej między recepturami
- **Kontrola szkodników** — tygodniowo
- **Identyfikowalność (traceability)** — przy każdej partii (lot/batch)

**Model:**
- `SalsaChecklist` — szablon: `name`, `frequency` (`daily`/`shift`/`weekly`/`per_event`), `items` (JSONB lista pytań).
- `SalsaResponse` — wypełnienie: `checklist_id`, `responded_by`, `responded_at`, `answers` (JSONB), `nonconformities_count`, `signature_hash`.

Checklisty są częścią workflow zmiany — bez wypełnienia checklisty otwarcia zmiany operator nie może zarejestrować pomiarów CCP.

### 2.8. Moduł `audit` — Ścieżka audytu

**Odpowiedzialność:** Niezmienialny zapis każdej akcji w systemie.

**Model `audit_log`:**
```sql
id BIGSERIAL PRIMARY KEY,
occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
user_id UUID,                  -- NULL dla zdarzeń systemowych
session_id UUID,
entity_type VARCHAR(50),       -- np. 'ticket', 'ccp_measurement'
entity_id UUID,
action VARCHAR(50),            -- 'create', 'update', 'delete', 'state_change', 'view'
diff JSONB,                    -- przed/po
ip_address INET,
user_agent TEXT,
checksum CHAR(64)              -- SHA-256 z poprzedniego rekordu (chain hashing)
```

**Mechanizmy zabezpieczające:**
- Tabela tylko `INSERT` (trigger PostgreSQL blokuje UPDATE/DELETE).
- Chain hashing — każdy rekord zawiera SHA-256 poprzedniego (tamper-evidence).
- Codzienna replikacja do WORM-storage (np. AWS S3 Object Lock w trybie compliance).
- Partycjonowanie miesięczne — 7-letnia retencja zgodna z FSA.

### 2.9. Moduł `reporting` — Raportowanie

**Odpowiedzialność:** Generowanie raportów dla FSA, audytów wewnętrznych, managementu.

**Typy raportów:**
| Raport | Częstotliwość | Format | Odbiorca |
|---|---|---|---|
| HACCP Monitoring Report | Miesięcznie | PDF | Compliance Officer / FSA |
| SALSA Compliance Report | Kwartalnie | PDF | Auditor SALSA |
| NCR Report (Non-Conformity) | Tygodniowo | PDF + CSV | QA Manager |
| Production Quality KPI Dashboard | Live | HTML | Plant Manager |
| Traceability Report (per batch) | Ad-hoc | PDF | FSA / klient |
| Audit Trail Export | Ad-hoc | CSV / PDF (signed) | Auditor zewnętrzny |

**Implementacja:** WeasyPrint (HTML→PDF) dla raportów; szablony Jinja2 z pełnym i18n; podpis cyfrowy raportu (PDF z osadzonym certyfikatem).

### 2.10. Moduł `admin` — Panel administracyjny

**Odpowiedzialność:** Konfiguracja systemu bez ingerencji programisty.

**Funkcje:**
- CRUD linii produkcyjnych
- Konfigurator pipeline'ów (drag-drop)
- Definicje CCP
- Szablony checklist SALSA
- Definicje triggerów (formularz JSON-builder)
- Zarządzanie użytkownikami i rolami
- Konfiguracja powiadomień (kanały, odbiorcy)
- Tłumaczenia UI (panel edycji message catalog)
- Health-check integracji (status MQTT, kolejki RQ)

### 2.11. Moduł `i18n` — Wielojęzyczność

**Odpowiedzialność:** Pełna lokalizacja PL/EN.

**Implementacja:**
- Flask-Babel + pliki `.po` / `.mo` w `app/translations/{pl,en}/LC_MESSAGES/`.
- Detekcja języka: cookie → preferencja użytkownika → `Accept-Language` → fallback `en`.
- Treści dynamiczne (np. tytuły etapów pipeline) trzymane w JSONB jako `{"pl": "...", "en": "..."}`.
- Funkcja `gettext_dynamic(field, lang)` w Jinja2.
- Eksport raportów w języku odbiorcy (parametr `?lang=en`).

Patrz: dokument **02-diagramy-architektury.md**, Diagram 5.

### 2.12. Moduł `integrations` — Integracje zewnętrzne

**Odpowiedzialność:** Komunikacja z urządzeniami i systemami zewnętrznymi.

**MQTT Bridge:**
- Subskrypcja topiców `factory/{line}/{device}/{metric}`.
- Normalizacja payloadu (różni producenci — różne formaty: JSON, CSV, binarny) przez warstwę adapterów (`adapters/oven_xyz.py`).
- Buforowanie offline (Redis Stream, max 100k odczytów / linia, FIFO drop).

**REST API:**
- `/api/v1/tickets` (POST) — przyjmowanie zgłoszeń od systemów zewnętrznych (np. ERP, system reklamacyjny).
- `/api/v1/measurements` (POST) — pomiary z urządzeń niewspierających MQTT.
- API-key + HMAC w nagłówku `X-Signature` dla autoryzacji.
- Rate-limiting: 100 req/min per klucz.

**Webhooks (wychodzące):**
- POST do skonfigurowanego URL przy zdarzeniu (`ticket.created`, `ccp.violated`).
- Retry z exponential backoff (3, 9, 27 minut), DLQ w Redis po wyczerpaniu.

---

## 3. Przepływ danych i integracje

### 3.1. Trzy źródła ticketów

#### Źródło 1: Wejście manualne (hala produkcyjna)
1. Operator klika ikonę „Nowe zgłoszenie" na tablecie (PWA).
2. Formularz: linia (auto-detect po zalogowanym urządzeniu), kategoria, severity, opis, zdjęcie z kamery.
3. Submit → `POST /tickets` → walidacja → zapis → emisja zdarzenia `ticket.created`.
4. Trigger reguł zaszywa `notify_qa` jeśli severity ≥ high.

#### Źródło 2: Urządzenia produkcyjne (IoT)
1. Czujnik publikuje na MQTT topic `factory/line_a/oven_1/temp` co 1s.
2. Mosquitto przekazuje do Flask MQTT Bridge.
3. Bridge waliduje payload → wstawia do Redis Stream `metrics:line_a`.
4. Worker `trigger_evaluator` konsumuje stream → ewaluuje aktywne triggery.
5. Trigger spełniony → tworzy ticket przez `TicketService.create_from_trigger()`.

#### Źródło 3: API zewnętrzne
1. System ERP wysyła `POST /api/v1/tickets` z reklamacją klienta.
2. Walidacja API-key + HMAC.
3. Mapowanie pól (zewnętrznych → wewnętrznych) przez adapter.
4. Utworzenie ticketu z flagą `source=api` i metadanymi źródła.

### 3.2. Wewnętrzne przepływy

**Trigger → Responder:**
```
Odczyt IoT → Redis Stream → Worker → Trigger Engine
   → match? → emit `trigger.fired` → Responder Dispatcher
   → wykonanie akcji (notify/create_ticket/pause_line)
   → audit_log INSERT
```

**Pomiar CCP:**
```
Operator wprowadza pomiar → walidacja kontra critical_limits
   → if poza limitem: tworzenie ticketu (severity=critical)
                    + alert do Compliance Officer
                    + zablokowanie release'u partii
   → audit_log INSERT
```

### 3.3. Diagramy

Szczegółowe diagramy sekwencyjne i flowcharty znajdują się w dokumencie **02-diagramy-architektury.md**:
- Diagram 1 — Architektura warstwowa
- Diagram 2 — Przepływ ticketów
- Diagram 3 — Integracja modułów compliance

---

## 4. Model bazy danych

### 4.1. Diagram ERD (logiczny)

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
                    │permissions │                           │ przy każdej
                    └────────────┘                           │ akcji
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

### 4.2. Kluczowe tabele

#### `users`
| Pole | Typ | Indeks/Constraint |
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
Klasyczny wzorzec RBAC. `permissions.code` to napis typu `tickets.create`, `pipeline.configure`, `audit.export`.

#### `production_lines`
| Pole | Typ |
|---|---|
| id | UUID PK |
| name | VARCHAR(100) |
| location | VARCHAR(100) |
| is_active | BOOLEAN |
| metadata | JSONB |

#### `pipelines`
| Pole | Typ |
|---|---|
| id | UUID PK |
| production_line_id | UUID FK |
| version | INT |
| is_active | BOOLEAN |
| created_at | TIMESTAMPTZ |
| created_by_user_id | UUID FK |

UNIQUE (`production_line_id`, `version`).

#### `pipeline_stages`
| Pole | Typ |
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
| Pole | Typ |
|---|---|
| id | UUID PK |
| ticket_number | VARCHAR(20) UNIQUE | (np. `QMS-2026-00042`) |
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

**Indeksy:**
- `idx_tickets_status_open` — partial: `WHERE status NOT IN ('CLOSED','REJECTED')`
- `idx_tickets_line_created` — `(production_line_id, created_at DESC)`
- `idx_tickets_severity_created` — `(severity, created_at DESC)`
- `idx_tickets_assignee_status` — `(assigned_to_user_id, status)`
- `idx_tickets_metadata_gin` — GIN na `metadata` (wyszukiwanie po batch_id, lot_number)

#### `ticket_events`
| Pole | Typ |
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
Patrz sekcja 2.8. Partycjonowanie miesięczne (`PARTITION BY RANGE (occurred_at)`), retention 7 lat.

#### `ccp_definitions`, `ccp_measurements`
Patrz sekcja 2.6. INDEX `(ccp_definition_id, measured_at DESC)` dla generowania raportów.

#### `salsa_checklists`, `salsa_responses`
Patrz sekcja 2.7.

#### `triggers`
| Pole | Typ |
|---|---|
| id | UUID PK |
| name | JSONB |
| scope | VARCHAR(100) |
| condition | JSONB |
| severity | VARCHAR(10) |
| is_active | BOOLEAN |
| created_by_user_id | UUID |

#### `responders`
| Pole | Typ |
|---|---|
| id | UUID PK |
| name | JSONB |
| type | VARCHAR(30) |
| config | JSONB |
| is_active | BOOLEAN |

#### `trigger_responders` (M:N)
PK (`trigger_id`, `responder_id`, `order_index`).

#### `trigger_executions`
Każde wykonanie respondera. Indeks na `(trigger_id, executed_at DESC)`.

#### `translations` (opcjonalnie — dla edycji w runtime)
Override dla Babel `.po` przez panel admina. Klucz + język + tekst.

### 4.3. Strategia indeksów i wydajności

- **Partial indexes** dla otwartych ticketów (większość zapytań dotyczy aktywnych).
- **GIN** na JSONB tam, gdzie potrzebne wyszukiwanie po polach (`metadata`, `name`).
- **Partycjonowanie** `audit_log` i `ccp_measurements` po dacie (po 12 miesięcy partycje aktywne, starsze read-only).
- **VACUUM/ANALYZE** harmonogram nocny.
- **Replikacja** read-replica dla raportowania (oddzielenie od OLTP).
- **Connection pooling** PgBouncer w trybie transaction.

### 4.4. Skalowanie

| Skala | Strategia |
|---|---|
| **MVP / 1 zakład / ~50 użytkowników** | Single-node PostgreSQL, ~100 GB |
| **5 zakładów / 250 użytkowników** | Read-replica + PgBouncer, partycjonowanie |
| **Korporacja / >1000 użytkowników** | Sharding po `tenant_id` + Citus / Patroni HA |

---

## 5. UX/UI — wireframy i założenia

### 5.1. Zasady projektowe

1. **Hala produkcyjna ≠ biuro.** Tablety w rękawiczkach → przyciski min 56×56 px, kontrast WCAG AAA.
2. **3 kliknięcia do zgłoszenia.** Operator nie ma czasu klikać przez 10 ekranów.
3. **Offline-first.** PWA cache'uje ostatnie dane; submit kolejkowany w IndexedDB.
4. **Język — w jednym kliknięciu.** Switch PL/EN w prawym górnym rogu, persistuje per użytkownik.
5. **Dark mode i wysoki kontrast.** Hala bywa ciemna, monitor — zalany światłem.
6. **Brak żargonu IT.** „Zgłoszenie" zamiast „ticket", „etap" zamiast „stage", „alarm" zamiast „trigger".

### 5.2. Wireframe — Dashboard główny

```
┌────────────────────────────────────────────────────────────────────┐
│  QMS — Piekarnia A                  🇵🇱 PL │ 🇬🇧 EN     [Jan K. ▼] │
├────────────────────────────────────────────────────────────────────┤
│ ▌ MENU       │  ALARMY (3)                                          │
│              │  ┌──────────────────────────────────────────────┐   │
│ ▣ Dashboard  │  │ 🔴 LINIA A — piec 1 — temp 232°C  [PRZEJDŹ]  │   │
│ ▢ Zgłoszenia │  │ 🟠 LINIA B — waga — odchył +3.2%  [PRZEJDŹ]  │   │
│ ▢ Pipeline   │  │ 🟡 LINIA C — SLA przekroczony     [PRZEJDŹ]  │   │
│ ▢ HACCP/CCP  │  └──────────────────────────────────────────────┘   │
│ ▢ SALSA      │                                                      │
│ ▢ Raporty    │  PRZEGLĄD LINII                                      │
│ ▢ Admin      │  ┌─────────┬─────────┬─────────┐                    │
│              │  │ LINIA A │ LINIA B │ LINIA C │                    │
│              │  │  ✅ OK  │ ⚠️ NCR │  🔴 STOP│                    │
│              │  │  98% FPY│ 92% FPY │  —     │                    │
│              │  │  2 open │ 5 open  │ 12 open │                    │
│              │  └─────────┴─────────┴─────────┘                    │
│              │                                                      │
│              │  KPI (24h)            │  CCP (dziś)                  │
│              │  • Otwarte: 19        │  ┌──────────────────────┐   │
│              │  • Zamknięte: 47      │  │ ▓▓▓▓▓▓░░░ 7/9 done   │   │
│              │  • MTTR: 42 min       │  │ ❌ 1 odchylenie       │   │
│              │  • Severity h+: 3     │  └──────────────────────┘   │
└──────────────┴──────────────────────────────────────────────────────┘
```

### 5.3. Wireframe — Lista zgłoszeń

```
┌────────────────────────────────────────────────────────────────────┐
│  ZGŁOSZENIA                                  [+ NOWE ZGŁOSZENIE]   │
├────────────────────────────────────────────────────────────────────┤
│ Linia: [Wszystkie ▼]  Status: [Otwarte ▼]  Severity: [Wszystkie ▼] │
│ Data od: [____] do: [____]    🔍 Szukaj: [_____________]   [Filtr] │
├────────────────────────────────────────────────────────────────────┤
│ # NUMER       │ LINIA │ KAT.        │ SEV │ STATUS  │ SLA  │ AKCJA │
├───────────────┼───────┼─────────────┼─────┼─────────┼──────┼───────┤
│ QMS-2026-0042 │ A     │ Temperatura │ 🔴  │ Analiza │ 12m  │ [▶]   │
│ QMS-2026-0041 │ B     │ Waga        │ 🟠  │ Akcja   │ 1h   │ [▶]   │
│ QMS-2026-0040 │ A     │ Higiena     │ 🟡  │ Weryf.  │ 4h   │ [▶]   │
│ QMS-2026-0039 │ C     │ Alergen     │ 🔴  │ Zamkn.  │ —    │ [▶]   │
│ ...                                                                │
├────────────────────────────────────────────────────────────────────┤
│                                  [‹ Poprz.]  Strona 1/12  [Nast. ›]│
└────────────────────────────────────────────────────────────────────┘
```

### 5.4. Wireframe — Szczegół zgłoszenia

```
┌────────────────────────────────────────────────────────────────────┐
│ ← Wstecz   QMS-2026-0042: Przegrzanie pieca 1 (LINIA A)            │
│            🔴 Severity: HIGH    Status: ANALIZA    SLA: 12 min ⏱️   │
├────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────┬─────────────────────────────────────┐ │
│ │ TIMELINE                 │ AKCJE DOSTĘPNE                      │ │
│ │                          │ [Przejdź do następnego etapu]       │ │
│ │ 🟢 14:02 Zgłoszenie      │ [Eskaluj do managera]               │ │
│ │    auto z czujnika       │ [Dodaj komentarz]                   │ │
│ │                          │ [Załącz plik]                       │ │
│ │ 🟢 14:03 Klasyfikacja    │                                     │ │
│ │    Anna K. — kat: temp   │ POMIARY POWIĄZANE                   │ │
│ │                          │ • Temp 232°C @ 14:01:32             │ │
│ │ 🔵 14:05 Analiza         │ • Temp 234°C @ 14:01:58             │ │
│ │    Trwa... (Marek W.)    │ • Temp 230°C @ 14:02:15             │ │
│ │                          │                                     │ │
│ │ ⚪ Akcja korygująca      │ POWIĄZANY CCP: Temp pieca           │ │
│ │ ⚪ Weryfikacja           │ Pomiar wymagany: TAK                │ │
│ │ ⚪ Zamknięcie            │ Akcja korygująca: szablon dostępny  │ │
│ └──────────────────────────┴─────────────────────────────────────┘ │
│                                                                    │
│ KOMENTARZE                                                         │
│ ┌────────────────────────────────────────────────────────────────┐ │
│ │ Anna K. (14:03): Klasyfikacja jako temperature_deviation       │ │
│ │ Marek W. (14:05): Sprawdzam czujnik i kalibrację...            │ │
│ └────────────────────────────────────────────────────────────────┘ │
│ [Dodaj komentarz_______________________________________] [Wyślij]  │
└────────────────────────────────────────────────────────────────────┘
```

### 5.5. Wireframe — Konfiguracja pipeline

```
┌────────────────────────────────────────────────────────────────────┐
│ KONFIGURACJA PIPELINE — LINIA A          Wersja: 7 (draft)         │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ 1. WYKRYCIE  │→│2. KLASYFIK.  │→│ 3. ANALIZA   │→ ...           │
│  │ Rola: Op.    │  │ Rola: QA     │  │ Rola: QA     │              │
│  │ SLA: 5 min   │  │ SLA: 15 min  │  │ SLA: 60 min  │              │
│  │ ☐ CCP        │  │ ☐ CCP        │  │ ☑ CCP        │              │
│  │ [✏ Edytuj]   │  │ [✏ Edytuj]   │  │ [✏ Edytuj]   │              │
│  │ [🗑 Usuń]    │  │ [🗑 Usuń]    │  │ [🗑 Usuń]    │              │
│  └──────────────┘  └──────────────┘  └──────────────┘              │
│        ⇅                ⇅                ⇅                          │
│      drag                                                          │
│                                                                    │
│  [+ Dodaj etap]                                                    │
│                                                                    │
│  ────────────────────────────────────────────────────────────────  │
│  [Anuluj]   [Zapisz jako draft]   [Opublikuj wersję 7]             │
└────────────────────────────────────────────────────────────────────┘
```

### 5.6. Wireframe — Mobile (PWA, operator hali)

```
┌──────────────────────┐
│ 🇵🇱  QMS  LINIA A   │
├──────────────────────┤
│                      │
│  +  NOWE ZGŁOSZENIE  │
│ ┌──────────────────┐ │
│ │  ️⚠️ ZGŁOŚ        │ │
│ │     PROBLEM      │ │
│ └──────────────────┘ │
│                      │
│  📋 CHECKLISTY DZIŚ  │
│ ┌──────────────────┐ │
│ │ Higiena   ✅     │ │
│ │ Maszyny   ⚠️ 1/3 │ │
│ │ Dostawy   ⏳     │ │
│ └──────────────────┘ │
│                      │
│  📊 MOJE ZGŁOSZENIA  │
│ ┌──────────────────┐ │
│ │ 0042 🔴 Analiza  │ │
│ │ 0038 🟡 Akcja    │ │
│ └──────────────────┘ │
│                      │
│  🌡️ POMIARY CCP      │
│ ┌──────────────────┐ │
│ │ Temp pieca 1     │ │
│ │ [____] °C [Zapisz]│ │
│ └──────────────────┘ │
└──────────────────────┘
```

### 5.7. Wireframe — Panel admina (definicja triggera)

```
┌────────────────────────────────────────────────────────────────────┐
│ NOWY TRIGGER — Definicja                                           │
├────────────────────────────────────────────────────────────────────┤
│ Nazwa (PL): [Przegrzanie pieca 1                  ]                │
│ Nazwa (EN): [Oven 1 overheating                   ]                │
│                                                                    │
│ Zakres: [Linia A ▼]                                                │
│                                                                    │
│ WARUNEK                                                            │
│ Metryka:    [temperature ▼]                                        │
│ Operator:   [>           ▼]                                        │
│ Wartość:    [220     ] °C                                          │
│ Czas trwania: [30   ] sekund                                       │
│                                                                    │
│ PO SPEŁNIENIU                                                      │
│ ☑ Utwórz zgłoszenie  (severity: [HIGH ▼], kategoria: [Temp ▼])    │
│ ☑ Powiadom: [QA Manager, Line Manager A      ] (e-mail + SMS)      │
│ ☐ Wstrzymaj linię                                                  │
│                                                                    │
│ [Anuluj]                            [Zapisz draft]   [Aktywuj]     │
└────────────────────────────────────────────────────────────────────┘
```

---

## 6. System uprawnień i ról

### 6.1. Role

| Rola | Skrót | Opis |
|---|---|---|
| **Operator produkcji** | `operator` | Pracownik hali — zgłasza problemy, wypełnia checklisty, wprowadza pomiary CCP |
| **QA Specialist** | `qa` | Specjalista jakości — klasyfikuje, analizuje, weryfikuje akcje korygujące |
| **Line Manager** | `line_manager` | Kierownik linii — zatwierdza akcje korygujące, eskalacje, batch hold/release |
| **Compliance Officer** | `compliance` | Specjalista compliance — definiuje CCP, SALSA checklisty, eksportuje raporty FSA |
| **Plant Manager** | `plant_manager` | Kierownik zakładu — przegląda KPI, raporty, ale nie modyfikuje konfiguracji technicznej |
| **Administrator** | `admin` | Administrator systemu — pełna konfiguracja, zarządzanie użytkownikami, audyty |

### 6.2. Macierz uprawnień (RBAC matrix)

Legenda: ✅ pełen dostęp | 👁️ tylko odczyt | ✍️ ograniczony zapis | ❌ brak dostępu

| Funkcja                       | Operator | QA  | Line Mgr | Compl. | Plant Mgr | Admin |
|---|---|---|---|---|---|---|
| Tworzenie zgłoszeń            | ✅       | ✅  | ✅       | ✅     | ✅        | ✅    |
| Klasyfikacja zgłoszeń         | ❌       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Akcja korygująca              | ❌       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Zatwierdzanie zamknięcia      | ❌       | ❌  | ✅       | ✅     | ❌        | ✅    |
| Wprowadzanie pomiarów CCP     | ✅       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Definiowanie CCP              | ❌       | ❌  | ❌       | ✅     | ❌        | ✅    |
| Wypełnianie checklist SALSA   | ✅       | ✅  | ✅       | ✅     | ❌        | ✅    |
| Definiowanie checklist SALSA  | ❌       | ❌  | ❌       | ✅     | ❌        | ✅    |
| Konfiguracja pipeline         | ❌       | ❌  | ✍️*      | ✅     | ❌        | ✅    |
| Definiowanie triggerów        | ❌       | ✍️* | ✍️*      | ✅     | ❌        | ✅    |
| Zarządzanie użytkownikami     | ❌       | ❌  | ❌       | ❌     | ❌        | ✅    |
| Eksport audit trail           | ❌       | 👁️  | 👁️       | ✅     | 👁️        | ✅    |
| Generowanie raportu FSA       | ❌       | ✍️  | ✍️       | ✅     | ✅        | ✅    |
| Dashboard KPI                 | 👁️ (linia)| 👁️ | 👁️       | 👁️    | 👁️ (zakład)| 👁️   |
| Konfiguracja systemu          | ❌       | ❌  | ❌       | ❌     | ❌        | ✅    |
| Przeglądanie audit_log        | ❌       | 👁️* | 👁️*      | 👁️    | 👁️        | 👁️   |

\* tylko swoje akcje / swoja linia.

### 6.3. Kontrola konkurencji (multi-user)

- **Optimistic locking** dla edycji ticketów (kolumna `version`, INC przy update). Próba zapisu starszej wersji → 409 Conflict + UI pyta „przejmij / odśwież".
- **Pessimistic locking** dla CCP measurement w toku (Redis lock z TTL 5 min) — uniknięcie podwójnego zapisu pomiaru z dwóch tabletów.
- **Server-Sent Events** dla aktualizacji listy zgłoszeń w czasie rzeczywistym — gdy QA klika ticket, lista u Line Managera odświeża się automatycznie.

### 6.4. Audyt dostępu

Każda autoryzacyjna decyzja (allow/deny) zapisywana w `audit_log` z `entity_type='access'`, `action='check'`. Pozwala wykryć próby eskalacji uprawnień.

---

## 7. Raportowanie i analityka

### 7.1. KPI operacyjne (live)

| KPI | Definicja | Cel |
|---|---|---|
| **First Pass Yield (FPY)** | (Partie bez NCR / Wszystkie partie) × 100% | ≥ 98% |
| **NCR Rate** | Liczba zgłoszeń / 1000 partii | ≤ 5 |
| **Mean Time To Resolve (MTTR)** | Średni czas od `NEW` do `CLOSED` | ≤ 4h dla high+ |
| **SLA Compliance** | % ticketów zamkniętych w SLA | ≥ 95% |
| **CCP Compliance** | % pomiarów w granicach krytycznych | ≥ 99.5% |
| **SALSA Checklist Completion** | % wypełnionych checklist w terminie | 100% |
| **Cost of Poor Quality (CoPQ)** | Σ utrata surowca + przestoje | śledzona, redukcja YoY |

### 7.2. Raporty regulacyjne

#### Raport HACCP (miesięczny)
- Lista wszystkich CCP definicji aktywnych w okresie
- Lista pomiarów (data, wartość, w/poza limitem, operator)
- Odchylenia + akcje korygujące + weryfikacje
- Podpis cyfrowy Compliance Officera (TOTP-confirmed)
- Format: PDF/A-2 (długoterminowa archiwizacja)

#### Raport SALSA (kwartalny)
- Wynik wszystkich checklist w okresie
- Niezgodności + akcje
- Trendy (% completion, % nonconformities)

#### Raport FSA on-demand
- Traceability per batch: skąd surowiec, kiedy, kto, jakie pomiary, jakie zgłoszenia
- Wygenerowanie w < 60 sekund (wymóg FSA przy kontroli)

### 7.3. Dashboard analityczny

- Wykresy Chart.js — trendy 7/30/90 dni
- Drill-down: klik w słupek → lista ticketów w okresie
- Heatmap odchyleń CCP (godzina × dzień tygodnia)
- Pareto najczęstszych kategorii NCR

### 7.4. Eksporty

| Format | Zawartość |
|---|---|
| CSV | Tickety, pomiary CCP, checklisty SALSA — surowe dane do BI |
| PDF | Raporty z podpisem |
| JSON | API export dla integracji z BI/Power BI |
| Audit Trail (CSV/PDF, podpisany) | Dla auditora zewnętrznego |

---

## 8. Plan wdrożenia

### 8.1. Fazy

#### Faza 0 — Discovery (4 tygodnie)
- Wywiady z operatorami, QA, Line Managerami, Compliance Officerem
- Inwentaryzacja urządzeń IoT (modele, protokoły)
- Mapowanie istniejących procesów i dokumentów (papierowych)
- Ustalenie pierwszej linii pilotażowej
- **Deliverable:** Specyfikacja funkcjonalna v1.1

#### Faza 1 — MVP (8 tygodni)
- Setup infrastruktury (Docker Compose dev + staging)
- Auth, RBAC, podstawowy CRUD ticketów
- Statyczny pipeline (1 linia, 5 etapów hardcoded)
- Manual ticket entry (PWA)
- Audit trail
- i18n PL/EN
- **Deliverable:** Działający system bez IoT, gotowy do testów wewnętrznych

#### Faza 2 — Pilot na 1 linii (6 tygodni)
- Konfigurowalny pipeline + admin panel
- HACCP/CCP — definicje + manualne pomiary
- SALSA checklisty
- Integracja MQTT — 2-3 czujniki pilotowe
- Triggery i podstawowe respondery (notify_email, notify_in_app)
- Raport HACCP (PDF)
- **Deliverable:** Pilot na linii A; szkolenia operatorów

#### Faza 3 — Walidacja compliance (4 tygodnie)
- Audyt wewnętrzny przez Compliance Officera
- Pre-audit SALSA — symulacja audytu zewnętrznego
- Test penetracyjny (OWASP Top 10)
- Test obciążeniowy (Locust — 200 ticketów/min)
- Recovery test (failover bazy, restart MQTT)
- **Deliverable:** Certyfikat zgodności wewnętrznej; gotowość do auditu SALSA

#### Faza 4 — Rollout (8-12 tygodni)
- Stopniowe przepinanie kolejnych linii (1 linia / 2 tygodnie)
- Migracja danych historycznych (skany dokumentów papierowych)
- Szkolenia dla wszystkich zmian (operator + line manager)
- Hypercare 2 tygodnie po rolloucie każdej linii
- **Deliverable:** Pełny zakład na QMS

#### Faza 5 — Optymalizacja (ciągle)
- Tuning triggerów na podstawie 3 mc danych
- Dodanie ML do predykcji odchyleń (opcjonalnie, po stabilizacji)
- Integracja z ERP (zamówienia surowca, traceability)
- Mobile app native (jeśli PWA niewystarczające)

### 8.2. Zasoby

| Rola | FTE | Faza |
|---|---|---|
| Product Owner / Analityk biznesowy | 1.0 | 0–5 |
| Architekt systemu | 0.5 | 0–2 |
| Backend dev (Python/Flask) | 2.0 | 1–4 |
| Frontend dev (HTML/CSS/JS) | 1.0 | 1–4 |
| DevOps / SRE | 0.5 | 0–5 |
| QA Engineer | 1.0 | 1–4 |
| UX Designer | 0.5 | 0–2 |
| Specjalista Compliance (wewn.) | 0.3 | 0–5 |
| **Razem (peak)** | **~6.8 FTE** | Faza 2 |

### 8.3. Timeline (overview)

```
M1   M2   M3   M4   M5   M6   M7   M8   M9   M10  M11  M12
├─F0─┤
       ├──── Faza 1 (MVP) ────┤
                              ├── Faza 2 (Pilot) ──┤
                                                  ├─F3─┤
                                                       ├── Faza 4 (Rollout) ─────┤
                                                                                  ├ Faza 5 →
```

### 8.4. Kryteria akceptacji per faza (DoD)

- Wszystkie testy automatyczne zielone (≥ 80% coverage)
- Test penetracyjny bez „Critical"/„High" findings
- Audit trail kompletny dla wszystkich akcji
- Dokumentacja użytkownika (PL+EN) zaktualizowana
- Szkolenia dla użytkowników końcowych zrealizowane
- Acceptance test z udziałem Plant Managera + Compliance Officera

---

## 9. Potencjalne zagrożenia i rozwiązania

### 9.1. Ryzyka techniczne

| Ryzyko | Prawdopodobieństwo | Impact | Mitygacja |
|---|---|---|---|
| **Utrata połączenia MQTT** (sieć hali) | Średnie | Wysoki | Buffer w Redis Stream + retry; alarm po 60s offline; fallback ręczny pomiar |
| **Awaria PostgreSQL** | Niskie | Krytyczny | Replikacja streaming (Patroni HA), backup co 4h, RPO ≤ 4h, RTO ≤ 1h |
| **Wzrost objętości audit_log** | Pewne | Średni | Partycjonowanie miesięczne, archiwizacja do S3 Object Lock po 12 mc |
| **Performance — wolne raporty** | Średnie | Średni | Read-replica, materialized views dla KPI, generowanie nocą |
| **Niezgodne formaty IoT** | Wysokie | Średni | Warstwa adapterów per producent, walidacja schemy, DLQ dla niewspieranych |
| **PWA cache stale** | Średnie | Niski | Service Worker z `network-first` dla danych krytycznych, force-refresh po deploy |
| **SQL injection / XSS** | Niskie | Krytyczny | SQLAlchemy ORM (parametryzacja), Jinja2 autoescape, CSP headers, regularne SAST |
| **Brak idempotencji API** | Średnie | Średni | Wymagany nagłówek `Idempotency-Key`, cache odpowiedzi 24h |

### 9.2. Ryzyka regulacyjne

| Ryzyko | Mitygacja |
|---|---|
| **Zmiana wymogów FSA** | Modułowa architektura raportów; subskrypcja newslettera FSA; review compliance kwartalny |
| **Audyt SALSA z negatywnym wynikiem** | Pre-audit wewnętrzny przed Fazą 4; checklisty zgodne 1:1 ze standardem |
| **GDPR — dane osobowe operatorów** | DPIA przed wdrożeniem; minimalizacja danych; retention policy; prawo do usunięcia (z wyłączeniem audit trail z uzasadnieniem prawnym) |
| **Brak podpisu elektronicznego zgodnego z eIDAS** | TOTP + audit trail jako rozsądny zamiennik dla wewnętrznych procesów; dla raportów FSA — opcja eksportu i podpisu kwalifikowanego poza systemem |

### 9.3. Ryzyka operacyjne

| Ryzyko | Mitygacja |
|---|---|
| **Opór pracowników (papier vs system)** | Champions program — 1 ambasador per zmiana; szkolenia w języku natywnym; UX testowany z prawdziwymi operatorami |
| **Bariera językowa** | Pełne PL/EN; piktogramy + kolory dla najczęstszych akcji; instrukcja wideo |
| **Operatorzy zgłaszają fałszywe alerty (gaming KPI)** | Audyt korelacji manual vs IoT; wymagane zdjęcie + podpis przy zgłoszeniu; review przez QA |
| **Nieprawidłowo skonfigurowane triggery (false positives)** | Tryb „dry-run" przy aktywacji nowego triggera (logowanie bez akcji przez 7 dni); panel statystyk false-positive rate |
| **Single point of failure — admin** | Min. 2 administratorzy; eskalacja do dostawcy w SLA; runbooks |

### 9.4. Ryzyka biznesowe

| Ryzyko | Mitygacja |
|---|---|
| **Przekroczenie budżetu** | Faza MVP z jasno ograniczonym scopem; review po każdej fazie; rezerwa 15% |
| **Vendor lock-in** | Tylko open-source w core stacku (Flask, PostgreSQL, Redis, Mosquitto); dokumentacja architektury i runbooks |
| **Odejście kluczowego dewelopera** | Pair programming, code review, wewnętrzna dokumentacja, brak „bus factor = 1" |

### 9.5. Plan ciągłości działania (BCP)

- **Backup:** PostgreSQL pg_basebackup co 4h + WAL archiving co 5 min → S3 z retention 90 dni.
- **Disaster Recovery:** Restore tested co kwartał; RPO 5 min, RTO 1h.
- **Tryb degradowany:** Jeśli baza niedostępna — operator może wypełnić papierowy formularz (wzór wydrukowany z systemu); manualny import po przywróceniu (z audit trail oznaczonym `recovery=true`).

---

## Dodatek A — Słownik terminów

| Termin | Definicja |
|---|---|
| **CCP** | Critical Control Point — krytyczny punkt kontroli wg HACCP |
| **HACCP** | Hazard Analysis and Critical Control Points — system bezpieczeństwa żywności |
| **SALSA** | Safe And Local Supplier Approval — UK system certyfikacji małych dostawców żywności |
| **FSA** | Food Standards Agency — brytyjski regulator żywności |
| **NCR** | Non-Conformity Report — raport niezgodności |
| **FPY** | First Pass Yield — % partii produkowanych poprawnie za pierwszym razem |
| **MTTR** | Mean Time To Resolve — średni czas rozwiązania |
| **SLA** | Service Level Agreement — umowa o poziomie usług / czas reakcji |
| **PWA** | Progressive Web App — aplikacja webowa offline-capable |
| **MQTT** | Message Queuing Telemetry Transport — protokół IoT |
| **RBAC** | Role-Based Access Control — kontrola dostępu oparta o role |
| **Pipeline** | Sekwencja etapów obsługi zgłoszenia |
| **Trigger** | Reguła wykrywająca warunek w danych |
| **Responder** | Akcja wykonywana w odpowiedzi na trigger |

## Dodatek B — Powiązane dokumenty

- `02-diagramy-architektury.md` — szczegółowe diagramy techniczne
- `README.md` — punkt wejścia do dokumentacji

---

*Dokument przygotowany przez zespół: Specjalista QMS, Python Developer, QA Specialist (UK Bakery), UX/UI Designer.*
