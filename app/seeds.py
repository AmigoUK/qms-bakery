"""Idempotent seed data - roles, permissions, default admin, demo line."""

from __future__ import annotations

from app.auth import hash_password
from app.extensions import db
from app.models.auth import Permission, Role, User, UserRoleEnum
from app.models.haccp import CCPDefinition
from app.models.production import Pipeline, PipelineStage, ProductionLine
from app.models.salsa import ChecklistFrequency, SalsaChecklist
from app.models.training import (
    TrainingAnswerOption,
    TrainingAssignment,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingModule,
    TrainingQuestion,
)
from app.models.triggers import Responder, ResponderType, Trigger, trigger_responders
from app.permissions import PERMISSIONS, Perm


ROLE_PERMISSIONS: dict[str, list[str]] = {
    UserRoleEnum.OPERATOR.value: [
        Perm.TICKETS_CREATE,
        Perm.TICKETS_VIEW,
        Perm.CCP_MEASURE,
        Perm.SALSA_RESPOND,
        Perm.DASHBOARD_VIEW,
    ],
    UserRoleEnum.QA.value: [
        Perm.TICKETS_CREATE,
        Perm.TICKETS_VIEW,
        Perm.TICKETS_CLASSIFY,
        Perm.TICKETS_CORRECTIVE_ACTION,
        Perm.CCP_MEASURE,
        Perm.SALSA_RESPOND,
        Perm.DASHBOARD_VIEW,
        Perm.REPORTS_GENERATE,
        Perm.AUDIT_VIEW,
    ],
    UserRoleEnum.LINE_MANAGER.value: [
        Perm.TICKETS_CREATE,
        Perm.TICKETS_VIEW,
        Perm.TICKETS_CLASSIFY,
        Perm.TICKETS_CORRECTIVE_ACTION,
        Perm.TICKETS_CLOSE,
        Perm.CCP_MEASURE,
        Perm.SALSA_RESPOND,
        Perm.DASHBOARD_VIEW,
        Perm.REPORTS_GENERATE,
        Perm.AUDIT_VIEW,
    ],
    UserRoleEnum.COMPLIANCE.value: [
        Perm.TICKETS_CREATE,
        Perm.TICKETS_VIEW,
        Perm.TICKETS_CLASSIFY,
        Perm.TICKETS_CORRECTIVE_ACTION,
        Perm.TICKETS_CLOSE,
        Perm.CCP_MEASURE,
        Perm.CCP_DEFINE,
        Perm.SALSA_RESPOND,
        Perm.SALSA_DEFINE,
        Perm.PIPELINE_CONFIGURE,
        Perm.TRIGGERS_DEFINE,
        Perm.AUDIT_VIEW,
        Perm.AUDIT_EXPORT,
        Perm.REPORTS_GENERATE,
        Perm.DASHBOARD_VIEW,
        Perm.DLQ_MANAGE,
        Perm.TRAINING_AUTHOR,
        Perm.TRAINING_REVIEW,
        Perm.TRAINING_SEND,
    ],
    UserRoleEnum.PLANT_MANAGER.value: [
        Perm.TICKETS_VIEW,
        Perm.DASHBOARD_VIEW,
        Perm.REPORTS_GENERATE,
        Perm.AUDIT_VIEW,
        Perm.TRAINING_REVIEW,
        Perm.TRAINING_SEND,
    ],
    UserRoleEnum.ADMIN.value: [code for code, _ in PERMISSIONS],
}


ROLE_LABELS: dict[str, tuple[str, str]] = {
    UserRoleEnum.OPERATOR.value: ("Operator produkcji", "Production operator"),
    UserRoleEnum.QA.value: ("Specjalista QA", "QA specialist"),
    UserRoleEnum.LINE_MANAGER.value: ("Kierownik linii", "Line manager"),
    UserRoleEnum.COMPLIANCE.value: ("Compliance Officer", "Compliance officer"),
    UserRoleEnum.PLANT_MANAGER.value: ("Kierownik zakładu", "Plant manager"),
    UserRoleEnum.ADMIN.value: ("Administrator", "Administrator"),
}


