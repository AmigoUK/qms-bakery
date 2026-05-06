"""Trainee-facing magic-link routes — public, unauthenticated.

Identity is the magic-link token in the URL. Every route except the
expired-page renderer goes through `_load_enrolment`, which both
verifies the HMAC and confirms the row is still live; both checks fail
into the same 'expired' page so we don't leak which one tripped.
"""

from __future__ import annotations

import base64
import binascii

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app.extensions import db
from app.i18n import gettext as _
from app.models import EnrolmentStatus, TrainingEnrolment
from app.services import training as training_service

bp = Blueprint("training", __name__, url_prefix="/training", template_folder="../templates")


# Magic-link routes can't carry CSRF on the FIRST GET; subsequent POSTs
# do (CSRF token is rendered into each form). We do NOT csrf-exempt the
# blueprint — every state-changing route is a POST with a token.


def _load_enrolment(token: str) -> tuple[TrainingEnrolment | None, str]:
    """Returns (enrolment, reason). reason is "" on success, else
    "expired" or "invalid" for the 410 page to branch on."""
    enrolment, reason = training_service.get_enrolment_with_reason(token)
    if enrolment is None:
        return (None, reason)
    g.trainee_lang = enrolment.trainee.language or "en"
    return (enrolment, "")


def _expired(reason: str = "expired") -> Response:
    return Response(
        render_template(
            "training/expired.html", trainee_lang="en", reason=reason,
        ),
        status=410,
    )


def _render(template: str, *, enrolment: TrainingEnrolment, **ctx) -> Response:
    ctx.setdefault("trainee_lang", enrolment.trainee.language or "en")
    ctx.setdefault("trainee", enrolment.trainee)
    ctx.setdefault("enrolment", enrolment)
    ctx.setdefault("version", enrolment.course_version)
    return render_template(template, **ctx)


