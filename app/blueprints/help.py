"""Public help & documentation pages.

Five guides, one per audience:
  - /help              — index, who-is-this-for routing
  - /help/training     — trainee guide (PL primary, EN parallel)
  - /help/admin        — compliance officer / plant manager guide
  - /help/auditor      — external SALSA / BRC auditor packet
  - /help/ops          — devops / sysadmin runbook

Plus the printable A4 quick-reference card for the bakery floor:
  - /help/trainee-card.pdf — single-page PDF, PL only

All routes are public reads. The pages contain no PII and no secrets;
keeping them unauthenticated lets a trainee on the SMS-magic-link page
reach them, lets a manager print the card without logging in, and lets
an external auditor browse the auditor packet before the engagement.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, render_template

bp = Blueprint("help", __name__, template_folder="../templates", url_prefix="/help")


@bp.route("/", methods=["GET"])
def index():
    return render_template("help/index.html")


@bp.route("/training", methods=["GET"])
def training():
    return render_template("help/training.html")


@bp.route("/admin", methods=["GET"])
def admin():
    return render_template("help/admin.html")


@bp.route("/auditor", methods=["GET"])
def auditor():
    return render_template("help/auditor.html")


@bp.route("/ops", methods=["GET"])
def ops():
    return render_template("help/ops.html")


@bp.route("/trainee-card.pdf", methods=["GET"])
def trainee_card_pdf():
    """A4 single-page printable Polish quick-reference for trainees.

    Designed to be laminated and stuck on the bakery noticeboard. Five
    steps from receiving the SMS to downloading the cert.
    """
    import weasyprint

    html = render_template("reports/trainee_quick_card.html")
    pdf = weasyprint.HTML(
        string=html, base_url=str(current_app.root_path)
    ).write_pdf()
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="trainee-quick-card.pdf"',
        },
    )