def seed_initial(*, admin_email: str, admin_password: str) -> None:
    """Idempotent seeding - safe to call multiple times.

    Admin credentials are required arguments — never embed them as
    defaults so a misconfigured first-run can't silently provision a
    well-known account.
    """
    if not admin_email or not admin_password:
        raise ValueError("admin_email and admin_password are required for seeding")
    _seed_permissions()
    _seed_roles()
    _seed_admin(admin_email, admin_password)
    _seed_demo_line()
    _seed_demo_ccps()
    _seed_demo_salsa()
    _seed_demo_triggers()
    _seed_demo_training()
    db.session.commit()


_DEMO_COURSE_CODE = "HACCP-REFRESHER"

_DEMO_MODULES: list[tuple[dict, dict]] = [
    (
        {"pl": "Wprowadzenie do HACCP", "en": "Introduction to HACCP"},
        {
            "pl": (
                "## Czym jest HACCP?\n\n"
                "HACCP (Hazard Analysis and Critical Control Points) to systematyczne "
                "podejście do bezpieczeństwa żywności. Jako pracownik produkcji "
                "**masz osobistą odpowiedzialność** za przestrzeganie procedur "
                "krytycznych punktów kontrolnych (CCP) na swojej linii.\n\n"
                "### Twoje obowiązki\n\n"
                "- Wykonuj pomiary CCP w wyznaczonych odstępach czasu\n"
                "- Natychmiast zgłaszaj odchylenia od limitów krytycznych\n"
                "- Stosuj procedury higieny osobistej i higieny maszyn\n"
                "- Nie podejmuj samodzielnych decyzji w razie wątpliwości — "
                "skonsultuj się z kierownikiem linii lub QA"
            ),
            "en": (
                "## What is HACCP?\n\n"
                "HACCP (Hazard Analysis and Critical Control Points) is a "
                "systematic approach to food safety. As a production worker you "
                "**personally own** the discipline at critical control points "
                "(CCPs) on your line.\n\n"
                "### Your responsibilities\n\n"
                "- Take CCP measurements at the prescribed intervals\n"
                "- Report deviations from critical limits immediately\n"
                "- Follow personal hygiene and machine hygiene procedures\n"
                "- Never improvise when in doubt — escalate to your line "
                "manager or QA"
            ),
        },
    ),
    (
        {"pl": "Krytyczne limity i pomiary", "en": "Critical limits and measurements"},
        {
            "pl": (
                "## Limity krytyczne\n\n"
                "Każdy CCP ma zdefiniowany **limit krytyczny** — wartość, której "
                "przekroczenie oznacza utratę kontroli nad zagrożeniem. Limity "
                "są ustalane na podstawie wymagań prawnych, danych naukowych i "
                "wytycznych retailerów (SALSA, BRC, M&S, Tesco).\n\n"
                "### Procedura pomiaru\n\n"
                "1. Sprawdź, czy urządzenie jest skalibrowane\n"
                "2. Wprowadź wynik do aplikacji QMS na tablecie\n"
                "3. Jeżeli system zgłasza odchylenie, **wstrzymaj produkcję** "
                "i zastosuj akcję korygującą zdefiniowaną dla danego CCP\n"
                "4. Każdy pomiar — w limicie czy poza nim — jest zapisany w "
                "dzienniku audytu (chain-hashed)"
            ),
            "en": (
                "## Critical limits\n\n"
                "Each CCP has a defined **critical limit** — the value beyond "
                "which the hazard is no longer controlled. Limits come from "
                "legal requirements, scientific data, and retailer standards "
                "(SALSA, BRC, M&S, Tesco).\n\n"
                "### Measurement procedure\n\n"
                "1. Confirm the device is calibrated\n"
                "2. Enter the reading into the QMS app on the tablet\n"
                "3. If the system reports a deviation, **halt production** "
                "and apply the corrective action defined for that CCP\n"
                "4. Every measurement — within limits or out — is written to "
                "the chain-hashed audit log"
            ),
        },
    ),
    (
        {"pl": "Higiena i identyfikowalność", "en": "Hygiene and traceability"},
        {
            "pl": (
                "## Higiena osobista\n\n"
                "- Czyste rękawice, siatki na włosy, brak biżuterii\n"
                "- Codzienne checklisty SALSA wypełniane na początku zmiany\n"
                "- Zgłaszanie objawów chorobowych przed rozpoczęciem pracy\n\n"
                "## Identyfikowalność\n\n"
                "Każda partia, każdy pomiar i każdy podpis muszą być "
                "powiązane z numerem zgłoszenia (ticketu) w systemie. "
                "Audyt FSA / EHO oczekuje pełnej ścieżki od surowca do "
                "wysyłki w mniej niż 4 godziny."
            ),
            "en": (
                "## Personal hygiene\n\n"
                "- Clean gloves, hairnets, no jewellery\n"
                "- Daily SALSA checklists at the start of every shift\n"
                "- Report illness symptoms before starting work\n\n"
                "## Traceability\n\n"
                "Every batch, every measurement, and every signature must be "
                "linked to a ticket number in the system. FSA / EHO audits "
                "expect a complete trail from raw material to dispatch in "
                "under 4 hours."
            ),
        },
    ),
]