@bp.route("/take/<token>", methods=["GET"])
def take(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    # If the trainee already submitted their attempt, jump straight to the
    # result so reopening the link doesn't re-prompt the exam.
    if enrolment.status == EnrolmentStatus.SUBMITTED.value:
        return redirect(url_for("training.result", token=token))
    return _render("training/landing.html", enrolment=enrolment)


@bp.route("/take/<token>/start", methods=["POST"])
def start(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    if enrolment.status == EnrolmentStatus.ISSUED.value:
        enrolment.status = EnrolmentStatus.STARTED.value
        db.session.commit()
    if enrolment.module_progress >= len(enrolment.course_version.modules):
        # All modules done — drop into the optional practice/study step.
        # Trainee can press "I know this" on /study to skip to /exam.
        return redirect(url_for("training.study", token=token))
    return redirect(url_for("training.module", token=token))


@bp.route("/take/<token>/module", methods=["GET"])
def module(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    modules = enrolment.course_version.modules
    if not modules:
        return redirect(url_for("training.exam", token=token))
    idx = min(enrolment.module_progress, len(modules) - 1)
    return _render(
        "training/module.html",
        enrolment=enrolment,
        module=modules[idx],
        index=idx,
        total=len(modules),
    )


@bp.route("/take/<token>/next", methods=["POST"])
def next_module(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    total = len(enrolment.course_version.modules)
    enrolment.module_progress = min(enrolment.module_progress + 1, total)
    db.session.commit()
    if enrolment.module_progress >= total:
        # Past the last module → optional practice/study step.
        return redirect(url_for("training.study", token=token))
    return redirect(url_for("training.module", token=token))


@bp.route("/take/<token>/study", methods=["GET"])
def study(token: str):
    """Optional flash-card practice step between modules and the exam.

    Renders the same questions the exam will assess but reveals the
    answer key. No state is persisted — order is shuffled client-side
    each visit, the trainee can leave for /exam at any time.
    """
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    if enrolment.status == EnrolmentStatus.SUBMITTED.value:
        return redirect(url_for("training.result", token=token))
    return _render(
        "training/study.html",
        enrolment=enrolment,
        questions=enrolment.course_version.questions,
    )


@bp.route("/take/<token>/exam", methods=["GET"])
def exam(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    if enrolment.status == EnrolmentStatus.SUBMITTED.value:
        return redirect(url_for("training.result", token=token))
    return _render(
        "training/exam.html",
        enrolment=enrolment,
        questions=enrolment.course_version.questions,
    )


@bp.route("/take/<token>/exam", methods=["POST"])
def submit_exam(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    if enrolment.status == EnrolmentStatus.SUBMITTED.value:
        return redirect(url_for("training.result", token=token))

    answers: dict[str, list[str]] = {}
    for q in enrolment.course_version.questions:
        # Form field name pattern: "q-{question_id}"; multi-checkbox uses the same name.
        values = request.form.getlist(f"q-{q.id}")
        answers[q.id] = [v for v in values if v]
    try:
        attempt = training_service.submit_attempt(enrolment, answers)
    except training_service.TrainingError:
        return _expired()
    db.session.commit()
    if attempt.passed:
        return redirect(url_for("training.declaration", token=token))
    return redirect(url_for("training.result", token=token))


@bp.route("/take/<token>/declaration", methods=["GET"])
def declaration(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    attempt = enrolment.attempt
    if attempt is None or not attempt.passed:
        return redirect(url_for("training.result", token=token))
    declaration_text = _("training.declaration.text")
    return _render(
        "training/declaration.html",
        enrolment=enrolment,
        attempt=attempt,
        declaration_text=declaration_text,
    )


@bp.route("/take/<token>/declare", methods=["POST"])
def declare(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    attempt = enrolment.attempt
    if attempt is None or not attempt.passed:
        return redirect(url_for("training.result", token=token))

    typed_name = (request.form.get("typed_name") or "").strip()
    declaration_text = request.form.get("declaration_text") or _(
        "training.declaration.text"
    )
    sig_data_url = request.form.get("signature_data_url") or ""
    sig_png: bytes | None = None
    if sig_data_url.startswith("data:image/png;base64,"):
        try:
            sig_png = base64.b64decode(
                sig_data_url.split(",", 1)[1], validate=True
            )
            if len(sig_png) > 256 * 1024:  # 256KB cap; honest signatures are <30KB
                sig_png = None
        except (binascii.Error, ValueError):
            sig_png = None

    try:
        training_service.record_declaration(
            attempt=attempt,
            typed_name=typed_name,
            declaration_text=declaration_text,
            signature_png=sig_png,
            ip_address=request.remote_addr,
            user_agent=(request.user_agent.string or "")[:255] or None,
        )
    except training_service.TrainingError:
        # Already declared — fall through to result page.
        pass
    db.session.commit()
    return redirect(url_for("training.result", token=token))


@bp.route("/take/<token>/result", methods=["GET"])
def result(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    attempt = enrolment.attempt
    if attempt is None:
        return redirect(url_for("training.take", token=token))
    cert = None
    if attempt.passed:
        cert = training_service.latest_certification(
            enrolment.trainee_id, enrolment.course_version.course_id
        )
    return _render(
        "training/result.html", enrolment=enrolment, attempt=attempt, cert=cert
    )


@bp.route("/take/<token>/certificate", methods=["GET"])
def certificate(token: str):
    enrolment, reason = _load_enrolment(token)
    if enrolment is None:
        return _expired(reason)
    attempt = enrolment.attempt
    if attempt is None or not attempt.passed:
        abort(404)
    cert = training_service.latest_certification(
        enrolment.trainee_id, enrolment.course_version.course_id
    )
    if cert is None:
        abort(404)
    import weasyprint

    html = render_template(
        "reports/training_certificate.html",
        trainee=enrolment.trainee,
        course=enrolment.course_version.course,
        version=enrolment.course_version,
        attempt=attempt,
        cert=cert,
    )
    pdf = weasyprint.HTML(string=html, base_url=str(current_app.root_path)).write_pdf()
    filename = f"training-cert-{enrolment.trainee.phone[-4:]}-{cert.id[:8]}.pdf"
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
