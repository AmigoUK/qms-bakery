"""Audit-action constants — single source of truth.

Every `audit.record(action=...)` call goes through one of these so the
audit trail can't fork into "create" + "created" because of a typo. The
strings themselves stay short and lowercase to keep the audit_log table
human-greppable.
"""

from __future__ import annotations


class AuditAction:
    # Auth
    LOGIN_SUCCESS = "login_success"
    LOGIN_2FA_SUCCESS = "login_2fa_success"
    LOGIN_2FA_FAILURE = "login_2fa_failure"
    LOGOUT = "logout"
    ACCOUNT_LOCKED = "account_locked"
    TOTP_ENROLLED = "totp_enrolled"
    DENIED = "denied"

    # CRUD-ish
    CREATE = "create"
    UPDATE = "update"
    TOGGLE_ACTIVE = "toggle_active"
    CREATE_VERSION = "create_version"

    # Tickets
    STATUS_CHANGE = "status_change"
    ASSIGNED = "assigned"
    COMMENT = "comment"

    # Domain events
    SUBMIT = "submit"
    RECORD = "record"
    FIRE = "fire"
    FIRE_DRY_RUN = "fire_dry_run"

    # Ops
    DLQ_REQUEUE = "dlq_requeue"
    DLQ_DISCARD = "dlq_discard"

    # Training
    TRAINING_ENROLLED = "training_enrolled"
    TRAINING_LINK_SENT = "training_link_sent"
    TRAINING_ATTEMPT_STARTED = "training_attempt_started"
    TRAINING_ATTEMPT_SUBMITTED = "training_attempt_submitted"
    TRAINING_DECLARED = "training_declared"
    TRAINING_CERTIFIED = "training_certified"
    TRAINING_LINK_EXPIRED = "training_link_expired"