_DEMO_QUESTIONS: list[tuple[dict, list[tuple[dict, bool]]]] = [
    (
        {
            "pl": "Co oznacza skrót HACCP?",
            "en": "What does HACCP stand for?",
        },
        [
            (
                {
                    "pl": "Hazard Analysis and Critical Control Points",
                    "en": "Hazard Analysis and Critical Control Points",
                },
                True,
            ),
            (
                {
                    "pl": "Hygiene Audit and Compliance Certification Procedure",
                    "en": "Hygiene Audit and Compliance Certification Procedure",
                },
                False,
            ),
            (
                {
                    "pl": "High Altitude Cooking and Cooling Practice",
                    "en": "High Altitude Cooking and Cooling Practice",
                },
                False,
            ),
        ],
    ),
    (
        {
            "pl": "Co należy zrobić w pierwszej kolejności po wykryciu odchylenia od limitu krytycznego?",
            "en": "What should you do first after detecting a deviation from a critical limit?",
        },
        [
            (
                {
                    "pl": "Wstrzymać produkcję i zastosować akcję korygującą",
                    "en": "Halt production and apply the corrective action",
                },
                True,
            ),
            (
                {
                    "pl": "Kontynuować produkcję i zgłosić to po zmianie",
                    "en": "Keep producing and report it after the shift",
                },
                False,
            ),
            (
                {
                    "pl": "Skorygować wynik ręcznie w dzienniku",
                    "en": "Manually correct the reading in the log",
                },
                False,
            ),
        ],
    ),
    (
        {
            "pl": "Czy każdy pomiar CCP jest zapisywany w dzienniku audytu?",
            "en": "Is every CCP measurement written to the audit log?",
        },
        [
            (
                {
                    "pl": "Tak — wszystkie, niezależnie od wyniku",
                    "en": "Yes — every reading, regardless of outcome",
                },
                True,
            ),
            (
                {
                    "pl": "Tylko te poza limitami",
                    "en": "Only out-of-limit ones",
                },
                False,
            ),
        ],
    ),
    (
        {
            "pl": "Jakie są wymagania higieny osobistej na linii?",
            "en": "What are the personal hygiene requirements on the line?",
        },
        [
            (
                {
                    "pl": "Czyste rękawice, siatki na włosy, brak biżuterii",
                    "en": "Clean gloves, hairnets, no jewellery",
                },
                True,
            ),
            (
                {
                    "pl": "Tylko czyste ubranie robocze",
                    "en": "Just clean work clothing",
                },
                False,
            ),
            (
                {
                    "pl": "Tylko siatki na włosy w piekarni",
                    "en": "Only hairnets in the bakery",
                },
                False,
            ),
        ],
    ),
    (
        {
            "pl": "Jaka jest oczekiwana czas pełnej identyfikowalności partii dla audytu FSA?",
            "en": "What is the expected traceability turnaround for an FSA audit?",
        },
        [
            (
                {
                    "pl": "Mniej niż 4 godziny",
                    "en": "Under 4 hours",
                },
                True,
            ),
            (
                {
                    "pl": "Mniej niż 24 godziny",
                    "en": "Under 24 hours",
                },
                False,
            ),
            (
                {
                    "pl": "Następnego dnia roboczego",
                    "en": "Next business day",
                },
                False,
            ),
        ],
    ),
]


