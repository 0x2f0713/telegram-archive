"""Application-owned records for durable operator workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain import OperationStatus

ACTIVE_OPERATION_STATUSES = frozenset(
    status.value
    for status in (OperationStatus.QUEUED, OperationStatus.RUNNING, OperationStatus.STOPPING)
)
TERMINAL_OPERATION_STATUSES = frozenset(
    status.value
    for status in (
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
        OperationStatus.INTERRUPTED,
    )
)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    id: int
    command: str
    status: str
    phase: str
    parameters: dict[str, Any]
    detail: str | None
    progress_current: int
    progress_total: int | None
    chats_completed: int
    chats_total: int
    messages_processed: int
    downloads_completed: int
    retry_attempted: int
    retry_completed: int
    stop_requested: bool
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in ("created_at", "started_at", "finished_at", "updated_at"):
            value = payload[name]
            payload[name] = value.isoformat() if value else None
        payload["terminal"] = self.status in TERMINAL_OPERATION_STATUSES
        payload["active"] = self.status in ACTIVE_OPERATION_STATUSES
        payload["progress_percent"] = (
            min(100, round(self.progress_current / self.progress_total * 100, 1))
            if self.progress_total and self.progress_total > 0
            else None
        )
        started = self.started_at or self.created_at
        ended = self.finished_at or datetime.now(UTC)
        payload["elapsed_seconds"] = max(0, round((ended - started).total_seconds()))
        return payload


@dataclass(frozen=True, slots=True)
class OperationLogRecord:
    id: int
    job_id: int
    level: str
    message: str
    created_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "level": self.level,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }
