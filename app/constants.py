"""Constants for cross-cutting magic numbers.

Length caps mirror the corresponding model column widths — when a model
column changes width, update the matching constant here so the WTForms
validator stays in sync. Pagination limits and validation ranges live
here instead of being repeated across blueprints.
"""

from __future__ import annotations

# WTForms `Length(max=...)` caps. Match the underlying model columns —
# changing one without the other gets you a 500 from the DB instead of
# a clean form-validation error.
MAX_EMAIL_LENGTH = 255
MAX_FULL_NAME_LENGTH = 120
MAX_CODE_LENGTH = 64
MAX_NAME_BILINGUAL_LENGTH = 200
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 5000
MAX_NOTES_LENGTH = 500
MAX_DEVICE_ID_LENGTH = 64
MAX_PASSWORD_LENGTH = 128

# Pagination caps surfaced in the UI. Keep small enough for one
# screenfull on a tablet — operators don't scroll long lists.
DASHBOARD_RECENT_TICKETS_LIMIT = 10
HACCP_RECENT_MEASUREMENTS_LIMIT = 25
ADMIN_RECENT_AUDIT_LIMIT = 20

# CCP measurement value range. The hard caps catch obviously corrupt
# sensor reads; per-CCP critical_limit_min/max are the real business
# bounds checked downstream.
MEASUREMENT_VALUE_MIN = -1000.0
MEASUREMENT_VALUE_MAX = 10000.0

# Trigger duration window: how long a condition must hold continuously
# before the trigger fires. 1 hour is a generous ceiling for any
# realistic process.
TRIGGER_DURATION_MAX_SECONDS = 3600
