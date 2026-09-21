"""SQLAlchemy models for the Siyona control plane.

A user sends one instruction, which becomes a ``Task``. The orchestrator may
place several ``Call`` attempts for that task; each call accumulates
``Transcript`` turns and resolves to a single ``Outcome``.

Create the schema against a throwaway SQLite database:

    python -c "from services.api.models import Base, engine_for; \
        Base.metadata.create_all(engine_for('sqlite://'))"
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Transcripts and recordings are purged this many days after the call ends.
DEFAULT_RETENTION_DAYS = 30


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every Siyona table."""


class TaskStatus(StrEnum):
    """Lifecycle of a user instruction."""

    RECEIVED = "received"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    NEEDS_USER_INPUT = "needs_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CallStatus(StrEnum):
    """Lifecycle of a single outbound call attempt."""

    DIALING = "dialing"
    IN_IVR = "in_ivr"
    CONNECTED = "connected"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    FAILED = "failed"


class Speaker(StrEnum):
    """Who produced a transcript turn."""

    AGENT = "agent"
    CALLEE = "callee"
    SYSTEM = "system"


class Task(Base):
    """One instruction from a user, e.g. 'book a table for four at 7pm'."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_phone: Mapped[str] = mapped_column(String(32), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False), default=TaskStatus.RECEIVED, index=True
    )
    retention_days: Mapped[int] = mapped_column(Integer, default=DEFAULT_RETENTION_DAYS)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    calls: Mapped[list[Call]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Call.created_at"
    )
    outcome: Mapped[Outcome | None] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status.value}>"


class Call(Base):
    """One outbound call attempt made in service of a task."""

    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    callee_number: Mapped[str] = mapped_column(String(32))
    provider_call_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[CallStatus] = mapped_column(
        Enum(CallStatus, native_enum=False), default=CallStatus.DIALING, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    median_turn_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="calls")
    transcripts: Mapped[list[Transcript]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="Transcript.turn_index"
    )

    def __repr__(self) -> str:
        return f"<Call {self.id} {self.status.value} attempt={self.attempt}>"


class Transcript(Base):
    """A single turn of speech within a call."""

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker, native_enum=False))
    text: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    spoken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    call: Mapped[Call] = relationship(back_populates="transcripts")

    def __repr__(self) -> str:
        return f"<Transcript {self.call_id}#{self.turn_index} {self.speaker.value}>"


class Outcome(Base):
    """The consolidated result reported back to the user on WhatsApp."""

    __tablename__ = "outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, index=True
    )
    succeeded: Mapped[bool] = mapped_column(default=False)
    summary: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="outcome")

    def __repr__(self) -> str:
        return f"<Outcome {self.task_id} succeeded={self.succeeded}>"


def engine_for(url: str = "sqlite://", echo: bool = False) -> Engine:
    """Create an engine and the schema on it. Defaults to in-memory SQLite."""
    engine = create_engine(url, echo=echo, future=True)
    Base.metadata.create_all(engine)
    return engine
