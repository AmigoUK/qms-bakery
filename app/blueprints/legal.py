"""Public legal pages: privacy notice (Art. 13 UK GDPR).

Mounted at the root so trainees and users alike can reach `/privacy`
without authenticating. Bilingual via the existing i18n stack — same
language switcher (`/auth/lang/<code>`) drives this page.
"""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("legal", __name__, template_folder="../templates")


@bp.route("/privacy", methods=["GET"])
def privacy():
    return render_template("legal/privacy.html")
