# Load test harness

Locust suite that validates the QMS Phase-3 throughput target:
**200 tickets/min sustained on `/api/v1/measurements`** with P95 latency
under 250 ms and no 5xx / no 429s (when the rate-limit bucket is sized
for the test).

## What it covers

| User class | Endpoint(s) | Purpose |
|---|---|---|
| `LoadtestApi` | `POST /api/v1/measurements` (HMAC-signed) | Exercises the synchronous trigger-eval + ticket-create path. 30% of requests carry a deviating reading. |
| `DashboardBrowser` | `GET /auth/login`, `POST /auth/login`, `GET /tickets`, `GET /` | Realistic concurrent read traffic from an operator logged into the UI. |

Health endpoint hits are mixed in so you can sanity-check liveness during the run.

## Prerequisites

1. **Install Locust** in your dev venv:
   ```bash
   uv pip install locust
   ```
2. **Seed an API key** on the target instance (`API_KEYS` is a config dict, not a table — see `app/blueprints/api.py`):
   ```bash
   export API_KEYS_LOADTEST=loadtest-secret  # or wire via env-loader
   ```
   For a quick local run, drop into `flask shell` and assign `current_app.config["API_KEYS"] = {"loadtest": "loadtest-secret"}` before booting Locust.
3. **Lift the rate limit** for the source IP, or the test will measure 429 throughput rather than real performance:
   ```bash
   export RATELIMIT_API_MAX=100000
   ```
4. **Seed at least one trigger** (optional but recommended) so deviating payloads create tickets — otherwise you measure only the no-match branch.

## Running

### Smoke (baseline latency)
```bash
locust -f loadtest/locustfile.py --headless \
    -u 1 -r 1 -t 30s \
    --host http://localhost:8000 \
    LoadtestApi
```
Expected: P95 well under 100 ms, 0 failures.

### Soak (200 tickets/min target)
```bash
LOCUST_API_KEY=loadtest \
LOCUST_API_SECRET=loadtest-secret \
locust -f loadtest/locustfile.py --headless \
    -u 50 -r 5 -t 5m \
    --host http://localhost:8000 \
    LoadtestApi DashboardBrowser
```
50 users × ~5 req/s ≈ 250 req/s headroom, 30 % deviating ≈ 75 tickets/s
peak — well above the 200/min target. Pass/fail criteria:
- P95 `POST /api/v1/measurements` < 250 ms
- 0 × 5xx
- 0 × 429 (otherwise raise `RATELIMIT_API_MAX`)
- Dashboard P95 < 1.5 s

### Web UI (interactive)
Drop the `--headless -t -u -r` flags and visit `http://localhost:8089`.

## Interpreting results

- **High latency, low throughput** → DB is the bottleneck. Check the trigger-evaluation query path (`app/services/triggers.py`); the synchronous `evaluate()` runs N×M filters per POST.
- **Spike of 5xx** → likely a worker queue / Redis saturation. Confirm `flask rq-worker` is running and the DLQ is empty (`/admin/dlq`).
- **Sustained 429** → `RATELIMIT_API_MAX` too low for the test rate. Bump it for the loadtest IP or disable rate limit for the run.
- **Deviating payloads not creating tickets** → seed a trigger that matches `metric=temperature`, `temperature > 4.0`, scope `line:LINE_A`. Check `app/blueprints/admin.py:triggers_*` for the form.

## CI smoke

The smoke profile (1u/30s) takes < 60 s and is suitable for a nightly
job. Soak runs are operator-initiated and want a fresh database to
keep ticket counts comparable across runs.
