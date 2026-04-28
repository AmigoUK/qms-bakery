from __future__ import annotations

import os
from typing import Any

from flask import Flask, g, redirect, request, url_for

from app.extensions import csrf, db, login_manager
from app.i18n import init_i18n


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    app.config.from_mapping(_default_config())
    if config:
        app.config.update(config)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    init_i18n(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return db.session.get(User, user_id)

    @login_manager.unauthorized_handler
    def _unauthorized():
        return redirect(url_for("auth.login", next=request.path))

    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.haccp import bp as haccp_bp
    from app.blueprints.tickets import bp as tickets_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/")
    app.register_blueprint(tickets_bp, url_prefix="/tickets")
    app.register_blueprint(haccp_bp, url_prefix="/haccp")

    @app.cli.command("init-db")
    def _init_db_cmd():
        from app.seeds import seed_initial

        with app.app_context():
            db.create_all()
            seed_initial()

    @app.context_processor
    def _inject_globals():
        return {"current_lang": g.get("lang", app.config["DEFAULT_LANGUAGE"])}

    if app.config.get("AUTO_CREATE_TABLES"):
        with app.app_context():
            db.create_all()

    return app


def _default_config() -> dict[str, Any]:
    db_url = os.environ.get("DATABASE_URL", "sqlite:///qms.db")
    return {
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        "SQLALCHEMY_DATABASE_URI": db_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "DEFAULT_LANGUAGE": os.environ.get("DEFAULT_LANGUAGE", "en"),
        "SUPPORTED_LANGUAGES": tuple(
            os.environ.get("SUPPORTED_LANGUAGES", "pl,en").split(",")
        ),
        "WTF_CSRF_ENABLED": True,
        "WTF_CSRF_TIME_LIMIT": 3600,
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * int(
            os.environ.get("SESSION_LIFETIME_HOURS", "8")
        ),
        "BCRYPT_LOG_ROUNDS": int(os.environ.get("BCRYPT_LOG_ROUNDS", "12")),
        "LOCKOUT_THRESHOLD": int(os.environ.get("LOCKOUT_THRESHOLD", "5")),
        "LOCKOUT_MINUTES": int(os.environ.get("LOCKOUT_MINUTES", "15")),
        "AUTO_CREATE_TABLES": False,
    }
