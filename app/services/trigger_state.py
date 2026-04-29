"""Duration-window state for triggers with `duration_seconds`.

Keyed in Redis as `trigger_state:<trigger_id>:<scope>:first_true`, holding
the ISO-formatted timestamp of the first reading where the trigger's
condition went True. The key carries a TTL of 3×duration so stale state
auto-expires when readings stop arriving (e.g. after a sensor reboot).

Per-scope isolation: a duration-gated trigger scoped to `line:LINE_A`
maintains independent state from the same condition on `line:LINE_B`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.stream import get_redis


def _key(trigger_id: str, scope: str | None) -> str:
    return f"trigger_state:{trigger_id}:{scope or 'global'}:first_true"


def should_fire_with_duration(
    trigger_id: str,
    scope: str | None,
    duration_seconds: int,
    *,
    now: datetime | None = None,
    redis_client=None,
) -> bool:
    """Apply duration-window gating; call only when the condition is True now.

    Returns True iff the condition has been continuously True for at least
    `duration_seconds`. Side-effect: maintains the first-true Redis key.
    On fire, the key is cleared so the next continuous violation is timed
    from scratch.
    """
    r = redis_client if redis_client is not None else get_redis()
    now = now or datetime.now(timezone.utc)
    key = _key(trigger_id, scope)

    raw = r.get(key)
    if raw is None:
        ttl = max(int(duration_seconds) * 3, 60)
        r.setex(key, ttl, now.isoformat())
        return False

    value = raw if isinstance(raw, str) else raw.decode("utf-8")
    first_true = datetime.fromisoformat(value)
    elapsed = (now - first_true).total_seconds()
    if elapsed >= duration_seconds:
        r.delete(key)
        return True
    return False


def reset_duration_state(
    trigger_id: str, scope: str | None, *, redis_client=None
) -> None:
    """Clear the first-true state when the condition flips back to False."""
    r = redis_client if redis_client is not None else get_redis()
    r.delete(_key(trigger_id, scope))
