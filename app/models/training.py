"""Worker training & certification models.

Trainees are floor workers who don't otherwise log into the QMS — they
exist only to receive an SMS magic link, complete a course on a phone,
sign a declaration, and produce a tamper-evident compliance record.

Course content is versioned the same way Pipelines are: edits create a
new TrainingCourseVersion; in-flight enrolments pin to the version
they started against, so an admin edit never disturbs a worker
mid-attempt.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db


class EnrolmentStatus(str, enum.Enum):
    """State machine for a single magic-link enrolment."""

    ISSUED = "issued"
    STARTED = "started"
    SUBMITTED = "submitted"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class EnrolmentSource(str, enum.Enum):
    """How an enrolment was created — useful for the dashboard's
    'why did this trainee receive an SMS?' column."""

    SCHEDULE = "schedule"
    TRIGGER = "trigger"
    MANUAL = "manual"


class QuestionKind(str, enum.Enum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    TRUE_FALSE = "true_false"


class Trainee(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "trainees"

    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role_code: Mapped[str] = mapped_column(String(32), nullable=False)
    line_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("production_lines.id"), nullable=True
    )
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Hybrid future-proof: link a trainee to a User if the same person
    # also has system access (line manager, QA). Nullable; the magic-link
    # flow never reads it.
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Trainee {self.phone} {self.full_name}>"


class TrainingCourse(UUIDPKMixin, TimestampMixin, db.Model):
    """The stable identity of a course. Content lives in versions."""

    __tablename__ = "training_courses"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    versions: Mapped[list["TrainingCourseVersion"]] = relationship(
        back_populates="course",
        order_by="TrainingCourseVersion.version",
        cascade="all, delete-orphan",
    )
    assignments: Mapped[list["TrainingAssignment"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )

    @property
    def active_version(self) -> "TrainingCourseVersion | None":
        for v in self.versions:
            if v.is_active:
                return v
        return None

    def __repr__(self) -> str:
        return f"<TrainingCourse {self.code}>"


class TrainingCourseVersion(UUIDPKMixin, TimestampMixin, db.Model):
    """An immutable snapshot of course content. Edits create v+1; in-flight
    enrolments keep pointing at their original version. Mirrors the
    Pipeline / PipelineStage pattern."""

    __tablename__ = "training_course_versions"

    course_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_courses.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Bilingual {pl, en} fields — same pattern as Trigger.name, CCPDefinition.name.
    title: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Per-version configuration knobs.
    pass_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    validity_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    link_ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    exam_time_limit_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    course: Mapped[TrainingCourse] = relationship(back_populates="versions")
    modules: Mapped[list["TrainingModule"]] = relationship(
        back_populates="course_version",
        order_by="TrainingModule.order_index",
        cascade="all, delete-orphan",
    )
    questions: Mapped[list["TrainingQuestion"]] = relationship(
        back_populates="course_version",
        order_by="TrainingQuestion.order_index",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("course_id", "version", name="uq_course_version"),
    )

    def __repr__(self) -> str:
        return f"<TrainingCourseVersion course={self.course_id} v{self.version}>"


class TrainingModule(UUIDPKMixin, TimestampMixin, db.Model):
    """A single chunk of reading material. Body is bilingual markdown."""

    __tablename__ = "training_modules"

    course_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_course_versions.id"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[dict] = mapped_column(JSON, nullable=False)
    body_md: Mapped[dict] = mapped_column(JSON, nullable=False)

    course_version: Mapped[TrainingCourseVersion] = relationship(back_populates="modules")

    __table_args__ = (
        UniqueConstraint(
            "course_version_id", "order_index", name="uq_module_version_order"
        ),
    )


class TrainingQuestion(UUIDPKMixin, TimestampMixin, db.Model):
    """A single exam question, fixed-list per course version."""

    __tablename__ = "training_questions"

    course_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_course_versions.id"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[dict] = mapped_column(JSON, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=QuestionKind.SINGLE_CHOICE.value
    )

    course_version: Mapped[TrainingCourseVersion] = relationship(back_populates="questions")
    options: Mapped[list["TrainingAnswerOption"]] = relationship(
        back_populates="question",
        order_by="TrainingAnswerOption.order_index",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "course_version_id", "order_index", name="uq_question_version_order"
        ),
    )


class TrainingAnswerOption(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "training_answer_options"

    question_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_questions.id"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    question: Mapped[TrainingQuestion] = relationship(back_populates="options")


class TrainingAssignment(UUIDPKMixin, TimestampMixin, db.Model):
    """Audience + cadence rule. role_code or line_id may be null
    (= applies to anyone in that dimension)."""

    __tablename__ = "training_assignments"

    course_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_courses.id"), nullable=False
    )
    role_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    line_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("production_lines.id"), nullable=True
    )
    recurrence_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    course: Mapped[TrainingCourse] = relationship(back_populates="assignments")


class TrainingEnrolment(UUIDPKMixin, TimestampMixin, db.Model):
    """One issued magic link binding a trainee to a course version."""

    __tablename__ = "training_enrolments"

    trainee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trainees.id"), nullable=False
    )
    course_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_course_versions.id"), nullable=False
    )

    magic_token: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EnrolmentStatus.ISSUED.value
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EnrolmentSource.MANUAL.value
    )
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 0..len(modules); how far the trainee has progressed through reading.
    module_progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    trainee: Mapped[Trainee] = relationship()
    course_version: Mapped[TrainingCourseVersion] = relationship()
    attempt: Mapped["TrainingAttempt | None"] = relationship(
        back_populates="enrolment", uselist=False
    )


class TrainingAttempt(UUIDPKMixin, TimestampMixin, db.Model):
    """The act of taking the exam. One per enrolment."""

    __tablename__ = "training_attempts"

    enrolment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_enrolments.id"), unique=True, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Frozen response snapshot — list of {question_id, selected_option_ids, is_correct}
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    enrolment: Mapped[TrainingEnrolment] = relationship(back_populates="attempt")
    declaration: Mapped["TrainingDeclaration | None"] = relationship(
        back_populates="attempt", uselist=False
    )


class TrainingDeclaration(UUIDPKMixin, TimestampMixin, db.Model):
    """The signed declaration. Captures both typed name and drawn
    signature, plus full request metadata for audit."""

    __tablename__ = "training_declarations"

    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_attempts.id"), unique=True, nullable=False
    )
    typed_name: Mapped[str] = mapped_column(String(120), nullable=False)
    signature_png: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    declaration_text: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempt: Mapped[TrainingAttempt] = relationship(back_populates="declaration")


class TrainingCertification(UUIDPKMixin, TimestampMixin, db.Model):
    """Issued only when a trainee passes. course_id (NOT version) is the
    durable identity — 'Anna is current on Hygiene Refresher' regardless
    of which version she passed against."""

    __tablename__ = "training_certifications"

    trainee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trainees.id"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_courses.id"), nullable=False
    )
    course_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_course_versions.id"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_attempts.id"), nullable=False
    )
    declaration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("training_declarations.id"), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
