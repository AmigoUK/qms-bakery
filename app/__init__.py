from __future__ import annotations

import os
from typing import Any

import click
from flask import Flask, flash, g, redirect, request, url_for
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from app.extensions import csrf, db, login_manager, migrate
from app.i18n import gettext as _l
from app.i18n import init_i18n
from app.security_headers import init_security_headers

# Section-classifier for the topbar's three-way grouping. The
# active-state matcher uses longest-prefix lookup; order matters
# (admin.audit_* must beat admin.* so the Audit Log highlights
# Compliance, not System). Module-level so tests can import it.
_SECTION_FOR_PREFIX: list[tuple[str, str]] = [
    ("admin.audit_", "compliance"),
    ("admin_training.", "compliance"),
    ("reports.", "compliance"),
    ("tickets.", "operations"),
    ("haccp.", "operations"),
    ("salsa.", "operations"),
    ("admin.", "system"),
]


def _classify_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    for prefix, section in _SECTION_FOR_PREFIX:
        if endpoint.startswith(prefix):
            return section
    return None


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

    # When deployed behind a trusted reverse proxy (Caddy / Nginx /
    # Tailnet), honour the X-Forwarded-* headers up to the configured
    # depth. Without this, a client could spoof X-Forwarded-For to
    # bucket every request under a fresh identity and bypass the
    # rate-limiter on /auth/login and /api/v1/measurements. Set
    # TRUSTED_PROXY_HOPS=0 (default) to keep request.remote_addr at the
    # raw socket peer for direct-exposed deployments.
    proxy_hops = app.config.get("TRUSTED_PROXY_HOPS", 0)
    if proxy_hops > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_hops,
            x_proto=proxy_hops,
            x_host=proxy_hops,
            x_port=proxy_hops,
            x_prefix=proxy_hops,
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
    from app.blueprints.help import bp as help_bp
    from app.blueprints.legal import bp as legal_bp
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
    app.register_blueprint(help_bp)
    app.register_blueprint(legal_bp)
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

    @app.cli.command("training-scheduler")
    def _training_scheduler_cmd():
        from app.workers.training_scheduler import run as run_sched

        run_sched(app)

    @app.cli.command("totp-rotate-keys")
    @click.option(
        "--dry-run/--apply", default=True,
        help="--dry-run reports counts only (default); --apply writes back.",
    )
    def _totp_rotate_keys_cmd(dry_run: bool):
        """Re-encrypt every users.totp_secret under the current primary key.

        Run after promoting a new TOTP_ENC_KEY (and listing the old one in
        TOTP_ENC_KEYS_OLD). Once this completes, every row is on the
        primary key and TOTP_ENC_KEYS_OLD can be dropped from the env.
        """
        from app.extensions import db
        from app.models import User
        from app.services import totp_crypto

        with app.app_context():
            rows = User.query.filter(User.totp_secret.isnot(None)).all()
            stale = []
            for user in rows:
                if not totp_crypto.is_on_primary_key(user.totp_secret):
                    stale.append(user)
            click.echo(
                f"Total TOTP-enrolled users: {len(rows)}; "
                f"on primary key: {len(rows) - len(stale)}; "
                f"to rotate: {len(stale)}"
            )
            if not stale:
                return
            if dry_run:
                click.echo("DRY RUN — re-run with --apply to rotate.")
                return
            for user in stale:
                try:
                    plaintext = totp_crypto.decrypt(user.totp_secret)
                except totp_crypto.TOTPCryptoError as exc:
                    click.echo(
                        f"  SKIP user={user.id} ({user.email}): "
                        f"undecryptable under any configured key ({exc})"
                    )
                    continue
                user.totp_secret = totp_crypto.encrypt(plaintext)
            db.session.commit()
            click.echo(f"Rotated {len(stale)} row(s).")

    @app.cli.command("retention-sweep")
    @click.option(
        "--dry-run/--apply", default=True,
        help="--dry-run reports counts only (default); --apply mutates rows.",
    )
    def _retention_sweep_cmd(dry_run: bool):
        """UK GDPR Art. 5(1)(e) — drop expired enrolments, redact stale
        declarations, redact dormant trainees, count audit rows past the
        retention horizon. Idempotent."""
        from app.services import retention

        with app.app_context():
            summary = retention.sweep(dry_run=dry_run, config=app.config)
            click.echo(f"Retention sweep ({'DRY RUN' if dry_run else 'APPLIED'}):")
            for key, value in summary.items():
                if key == "dry_run":
                    continue
                click.echo(f"  {key}: {value}")

    @app.cli.command("dsar-export")
    @click.option(
        "--subject", "subject_spec", required=True,
        help="Subject specifier: 'user:<id>' or 'trainee:<id>'.",
    )
    @click.option(
        "--output", "output_path", default="-", show_default=True,
        help="File path to write the JSON to. Use '-' for stdout.",
    )
    def _dsar_export_cmd(subject_spec: str, output_path: str):
        """UK GDPR Art. 15 / Art. 20 export of every record we hold for a
        given subject. The export itself is logged to the audit trail."""
        import json
        import sys

        from app.services import dsar

        with app.app_context():
            try:
                payload = dsar.export(subject_spec)
            except dsar.DSARError as exc:
                raise click.ClickException(str(exc)) from exc
            text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            if output_path == "-":
                sys.stdout.write(text + "\n")
            else:
                with open(output_path, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                click.echo(f"Wrote {output_path}")

    @app.cli.command("gdpr-redact")
    @click.option(
        "--subject", "subject_spec", required=True,
        help="Subject specifier: 'user:<id>' or 'trainee:<id>'.",
    )
    @click.option(
        "--confirm", is_flag=True, default=False,
        help="Required to actually run — without it, the command refuses.",
    )
    def _gdpr_redact_cmd(subject_spec: str, confirm: bool):
        """UK GDPR Art. 17 — redact a subject's personal data from audit
        diffs and source rows, rehashing the audit chain forward. The
        chain remains internally verifiable; the personal-data payload
        is replaced with a redaction marker. The action is itself
        logged to the audit trail."""
        if not confirm:
            raise click.ClickException(
                "Refusing to redact without --confirm. This rewrites audit "
                "diffs in place and rehashes every subsequent chain row."
            )
        from app.services import gdpr

        with app.app_context():
            try:
                summary = gdpr.redact(subject_spec)
            except gdpr.GDPRError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(
                f"Redacted {summary['audit_rows_redacted']} audit row(s) "
                f"for {summary['subject']['type']}:{summary['subject']['id']}."
            )
            if summary.get("declarations_redacted"):
                click.echo(
                    f"Redacted {summary['declarations_redacted']} "
                    "declaration(s) (signature, IP, UA wiped)."
                )

    @app.cli.command("training-issue-link")
    @click.option("--phone", required=True, help="E.164 phone number, e.g. +447700000000")
    @click.option("--course", "course_code", default="HACCP-REFRESHER", show_default=True)
    @click.option("--name", default="Smoke Test User", show_default=True)
    @click.option(
        "--role", "role_code", default="operator", show_default=True,
        help="Trainee role code; only matters if you're then exercising recurrence.",
    )
    @click.option(
        "--dry-run/--send", default=False,
        help="--dry-run prints the link without queueing the SMS (default --send).",
    )
    def _training_issue_link_cmd(
        phone: str, course_code: str, name: str, role_code: str, dry_run: bool
    ):
        """Live-SMS smoke helper: create-or-reuse a trainee with the given
        phone, issue an enrolment, print the magic-link URL, and queue
        the SMS via the existing ClickSend infra. Use --dry-run to skip
        the actual SMS enqueue (useful when validating URL shape before
        burning ClickSend credit)."""
        from app.extensions import db
        from app.models import Trainee
        from app.services import training as training_service

        with app.app_context():
            course = training_service.get_course_by_code(course_code)
            if course is None:
                raise click.ClickException(f"Course {course_code!r} not found.")

            trainee = Trainee.query.filter_by(phone=phone).first()
            if trainee is None:
                trainee = training_service.create_trainee(
                    phone=phone,
                    full_name=name,
                    role_code=role_code,
                )
                click.echo(f"Created trainee {trainee.id} ({phone}, role={role_code})")
            else:
                click.echo(f"Reusing trainee {trainee.id} ({phone})")

            if dry_run:
                # Build the link without enqueuing the SMS.
                from datetime import UTC, datetime, timedelta

                from app.models import TrainingEnrolment
                from app.services import training_links

                version = course.active_version
                if version is None:
                    raise click.ClickException(f"Course {course_code} has no active version.")
                issued_at = datetime.now(UTC)
                expires_at = issued_at + timedelta(days=version.link_ttl_days)
                enrolment = TrainingEnrolment(
                    trainee_id=trainee.id,
                    course_version_id=version.id,
                    magic_token="",
                    issued_at=issued_at,
                    expires_at=expires_at,
                    source="manual",
                    source_ref="cli-dry-run",
                )
                db.session.add(enrolment)
                db.session.flush()
                enrolment.magic_token = training_links.issue_token(
                    enrolment.id, expires_at
                )
                db.session.commit()
                base = app.config.get("TRAINING_BASE_URL", "")
                url = f"{base.rstrip('/')}/training/take/{enrolment.magic_token}"
                click.echo(f"DRY RUN — would send SMS to {phone}")
                click.echo(f"  URL: {url}")
                click.echo(f"  expires: {expires_at:%Y-%m-%d %H:%M UTC}")
                return

            enrolment = training_service.enrol(
                trainee=trainee, course=course, source="manual", source_ref="cli"
            )
            db.session.commit()
            base = app.config.get("TRAINING_BASE_URL", "")
            url = f"{base.rstrip('/')}/training/take/{enrolment.magic_token}"
            click.echo(f"Enrolment {enrolment.id} created.")
            click.echo(f"  URL:     {url}")
            click.echo(f"  expires: {enrolment.expires_at:%Y-%m-%d %H:%M UTC}")
            click.echo(
                "  SMS queued on the qms:sms RQ queue — "
                "run `flask rq-worker` to deliver it (or check the worker container's log)."
            )

    # ─── Compliance/admin TOTP enforcement ────────────────────────
    # Documented control: roles in ROLES_REQUIRING_2FA must have TOTP
    # enabled. Until they enrol, every request is bounced to the
    # enrolment page. Routes whitelisted below are the ones the user
    # must be able to reach to actually complete the enrolment or
    # log out.
    _TOTP_GATE_ALLOWLIST: frozenset[str] = frozenset(
        {
            "auth.totp_enroll",
            "auth.logout",
            "auth.set_language",
            "legal.privacy",
            "pwa.service_worker",
            "pwa.manifest",
            "health.healthz",
            "health.readyz",
            "help.index",
            "help.training",
            "help.admin",
            "help.auditor",
            "help.ops",
            "help.trainee_card_pdf",
            "static",
        }
    )

    @app.before_request
    def _enforce_totp_for_compliance_roles():
        if not app.config.get("ENFORCE_TOTP_FOR_ROLES", True):
            return None
        if not current_user.is_authenticated:
            return None
        if request.endpoint in _TOTP_GATE_ALLOWLIST:
            return None
        if request.endpoint and request.endpoint.startswith("static"):
            return None
        from app.services.totp import role_requires_totp

        role_code = current_user.role.code if current_user.role else None
        if not role_requires_totp(role_code):
            return None
        if current_user.totp_enabled:
            return None
        flash(_l("auth.2fa.required"), "warning")
        return redirect(url_for("auth.totp_enroll"))

    @app.context_processor
    def _inject_globals():
        from app.permissions import Perm

        return {
            "current_lang": g.get("lang", app.config["DEFAULT_LANGUAGE"]),
            "Perm": Perm,
            "current_section": lambda: _classify_endpoint(request.endpoint),
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
    if not app.config.get("TOTP_ENC_KEY"):
        warnings.append(
            "TOTP_ENC_KEY is not set — any TOTP enrolment or verification "
            "will fail at runtime"
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
        # Fernet key (urlsafe-base64) for TOTP-secret encryption-at-rest.
        # Generate with: python -c "from cryptography.fernet import Fernet;
        # print(Fernet.generate_key().decode())". Required only when
        # TOTP-protected roles actually enrol; missing key is flagged at
        # startup and refused at first encrypt/decrypt attempt.
        "TOTP_ENC_KEY": os.environ.get("TOTP_ENC_KEY"),
        # Comma-separated list of historical TOTP encryption keys. During
        # rotation, move the previous TOTP_ENC_KEY here so existing rows
        # keep decrypting; run `flask totp-rotate-keys` to bulk-re-encrypt
        # under the new primary, then drop the old keys from the env.
        "TOTP_ENC_KEYS_OLD": os.environ.get("TOTP_ENC_KEYS_OLD", ""),
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
        # Days *before* a cert expires that the recurrence scheduler should
        # start re-issuing magic links. Default 14 closes the SMS
        # roundtrip + retake window so workers don't go non-compliant
        # for a few hours at the moment of expiry.
        "TRAINING_RECERT_LEAD_DAYS": int(
            os.environ.get("TRAINING_RECERT_LEAD_DAYS", "14")
        ),
        # Number of trusted reverse-proxy hops between the public
        # internet and the app. 0 = direct exposure (don't honour XFF).
        # 1 = single Caddy/Nginx in front. 2 = e.g. Caddy → Tailscale
        # serve. Required for reliable rate-limiting; mis-configuring
        # this opens an XFF-spoof bypass.
        "TRUSTED_PROXY_HOPS": int(os.environ.get("TRUSTED_PROXY_HOPS", "0")),
        # Enforce TOTP enrolment for compliance & admin before they can
        # use any authenticated route. Disable only for tests that
        # don't exercise the gate (test fixtures opt out).
        "ENFORCE_TOTP_FOR_ROLES": os.environ.get(
            "ENFORCE_TOTP_FOR_ROLES", "1"
        ) not in ("0", "false", "False"),
    }
