from __future__ import annotations

import os
from typing import Any

from flask import Flask, g, redirect, request, url_for

from app.extensions import csrf, db, login_manager, migrate
from app.i18n import init_i18n
from app.security_headers import init_security_headers


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    app.config.from_mapping(_default_config())
    if config:
        app.config.update(config)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "SECRET_KEY is required. Set the SECRET_KEY environment variable "
            "(generate one with: python -c \"import secrets; print(secrets.token_hex(32))\")"
        )

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    init_i18n(app)
    init_security_headers(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return db.session.get(User, user_id)

    @login_manager.unauthorized_handler
    def _unauthorized():
        return redirect(url_for("auth.login", next=request.path))

    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.admin_training import bp as admin_training_bp
    from app.blueprints.api import bp as api_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.haccp import bp as haccp_bp
    from app.blueprints.health import bp as health_bp
    from app.blueprints.pwa import bp as pwa_bp
    from app.blueprints.reports import bp as reports_bp
    from app.blueprints.salsa import bp as salsa_bp
    from app.blueprints.tickets import bp as tickets_bp
    from app.blueprints.training import bp as training_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/")
    app.register_blueprint(tickets_bp, url_prefix="/tickets")
    app.register_blueprint(haccp_bp, url_prefix="/haccp")
    app.register_blueprint(salsa_bp, url_prefix="/salsa")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(admin_training_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(health_bp)
    app.register_blueprint(pwa_bp)
    app.register_blueprint(training_bp)

    @app.cli.command("init-db")
    def _init_db_cmd():
        from flask_migrate import upgrade

        from app.seeds import seed_initial

        admin_email = os.environ.get("INITIAL_ADMIN_EMAIL")
        admin_password = os.environ.get("INITIAL_ADMIN_PASSWORD")
        if not admin_email or not admin_password:
            raise RuntimeError(
                "INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD must be set "
                "before running `flask init-db` — refusing to seed a default admin."
            )

        with app.app_context():
            upgrade()
            seed_initial(admin_email=admin_email, admin_password=admin_password)

    @app.cli.command("mqtt-bridge")
    def _mqtt_bridge_cmd():
        from app.mqtt.bridge import run

        run(app)

    @app.cli.command("trigger-worker")
    def _trigger_worker_cmd():
        from app.workers.trigger_worker import run as run_worker

        run_worker(app)

    @app.cli.command("rq-worker")
    def _rq_worker_cmd():
        from app.workers.rq_worker import run as run_rq

        run_rq(app)

    @app.context_processor
    def _inject_globals():
        from app.permissions import Perm

        return {
            "current_lang": g.get("lang", app.config["DEFAULT_LANGUAGE"]),
            "Perm": Perm,
        }

    if app.config.get("AUTO_CREATE_TABLES"):
        with app.app_context():
            db.create_all()

    _warn_unsafe_defaults(app)

    return app


def _warn_unsafe_defaults(app: Flask) -> None:
    """Log a warning when production-shaped config still holds dev
    placeholders. Doesn't block startup — TESTING/DEBUG environments
    legitimately use these values."""
    if app.config.get("TESTING") or app.config.get("DEBUG"):
        return
    warnings: list[str] = []
    if app.config.get("SMTP_FROM") in ("qms@local", "qms@example.invalid"):
        warnings.append(
            f"SMTP_FROM={app.config['SMTP_FROM']!r} is a placeholder — outbound "
            "email will fail or be marked spam in production"
        )
    if app.config.get("SECRET_KEY") and len(app.config["SECRET_KEY"]) < 16:
        warnings.append("SECRET_KEY is shorter than 16 chars — generate a longer one")
    if not app.config.get("SESSION_COOKIE_SECURE"):
        warnings.append(
            "SESSION_COOKIE_SECURE is False — sessions can leak over HTTP"
        )
    for w in warnings:
        app.logger.warning("config-warning: %s", w)


def _default_config() -> dict[str, Any]:
    db_url = os.environ.get("DATABASE_URL", "sqlite:///qms.db")
    return {
        "SECRET_KEY": os.environ.get("SECRET_KEY"),
        "SQLALCHEMY_DATABASE_URI": db_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "DEFAULT_LANGUAGE": os.environ.get("DEFAULT_LANGUAGE", "en"),
        "SUPPORTED_LANGUAGES": tuple(
            os.environ.get("SUPPORTED_LANGUAGES", "pl,en").split(",")
        ),
        "WTF_CSRF_ENABLED": True,
        "WTF_CSRF_TIME_LIMIT": int(os.environ.get("WTF_CSRF_TIME_LIMIT", "3600")),
        "LANGUAGE_COOKIE_MAX_AGE": int(
            os.environ.get("LANGUAGE_COOKIE_MAX_AGE", str(60 * 60 * 24 * 365))
        ),
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * int(
            os.environ.get("SESSION_LIFETIME_HOURS", "8")
        ),
        # Cookie hardening. Secure defaults to True in prod; flip via
        # SESSION_COOKIE_SECURE=0 in pure-HTTP dev. SameSite=Lax keeps
        # the session cookie on top-level GET navigations (so the
        # post-login redirect chain works) but blocks it on cross-site
        # POSTs — which combined with our CSRF tokens stops CSRF cold.
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": os.environ.get("SESSION_COOKIE_SECURE", "1") not in ("0", "false", "False"),
        "REMEMBER_COOKIE_HTTPONLY": True,
        "REMEMBER_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_SECURE": os.environ.get("SESSION_COOKIE_SECURE", "1") not in ("0", "false", "False"),
        "BCRYPT_LOG_ROUNDS": int(os.environ.get("BCRYPT_LOG_ROUNDS", "12")),
        # Label shown by Authenticator apps. Doesn't affect verification
        # — already-enrolled users still work after a rebrand.
        "TOTP_ISSUER": os.environ.get("TOTP_ISSUER", "QMS"),
        "LOCKOUT_THRESHOLD": int(os.environ.get("LOCKOUT_THRESHOLD", "5")),
        "LOCKOUT_MINUTES": int(os.environ.get("LOCKOUT_MINUTES", "15")),
        "AUTO_CREATE_TABLES": False,
        # API keys for external integrations: {key_id: secret}
        "API_KEYS": {},
        "SECURITY_HEADERS_ENABLED": True,
        "HSTS_MAX_AGE_SECONDS": int(os.environ.get("HSTS_MAX_AGE_SECONDS", "31536000")),
        "RATELIMIT_ENABLED": os.environ.get("RATELIMIT_ENABLED", "1") not in ("0", "false", "False"),
        "RATELIMIT_API_MAX": int(os.environ.get("RATELIMIT_API_MAX", "600")),
        "RATELIMIT_LOGIN_MAX": int(os.environ.get("RATELIMIT_LOGIN_MAX", "10")),
        "RATELIMIT_LOGIN_WINDOW": int(os.environ.get("RATELIMIT_LOGIN_WINDOW", "60")),
        "MQTT_BROKER_HOST": os.environ.get("MQTT_BROKER_HOST", "localhost"),
        "MQTT_BROKER_PORT": int(os.environ.get("MQTT_BROKER_PORT", "1883")),
        "MQTT_TOPIC_FILTER": os.environ.get("MQTT_TOPIC_FILTER", "factory/+/+/+"),
        "MQTT_CLIENT_ID": os.environ.get("MQTT_CLIENT_ID", "qms-bridge"),
        "MQTT_USERNAME": os.environ.get("MQTT_USERNAME"),
        "MQTT_PASSWORD": os.environ.get("MQTT_PASSWORD"),
        "MQTT_USE_STREAM": os.environ.get("MQTT_USE_STREAM", "1") not in ("0", "false", "False"),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        # Tests inject text-mode (`decode_responses=True`) and binary
        # fakeredis clients sharing a single FakeServer, so stream + RQ
        # code paths run without a real broker.
        "REDIS_CLIENT": None,
        "REDIS_BINARY_CLIENT": None,
        "SMTP_HOST": os.environ.get("SMTP_HOST", "localhost"),
        "SMTP_PORT": int(os.environ.get("SMTP_PORT", "25")),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME"),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD"),
        "SMTP_USE_TLS": os.environ.get("SMTP_USE_TLS", "0") not in ("0", "false", "False"),
        "SMTP_FROM": os.environ.get("SMTP_FROM", "qms@example.invalid"),
        "CLICKSEND_USERNAME": os.environ.get("CLICKSEND_USERNAME", ""),
        "CLICKSEND_API_KEY": os.environ.get("CLICKSEND_API_KEY", ""),
        "CLICKSEND_SOURCE": os.environ.get("CLICKSEND_SOURCE", "QMS"),
        "CLICKSEND_BASE_URL": os.environ.get(
            "CLICKSEND_BASE_URL", "https://rest.clicksend.com/v3"
        ),
        # Training feature config
        "TRAINING_DEFAULT_LINK_TTL_DAYS": int(
            os.environ.get("TRAINING_DEFAULT_LINK_TTL_DAYS", "7")
        ),
        "TRAINING_DEFAULT_PASS_THRESHOLD": float(
            os.environ.get("TRAINING_DEFAULT_PASS_THRESHOLD", "0.7")
        ),
        # Distinct from SECRET_KEY so it can be rotated independently
        # to invalidate every outstanding magic link without disturbing
        # web sessions. Falls back to SECRET_KEY for dev convenience.
        "TRAINING_LINK_SIGNING_KEY": os.environ.get("TRAINING_LINK_SIGNING_KEY"),
        # Public base URL embedded in SMS bodies (e.g. "https://qms.example.com")
        "TRAINING_BASE_URL": os.environ.get("TRAINING_BASE_URL", ""),
    }
