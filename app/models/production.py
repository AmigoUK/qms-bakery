from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db


class ProductionLine(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "production_lines"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    pipelines: Mapped[list["Pipeline"]] = relationship(
        back_populates="line", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ProductionLine {self.code}>"


class Pipeline(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "pipelines"

    line_id: Mapped[str] = mapped_column(String(36), ForeignKey("production_lines.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    line: Mapped[ProductionLine] = relationship(back_populates="pipelines")
    stages: Mapped[list["PipelineStage"]] = relationship(
        back_populates="pipeline",
        order_by="PipelineStage.order_index",
        cascade="all, delete-orphan",
    )

    __table_args__ = (UniqueConstraint("line_id", "version", name="uq_pipeline_line_version"),)

    def __repr__(self) -> str:
        return f"<Pipeline line={self.line_id} v{self.version}>"


class PipelineStage(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "pipeline_stages"

    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id"))
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSONB on PG, TEXT JSON on SQLite — same API via SQLAlchemy `JSON`.
    name: Mapped[dict] = mapped_column(JSON, nullable=False)
    required_role_code: Mapped[str | None] = mapped_column(String(32))
    sla_minutes: Mapped[int | None] = mapped_column(Integer)
    is_ccp_checkpoint: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    pipeline: Mapped[Pipeline] = relationship(back_populates="stages")

    __table_args__ = (
        UniqueConstraint("pipeline_id", "order_index", name="uq_stage_pipeline_order"),
    )

    def __repr__(self) -> str:
        return f"<PipelineStage {self.code} @ {self.order_index}>"
