"""MQTT bridge: subscribes to factory device topics and feeds readings into
the trigger engine.

Topic schema (from 02-architecture-diagrams.md):
    factory/<line_code>/<device_id>/<metric>

Payload formats accepted:
    - JSON object  {"value": 232.5}
    - JSON object  {"<metric>": 232.5}            (metric-keyed)
    - JSON number  232.5
    - Bare ASCII   "232.5"

The parser is pure (`parse_message`) so it can be unit-tested without a
broker. The runtime entry-point is `run(app)`, which is wired to the
`flask mqtt-bridge` CLI command.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.extensions import db
from app.services import triggers as trigger_service

logger = logging.getLogger(__name__)

DEFAULT_TOPIC_FILTER = "factory/+/+/+"


def parse_message(topic: str, payload: bytes | str) -> dict[str, Any] | None:
    """Translate an MQTT message into a trigger-engine payload.

    Returns None if the topic shape or value is unparseable.
    """
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "factory":
        return None
    _, line_code, device_id, metric = parts
    if not (line_code and device_id and metric):
        return None

    raw = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    raw = raw.strip()
    if not raw:
        return None

    value: float | int | None = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            candidate = data.get("value")
            if candidate is None:
                candidate = data.get(metric)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                value = candidate
        elif isinstance(data, (int, float)) and not isinstance(data, bool):
            value = data
    except json.JSONDecodeError:
        try:
            value = float(raw)
        except ValueError:
            return None

    if value is None:
        return None

    return {
        "metric": metric,
        metric: float(value),
        "scope": f"line:{line_code}",
        "line_code": line_code,
        "device_id": device_id,
        "source": "iot",
    }


def _evaluate_in_context(app, parsed: dict[str, Any]) -> int:
    """Apply a parsed reading to the trigger engine inside the app context.

    Shared by the synchronous `handle_message` path and the worker that
    reads from the Redis Stream. Returns the number of triggers that
    fired; returns 0 on internal failure and rolls back the session.
    """
    with app.app_context():
        from app.models.production import ProductionLine

        line = ProductionLine.query.filter_by(code=parsed["line_code"]).first()
        if line is not None:
            parsed["line_id"] = line.id
        try:
            fired = trigger_service.evaluate(parsed)
            db.session.commit()
            return len(fired)
        except Exception:
            db.session.rollback()
            logger.exception(
                "MQTT trigger evaluation failed: line_code=%s metric=%s",
                parsed.get("line_code"),
                parsed.get("metric"),
            )
            return 0


def handle_message(app, topic: str, payload: bytes) -> int:
    """Synchronous in-process path: parse → evaluate.

    Used by tests and as the no-Redis fallback. Production routes through
    `enqueue_message` + the trigger worker instead.
    """
    parsed = parse_message(topic, payload)
    if parsed is None:
        logger.warning("MQTT message dropped (unparseable): topic=%s", topic)
        return 0
    return _evaluate_in_context(app, parsed)


def enqueue_message(app, topic: str, payload: bytes) -> str | None:
    """Async path: parse the MQTT message and XADD it to the Redis Stream.

    Returns the stream entry ID, or None on parse failure or publish error.
    The bridge does not block on trigger evaluation — that work happens in
    the `flask trigger-worker` consumer.
    """
    parsed = parse_message(topic, payload)
    if parsed is None:
        logger.warning("MQTT message dropped (unparseable): topic=%s", topic)
        return None
    from app.services import stream as stream_service

    try:
        with app.app_context():
            return stream_service.publish_reading(parsed, app=app)
    except Exception:
        logger.exception("Failed to publish MQTT reading to stream: topic=%s", topic)
        return None


def make_client(
    app,
    *,
    broker_host: str,
    broker_port: int = 1883,
    topic_filter: str = DEFAULT_TOPIC_FILTER,
    client_id: str | None = None,
    username: str | None = None,
    password: str | None = None,
    keepalive: int = 60,
):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id or "qms-bridge",
    )
    if username:
        client.username_pw_set(username, password)

    def on_connect(c, _userdata, _flags, reason_code, _properties=None):
        if int(reason_code) == 0:
            c.subscribe(topic_filter, qos=1)
            logger.info("MQTT connected; subscribed to %s", topic_filter)
        else:
            logger.error("MQTT connect failed: rc=%s", reason_code)

    use_stream = bool(app.config.get("MQTT_USE_STREAM", True))

    def on_message(_c, _userdata, msg):
        if use_stream:
            enqueue_message(app, msg.topic, msg.payload)
        else:
            handle_message(app, msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker_host, broker_port, keepalive=keepalive)
    return client


def run(app) -> None:
    cfg = app.config
    client = make_client(
        app,
        broker_host=cfg["MQTT_BROKER_HOST"],
        broker_port=int(cfg.get("MQTT_BROKER_PORT", 1883)),
        topic_filter=cfg.get("MQTT_TOPIC_FILTER", DEFAULT_TOPIC_FILTER),
        client_id=cfg.get("MQTT_CLIENT_ID"),
        username=cfg.get("MQTT_USERNAME"),
        password=cfg.get("MQTT_PASSWORD"),
    )
    logger.info(
        "MQTT bridge starting: broker=%s:%s",
        cfg["MQTT_BROKER_HOST"],
        cfg.get("MQTT_BROKER_PORT", 1883),
    )
    client.loop_forever()
