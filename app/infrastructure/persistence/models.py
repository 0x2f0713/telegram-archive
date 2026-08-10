"""SQLAlchemy models for durable Telegram archive state."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    last_synced_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ArchiveSelectionPolicy(Base):
    """Singleton policy controlling where archive workers read target chat IDs."""

    __tablename__ = "archive_selection_policy"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_archive_selection_policy_singleton"),
        CheckConstraint("mode IN ('specific', 'all')", name="ck_archive_selection_policy_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SelectedChat(Base):
    """A chat explicitly selected when the selection policy is ``specific``."""

    __tablename__ = "selected_chats"

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ContentSyncCheckpoint(Base):
    """Per-chat high-water mark for one explicitly selected content category."""

    __tablename__ = "content_sync_checkpoints"

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_scanned_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "telegram_chat_id", "telegram_message_id", name="uq_messages_telegram_identity"
        ),
        Index("ix_messages_download_status", "download_status"),
        Index("ix_messages_chat_date", "telegram_chat_id", "message_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(String(512))
    text: Mapped[str | None] = mapped_column(Text)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(32))
    media_path: Mapped[str | None] = mapped_column(Text)
    media_size: Mapped[int | None] = mapped_column(BigInteger)
    telegram_document_id: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str | None] = mapped_column(Text)
    download_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    download_error: Mapped[str | None] = mapped_column(Text)
    download_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class OperationJob(Base):
    """Durable state for a command started from the web operations console."""

    __tablename__ = "operation_jobs"
    __table_args__ = (
        CheckConstraint(
            "command IN ('sync', 'listen', 'retry-failed', 'doctor')",
            name="ck_operation_jobs_command",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'stopping', 'completed', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_operation_jobs_status",
        ),
        Index("ix_operation_jobs_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    detail: Mapped[str | None] = mapped_column(Text)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    chats_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chats_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    downloads_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class OperationLog(Base):
    """Bounded, operator-facing log entries for a web operation."""

    __tablename__ = "operation_logs"
    __table_args__ = (Index("ix_operation_logs_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("operation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RuntimeSetting(Base):
    """An operator-edited override on top of environment configuration.

    Values are stored as plain strings and validated again at load time by
    the settings codec, so a corrupt row can never crash application startup.
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
