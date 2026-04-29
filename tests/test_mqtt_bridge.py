"""MQTT bridge: parser + integration with the trigger engine.

These tests do not connect to a real broker. The runtime client is
glanced at separately in `test_make_client_constructs_paho`; everything
else exercises pure parsing and the in-process `handle_message` path."""

from __future__ import annotations

from app.models import Ticket, TriggerExecution
from app.mqtt.bridge import handle_message, make_client, parse_message


def test_parse_json_object_with_value_key():
    p = parse_message("factory/LINE_A/oven_1/temperature", b'{"value": 232.5}')
    assert p == {
        "metric": "temperature",
        "temperature": 232.5,
        "scope": "line:LINE_A",
        "line_code": "LINE_A",
        "device_id": "oven_1",
        "source": "iot",
    }


def test_parse_json_object_with_metric_key():
    p = parse_message("factory/LINE_A/oven_1/temperature", b'{"temperature": 218.0}')
    assert p is not None
    assert p["temperature"] == 218.0


def test_parse_bare_json_number():
    p = parse_message("factory/LINE_B/scale_2/weight", b"482.1")
    assert p is not None
    assert p["weight"] == 482.1
    assert p["scope"] == "line:LINE_B"
    assert p["device_id"] == "scale_2"


def test_parse_string_payload_accepted():
    p = parse_message("factory/LINE_A/oven_1/temperature", "230.0")
    assert p is not None
    assert p["temperature"] == 230.0


def test_parse_int_value_coerced_to_float():
    p = parse_message("factory/LINE_A/oven_1/temperature", b'{"value": 220}')
    assert p is not None
    assert isinstance(p["temperature"], float)
    assert p["temperature"] == 220.0


def test_parse_rejects_wrong_topic_depth():
    assert parse_message("factory/LINE_A/oven_1", b'{"value": 1}') is None
    assert parse_message("foo/bar/baz/qux", b'{"value": 1}') is None
    assert parse_message("factory/LINE_A/oven_1/temp/extra", b'{"value": 1}') is None


def test_parse_rejects_empty_segments():
    assert parse_message("factory//oven_1/temperature", b'{"value": 1}') is None
    assert parse_message("factory/LINE_A//temperature", b'{"value": 1}') is None
    assert parse_message("factory/LINE_A/oven_1/", b'{"value": 1}') is None


def test_parse_rejects_non_numeric_payload():
    assert parse_message("factory/LINE_A/oven_1/temperature", b"not-a-number") is None
    assert parse_message("factory/LINE_A/oven_1/temperature", b'{"value": "hot"}') is None
    assert parse_message("factory/LINE_A/oven_1/temperature", b'{"value": true}') is None
    assert parse_message("factory/LINE_A/oven_1/temperature", b"") is None


def test_parse_rejects_missing_value_field():
    assert parse_message("factory/LINE_A/oven_1/temperature", b'{"other": 1}') is None
    assert parse_message("factory/LINE_A/oven_1/temperature", b'{}') is None


def test_handle_message_fires_seeded_trigger(app):
    fired = handle_message(
        app, "factory/LINE_A/oven_1/temperature", b'{"value": 232.5}'
    )
    assert fired == 1
    with app.app_context():
        executions = TriggerExecution.query.all()
        assert len(executions) == 1
        # The CREATE_TICKET responder on OVEN1_OVERHEAT should have produced one ticket.
        tickets = Ticket.query.all()
        assert len(tickets) == 1
        assert tickets[0].is_ccp_related is False  # trigger-created, not CCP path


def test_handle_message_below_threshold_does_not_fire(app):
    fired = handle_message(
        app, "factory/LINE_A/oven_1/temperature", b'{"value": 180.0}'
    )
    assert fired == 0
    with app.app_context():
        assert TriggerExecution.query.count() == 0
        assert Ticket.query.count() == 0


def test_handle_message_unparseable_topic_returns_zero(app):
    assert handle_message(app, "garbage", b"1") == 0
    assert handle_message(app, "factory/LINE_A/oven_1/temperature", b"oops") == 0
    with app.app_context():
        assert TriggerExecution.query.count() == 0


def test_handle_message_wrong_scope_does_not_fire(app):
    # Trigger is scoped to LINE_A; a reading from LINE_X should be ignored.
    fired = handle_message(
        app, "factory/LINE_X/oven_99/temperature", b'{"value": 999.0}'
    )
    assert fired == 0
    with app.app_context():
        assert TriggerExecution.query.count() == 0


def test_make_client_constructs_paho(app, monkeypatch):
    """Smoke-test the client factory without opening a TCP connection."""
    import paho.mqtt.client as mqtt

    connected: dict[str, object] = {}

    def fake_connect(self, host, port, keepalive):
        connected["host"] = host
        connected["port"] = port
        connected["keepalive"] = keepalive

    monkeypatch.setattr(mqtt.Client, "connect", fake_connect)

    client = make_client(app, broker_host="broker.test", broker_port=1884)
    assert isinstance(client, mqtt.Client)
    assert connected == {"host": "broker.test", "port": 1884, "keepalive": 60}
