"""Permission codes — single source of truth.

Every place in the codebase that names a permission goes through this
module, so renames are one edit and typos surface as ImportError instead
of a silent 403.

The `PERMISSIONS` list below also drives the seed loader — adding a row
here is the only step needed to provision a new permission across all
environments.
"""

from __future__ import annotations


class Perm:
    """String constants for every permission code in the system.

    Use these in `@require_permission(...)`, `current_user.has_permission(...)`
    and Jinja templates (exposed as `perm` via context processor).
    """

    TICKETS_CREATE = "tickets.create"
    TICKETS_VIEW = "tickets.view"
    TICKETS_CLASSIFY = "tickets.classify"
    TICKETS_CORRECTIVE_ACTION = "tickets.corrective_action"
    TICKETS_CLOSE = "tickets.close"

    CCP_MEASURE = "ccp.measure"
    CCP_DEFINE = "ccp.define"

    SALSA_RESPOND = "salsa.respond"
    SALSA_DEFINE = "salsa.define"

    PIPELINE_CONFIGURE = "pipeline.configure"
    TRIGGERS_DEFINE = "triggers.define"

    USERS_MANAGE = "users.manage"
    AUDIT_VIEW = "audit.view"
    AUDIT_EXPORT = "audit.export"
    REPORTS_GENERATE = "reports.generate"
    DASHBOARD_VIEW = "dashboard.view"
    SYSTEM_CONFIGURE = "system.configure"
    DLQ_MANAGE = "dlq.manage"

    TRAINING_AUTHOR = "training.author"
    TRAINING_REVIEW = "training.review"
    TRAINING_SEND = "training.send"


PERMISSIONS: list[tuple[str, str]] = [
    (Perm.TICKETS_CREATE, "Create tickets"),
    (Perm.TICKETS_CLASSIFY, "Classify tickets"),
    (Perm.TICKETS_CORRECTIVE_ACTION, "Apply corrective action"),
    (Perm.TICKETS_CLOSE, "Close tickets"),
    (Perm.TICKETS_VIEW, "View tickets"),
    (Perm.CCP_MEASURE, "Record CCP measurements"),
    (Perm.CCP_DEFINE, "Define CCP parameters"),
    (Perm.SALSA_RESPOND, "Respond to SALSA checklists"),
    (Perm.SALSA_DEFINE, "Define SALSA checklists"),
    (Perm.PIPELINE_CONFIGURE, "Configure pipelines"),
    (Perm.TRIGGERS_DEFINE, "Define triggers and responders"),
    (Perm.USERS_MANAGE, "Manage users"),
    (Perm.AUDIT_EXPORT, "Export audit trail"),
    (Perm.AUDIT_VIEW, "View audit trail"),
    (Perm.REPORTS_GENERATE, "Generate reports"),
    (Perm.DASHBOARD_VIEW, "View dashboards"),
    (Perm.SYSTEM_CONFIGURE, "Configure system"),
    (Perm.DLQ_MANAGE, "View and replay failed responder jobs"),
    (Perm.TRAINING_AUTHOR, "Author training courses"),
    (Perm.TRAINING_REVIEW, "Review training results and dashboard"),
    (Perm.TRAINING_SEND, "Manage trainees and issue training links"),
]
