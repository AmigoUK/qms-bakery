"""Idempotent seed data - roles, permissions, default admin, demo line."""

from __future__ import annotations

from app.auth import hash_password
from app.extensions import db
from app.models.auth import Permission, Role, User, UserRoleEnum
from app.models.haccp import CCPDefinition
from app.models.production import Pipeline, PipelineStage, ProductionLine
from app.models.salsa import ChecklistFrequency, SalsaChecklist


PERMISSIONS: list[tuple[str, str]] = [
    ("tickets.create", "Create tickets"),
    ("tickets.classify", "Classify tickets"),
    ("tickets.corrective_action", "Apply corrective action"),
    ("tickets.close", "Close tickets"),
    ("tickets.view", "View tickets"),
    ("ccp.measure", "Record CCP measurements"),
    ("ccp.define", "Define CCP parameters"),
    ("salsa.respond", "Respond to SALSA checklists"),
    ("salsa.define", "Define SALSA checklists"),
    ("pipeline.configure", "Configure pipelines"),
    ("triggers.define", "Define triggers and responders"),
    ("users.manage", "Manage users"),
    ("audit.export", "Export audit trail"),
    ("audit.view", "View audit trail"),
    ("reports.generate", "Generate reports"),
    ("dashboard.view", "View dashboards"),
    ("system.configure", "Configure system"),
]


ROLE_PERMISSIONS: dict[str, list[str]] = {
    UserRoleEnum.OPERATOR.value: [
        "tickets.create",
        "tickets.view",
        "ccp.measure",
        "salsa.respond",
        "dashboard.view",
    ],
    UserRoleEnum.QA.value: [
        "tickets.create",
        "tickets.view",
        "tickets.classify",
        "tickets.corrective_action",
        "ccp.measure",
        "salsa.respond",
        "dashboard.view",
        "reports.generate",
        "audit.view",
    ],
    UserRoleEnum.LINE_MANAGER.value: [
        "tickets.create",
        "tickets.view",
        "tickets.classify",
        "tickets.corrective_action",
        "tickets.close",
        "ccp.measure",
        "salsa.respond",
        "dashboard.view",
        "reports.generate",
        "audit.view",
    ],
    UserRoleEnum.COMPLIANCE.value: [
        "tickets.create",
        "tickets.view",
        "tickets.classify",
        "tickets.corrective_action",
        "tickets.close",
        "ccp.measure",
        "ccp.define",
        "salsa.respond",
        "salsa.define",
        "pipeline.configure",
        "triggers.define",
        "audit.view",
        "audit.export",
        "reports.generate",
        "dashboard.view",
    ],
    UserRoleEnum.PLANT_MANAGER.value: [
        "tickets.view",
        "dashboard.view",
        "reports.generate",
        "audit.view",
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


def seed_initial(admin_email: str = "admin@local", admin_password: str = "ChangeMe123!") -> None:
    """Idempotent seeding - safe to call multiple times."""
    _seed_permissions()
    _seed_roles()
    _seed_admin(admin_email, admin_password)
    _seed_demo_line()
    _seed_demo_ccps()
    _seed_demo_salsa()
    db.session.commit()


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
