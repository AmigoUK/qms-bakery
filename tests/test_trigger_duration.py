"""Duration-window trigger gating.

Covers the `trigger_state` Redis state machine in isolation, then the
integration via `trigger_service.evaluate()` with a custom trigger that
carries `duration_seconds` in its condition JSON.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time

from app.extensions import db
from app.models import Ticket, TriggerExecution
from app.models.production import ProductionLine
from app.models.triggers import (
    Responder,
    ResponderType,
    Trigger,
    trigger_responders,
)
from app.services import trigger_state, triggers as trigger_service


# -- pure gate logic ----------------------------------------------------


def test_first_true_records_state_and_does_not_fire(redis_client):
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    fired = trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=now, redis_client=redis_client
    )
    assert fired is False
    raw = redis_client.get("trigger_state:trig-1:line:LINE_A:first_true")
    assert raw is not None


def test_second_true_within_duration_does_not_fire(redis_client):
    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=base, redis_client=redis_client
    )
    fired = trigger_state.should_fire_with_duration(
        "trig-1",
        "line:LINE_A",
        30,
        now=base + timedelta(seconds=10),
        redis_client=redis_client,
    )
    assert fired is False
    # Key still present — same first-true timestamp
    assert redis_client.get("trigger_state:trig-1:line:LINE_A:first_true") is not None


def test_true_after_duration_fires_and_clears_state(redis_client):
    base = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=base, redis_client=redis_client
    )
    fired = trigger_state.should_fire_with_duration(
        "trig-1",
        "line:LINE_A",
        30,
        now=base + timedelta(seconds=30),
        redis_client=redis_client,
    )
    assert fired is True
    assert redis_client.get("trigger_state:trig-1:line:LINE_A:first_true") is None


def test_reset_clears_state(redis_client):
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=now, redis_client=redis_client
    )
    trigger_state.reset_duration_state(
        "trig-1", "line:LINE_A", redis_client=redis_client
    )
    assert redis_client.get("trigger_state:trig-1:line:LINE_A:first_true") is None


def test_scope_isolation(redis_client):
    """Same trigger, different scopes → independent first-true timestamps."""
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=now, redis_client=redis_client
    )
    fired_b = trigger_state.should_fire_with_duration(
        "trig-1",
        "line:LINE_B",
        30,
        now=now + timedelta(seconds=60),
        redis_client=redis_client,
    )
    # LINE_B is fresh — first true, no fire even though wall-clock has advanced
    assert fired_b is False


def test_state_key_carries_ttl(redis_client):
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    trigger_state.should_fire_with_duration(
        "trig-1", "line:LINE_A", 30, now=now, redis_client=redis_client
    )
    ttl = redis_client.ttl("trigger_state:trig-1:line:LINE_A:first_true")
    # TTL is 3×duration, capped at min 60
    assert 60 <= ttl <= 90


# -- integration via evaluate() ----------------------------------------


def _make_duration_trigger(line_code: str = "LINE_A", duration: int = 30) -> Trigger:
    """Add a temperature-overheat trigger with duration_seconds and one
    CREATE_TICKET responder. Mirrors the seeded OVEN1_OVERHEAT shape."""
    line = ProductionLine.query.filter_by(code=line_code).first()
    responder = Responder(
        code="DUR_OPEN_TICKET",
        name={"en": "Duration open ticket", "pl": "Otwórz po czasie"},
        type=ResponderType.CREATE_TICKET.value,
        config={"title": "Sustained overheat: {temperature}°C"},
    )
    db.session.add(responder)
    db.session.flush()

    trigger = Trigger(
        code="OVEN1_SUSTAINED",
        name={"en": "Oven sustained overheat", "pl": "Trwałe przegrzanie"},
        scope=f"line:{line.code}",
        condition={
            "metric": "temperature",
            "operator": ">",
            "value": 220,
            "duration_seconds": duration,
        },
        severity="high",
        is_active=True,
    )
    db.session.add(trigger)
    db.session.flush()
    db.session.execute(
        trigger_responders.insert(),
        [{"trigger_id": trigger.id, "responder_id": responder.id, "order_index": 0}],
    )
    db.session.flush()
    return trigger


def test_evaluate_does_not_fire_on_first_overheat_when_duration_set(app):
    with app.app_context():
        trigger = _make_duration_trigger(duration=30)
        line = ProductionLine.query.filter_by(code="LINE_A").first()

        with freeze_time("2026-04-29 12:00:00"):
            fired = trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 232.5,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )
        # Custom OVEN1_SUSTAINED gated; seeded OVEN1_OVERHEAT (no duration) fires
        # immediately.
        fired_codes = {db.session.get(Trigger, e.trigger_id).code for e in fired}
        assert trigger.code not in fired_codes
        assert "OVEN1_OVERHEAT" in fired_codes


def test_evaluate_fires_after_duration_elapsed(app):
    with app.app_context():
        trigger = _make_duration_trigger(duration=30)
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        # Disable the seeded immediate trigger so we can isolate counts.
        seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        seeded.is_active = False
        db.session.flush()

        with freeze_time("2026-04-29 12:00:00"):
            trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 232.5,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )
        with freeze_time("2026-04-29 12:00:30"):
            fired = trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 232.5,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )

        db.session.commit()
        assert len(fired) == 1
        execution = TriggerExecution.query.filter_by(trigger_id=trigger.id).one()
        assert execution.success is True
        assert Ticket.query.count() == 1


def test_evaluate_resets_state_when_condition_flips_false(app, redis_client):
    with app.app_context():
        trigger = _make_duration_trigger(duration=30)
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        seeded.is_active = False
        db.session.flush()

        with freeze_time("2026-04-29 12:00:00"):
            trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 232.5,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )
        # State key should now be set
        key = f"trigger_state:{trigger.id}:line:LINE_A:first_true"
        assert redis_client.get(key) is not None

        # Condition flips false → state must be cleared
        with freeze_time("2026-04-29 12:00:10"):
            trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 180.0,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )
        assert redis_client.get(key) is None

        # Now even a high reading 31s after the original first-true must NOT
        # fire — the timer restarted.
        with freeze_time("2026-04-29 12:00:31"):
            fired = trigger_service.evaluate(
                {
                    "metric": "temperature",
                    "temperature": 232.5,
                    "scope": "line:LINE_A",
                    "line_id": line.id,
                }
            )
        assert len(fired) == 0
        assert TriggerExecution.query.filter_by(trigger_id=trigger.id).count() == 0


def test_no_duration_field_keeps_immediate_fire_behaviour(app):
    """Regression: the seeded OVEN1_OVERHEAT (no duration_seconds) still
    fires on the first reading that breaches the threshold."""
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 232.5,
                "scope": "line:LINE_A",
                "line_id": line.id,
            }
        )
        codes = {db.session.get(Trigger, e.trigger_id).code for e in fired}
        assert "OVEN1_OVERHEAT" in codes