def _seed_demo_training() -> None:
    if TrainingCourse.query.filter_by(code=_DEMO_COURSE_CODE).first():
        return
    course = TrainingCourse(
        code=_DEMO_COURSE_CODE,
        description="Annual HACCP knowledge refresher for floor operators.",
    )
    db.session.add(course)
    db.session.flush()
    version = TrainingCourseVersion(
        course_id=course.id,
        version=1,
        is_active=True,
        title={"pl": "Odświeżenie HACCP", "en": "HACCP Refresher"},
        summary={
            "pl": "10-minutowy kurs odświeżający kluczowe zasady HACCP.",
            "en": "A 10-minute refresher of the key HACCP principles.",
        },
        pass_threshold=0.7,
        validity_months=12,
        link_ttl_days=7,
    )
    db.session.add(version)
    db.session.flush()
    for idx, (title, body) in enumerate(_DEMO_MODULES):
        db.session.add(
            TrainingModule(
                course_version_id=version.id,
                order_index=idx,
                title=title,
                body_md=body,
            )
        )
    for idx, (prompt, options) in enumerate(_DEMO_QUESTIONS):
        question = TrainingQuestion(
            course_version_id=version.id,
            order_index=idx,
            prompt=prompt,
            kind="single_choice",
        )
        db.session.add(question)
        db.session.flush()
        for opt_idx, (label, is_correct) in enumerate(options):
            db.session.add(
                TrainingAnswerOption(
                    question_id=question.id,
                    order_index=opt_idx,
                    label=label,
                    is_correct=is_correct,
                )
            )
    # Default audience: every operator, annual recurrence.
    db.session.add(
        TrainingAssignment(
            course_id=course.id,
            role_code="operator",
            recurrence_months=12,
        )
    )
    db.session.flush()


def _seed_demo_triggers() -> None:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    if Trigger.query.filter_by(code="OVEN1_OVERHEAT").first():
        return

    notify = Responder(
        code="NOTIFY_QA",
        name={"pl": "Powiadom QA", "en": "Notify QA"},
        type=ResponderType.NOTIFY_IN_APP.value,
        config={
            "title": "Trigger {trigger_code}: {metric} = {temperature}°C",
            "body": "Severity: {severity}",
            "recipients": [{"role_code": "qa"}, {"role_code": "line_manager"}],
        },
    )
    create_ticket = Responder(
        code="OPEN_TICKET",
        name={"pl": "Otwórz zgłoszenie", "en": "Open ticket"},
        type=ResponderType.CREATE_TICKET.value,
        config={
            "title": "Auto: {trigger_code} — {metric} = {temperature}",
            "description": "Auto-generated by trigger {trigger_code}.",
        },
    )
    db.session.add_all([notify, create_ticket])
    db.session.flush()

    trigger = Trigger(
        code="OVEN1_OVERHEAT",
        name={"pl": "Przegrzanie pieca 1", "en": "Oven 1 overheating"},
        scope=f"line:{line.code}" if line else None,
        condition={"metric": "temperature", "operator": ">", "value": 220},
        severity="high",
        is_active=True,
    )
    db.session.add(trigger)
    db.session.flush()

    db.session.execute(
        trigger_responders.insert(),
        [
            {"trigger_id": trigger.id, "responder_id": notify.id, "order_index": 0},
            {"trigger_id": trigger.id, "responder_id": create_ticket.id, "order_index": 1},
        ],
    )
    db.session.flush()


def _seed_demo_salsa() -> None:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    if SalsaChecklist.query.filter_by(code="HYG-DAILY").first():
        return

    db.session.add(
        SalsaChecklist(
            code="HYG-DAILY",
            name={"pl": "Higiena personelu — codziennie", "en": "Personnel hygiene — daily"},
            frequency=ChecklistFrequency.DAILY.value,
            line_id=line.id if line else None,
            items=[
                {
                    "key": "gloves",
                    "prompt": {
                        "pl": "Wszyscy operatorzy mają czyste rękawice.",
                        "en": "All operators wear clean gloves.",
                    },
                },
                {
                    "key": "hairnets",
                    "prompt": {
                        "pl": "Wszyscy operatorzy mają siatki na włosy.",
                        "en": "All operators wear hairnets.",
                    },
                },
                {
                    "key": "no_jewellery",
                    "prompt": {
                        "pl": "Brak biżuterii (poza zatwierdzoną).",
                        "en": "No jewellery (except approved).",
                    },
                },
                {
                    "key": "health_check",
                    "prompt": {
                        "pl": "Brak zgłoszonych objawów chorobowych.",
                        "en": "No reported illness symptoms.",
                    },
                },
            ],
        )
    )
    db.session.add(
        SalsaChecklist(
            code="MACH-SHIFT",
            name={
                "pl": "Higiena maszyn — przed zmianą",
                "en": "Machine hygiene — pre-shift",
            },
            frequency=ChecklistFrequency.SHIFT.value,
            line_id=line.id if line else None,
            items=[
                {
                    "key": "mixer_clean",
                    "prompt": {"pl": "Mikser umyty.", "en": "Mixer cleaned."},
                },
                {
                    "key": "conveyor_clean",
                    "prompt": {"pl": "Taśma czysta.", "en": "Conveyor clean."},
                },
                {
                    "key": "oven_inspected",
                    "prompt": {"pl": "Piec sprawdzony.", "en": "Oven inspected."},
                },
            ],
        )
    )
    db.session.flush()


def _seed_demo_ccps() -> None:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    if line is None:
        return
    if CCPDefinition.query.filter_by(line_id=line.id).first():
        return
    db.session.add_all(
        [
            CCPDefinition(
                line_id=line.id,
                code="CCP-OVEN-1",
                name={"pl": "Temperatura pieca 1", "en": "Oven 1 temperature"},
                parameter="temperature",
                unit="°C",
                critical_limit_min=180.0,
                critical_limit_max=220.0,
                monitoring_frequency_minutes=15,
                corrective_action={
                    "pl": "Wstrzymaj produkcję, sprawdź czujnik, skalibruj.",
                    "en": "Halt production, inspect probe, recalibrate.",
                },
            ),
            CCPDefinition(
                line_id=line.id,
                code="CCP-CORE-TEMP",
                name={"pl": "Temperatura wewnętrzna pieczywa", "en": "Bread core temperature"},
                parameter="temperature",
                unit="°C",
                critical_limit_min=92.0,
                critical_limit_max=None,
                monitoring_frequency_minutes=60,
                corrective_action={
                    "pl": "Wstrzymaj partię, wydłuż czas pieczenia.",
                    "en": "Hold batch, extend bake time.",
                },
            ),
        ]
    )
    db.session.flush()


def _seed_permissions() -> None:
    existing = {p.code for p in Permission.query.all()}
    for code, desc in PERMISSIONS:
        if code not in existing:
            db.session.add(Permission(code=code, description=desc))
    db.session.flush()


def _seed_roles() -> None:
    perms = {p.code: p for p in Permission.query.all()}
    for role_code, perm_codes in ROLE_PERMISSIONS.items():
        role = Role.query.filter_by(code=role_code).first()
        name_pl, name_en = ROLE_LABELS[role_code]
        if role is None:
            role = Role(code=role_code, name_pl=name_pl, name_en=name_en)
            db.session.add(role)
            db.session.flush()
        role.permissions = [perms[c] for c in perm_codes if c in perms]
    db.session.flush()


def _seed_admin(email: str, password: str) -> None:
    if User.query.filter_by(email=email).first():
        return
    admin_role = Role.query.filter_by(code=UserRoleEnum.ADMIN.value).first()
    if not admin_role:
        return
    admin = User(
        email=email,
        password_hash=hash_password(password),
        full_name="Administrator",
        language="en",
        role_id=admin_role.id,
    )
    db.session.add(admin)
    db.session.flush()


def _seed_demo_line() -> None:
    if ProductionLine.query.filter_by(code="LINE_A").first():
        return
    line = ProductionLine(code="LINE_A", name="Line A — Bread", location="Zone 1")
    db.session.add(line)
    db.session.flush()

    pipeline = Pipeline(line_id=line.id, version=1, is_active=True)
    db.session.add(pipeline)
    db.session.flush()

    stages = [
        ("detection", {"pl": "Wykrycie", "en": "Detection"}, None, 5, False),
        ("classification", {"pl": "Klasyfikacja", "en": "Classification"}, "qa", 15, False),
        ("analysis", {"pl": "Analiza", "en": "Analysis"}, "qa", 60, True),
        ("corrective", {"pl": "Akcja korygująca", "en": "Corrective action"}, "line_manager", 120, False),
        ("verification", {"pl": "Weryfikacja", "en": "Verification"}, "qa", 60, False),
        ("closure", {"pl": "Zamknięcie", "en": "Closure"}, "line_manager", 30, False),
    ]
    for idx, (code, name, role, sla, ccp) in enumerate(stages):
        db.session.add(
            PipelineStage(
                pipeline_id=pipeline.id,
                order_index=idx,
                code=code,
                name=name,
                required_role_code=role,
                sla_minutes=sla,
                is_ccp_checkpoint=ccp,
            )
        )
    db.session.flush()
