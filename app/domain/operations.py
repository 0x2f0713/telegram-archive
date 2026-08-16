"""Long-running archiver workflows and their lifecycle states."""

from __future__ import annotations

from enum import StrEnum


class OperationCommand(StrEnum):
    """Allowlisted workflows a web operator may start or resume."""

    SYNC = "sync"
    LISTEN = "listen"
    RETRY_FAILED = "retry-failed"
    DOCTOR = "doctor"
    OPTIMIZE_MEDIA = "optimize-media"


class OperationStatus(StrEnum):
    """Lifecycle of one durable web operation."""

    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def is_active(self) -> bool:
        return self in {
            OperationStatus.QUEUED,
            OperationStatus.RUNNING,
            OperationStatus.STOPPING,
        }

    @property
    def is_terminal(self) -> bool:
        return self in {
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.INTERRUPTED,
        }

    @property
    def is_recoverable(self) -> bool:
        """Whether an unfinished operation can be resumed or retried."""
        return self in {
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.INTERRUPTED,
        }
