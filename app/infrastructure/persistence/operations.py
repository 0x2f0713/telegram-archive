"""Durable operation jobs and bounded web-facing logs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update

from app.domain import OperationStatus
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import OperationJob, OperationLog, utc_now

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


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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

    @classmethod
    def from_model(cls, model: OperationJob) -> OperationRecord:
        try:
            parameters = json.loads(model.parameters_json)
        except (TypeError, json.JSONDecodeError):
            parameters = {}
        if not isinstance(parameters, dict):
            parameters = {}
        return cls(
            id=model.id,
            command=model.command,
            status=model.status,
            phase=model.phase,
            parameters=parameters,
            detail=model.detail,
            progress_current=model.progress_current,
            progress_total=model.progress_total,
            chats_completed=model.chats_completed,
            chats_total=model.chats_total,
            messages_processed=model.messages_processed,
            downloads_completed=model.downloads_completed,
            retry_attempted=model.retry_attempted,
            retry_completed=model.retry_completed,
            stop_requested=model.stop_requested,
            error=model.error,
            created_at=_aware(model.created_at) or datetime.now(UTC),
            started_at=_aware(model.started_at),
            finished_at=_aware(model.finished_at),
            updated_at=_aware(model.updated_at) or datetime.now(UTC),
        )

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

    @classmethod
    def from_model(cls, model: OperationLog) -> OperationLogRecord:
        return cls(
            id=model.id,
            job_id=model.job_id,
            level=model.level,
            message=model.message,
            created_at=_aware(model.created_at) or datetime.now(UTC),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "level": self.level,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }


class OperationRepository:
    """Small repository used by both the controller and read-only web APIs."""

    def __init__(self, database: Database, *, max_logs_per_job: int = 300) -> None:
        self.database = database
        self.max_logs_per_job = max_logs_per_job

    async def create(self, command: str, parameters: dict[str, Any]) -> OperationRecord:
        async with self.database.sessions() as session, session.begin():
            model = OperationJob(
                command=command,
                status="queued",
                phase="queued",
                parameters_json=json.dumps(parameters, separators=(",", ":"), sort_keys=True),
                detail="Waiting for the operation worker",
            )
            session.add(model)
            await session.flush()
            record = OperationRecord.from_model(model)
        return record

    async def update(self, job_id: int, **values: Any) -> OperationRecord | None:
        if not values:
            return await self.get(job_id)
        values["updated_at"] = utc_now()
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(OperationJob).where(OperationJob.id == job_id).values(**values)
            )
        return await self.get(job_id)

    async def get(self, job_id: int) -> OperationRecord | None:
        async with self.database.sessions() as session:
            model = await session.get(OperationJob, job_id)
            return OperationRecord.from_model(model) if model else None

    async def recent(self, limit: int = 20) -> tuple[OperationRecord, ...]:
        statement = select(OperationJob).order_by(OperationJob.id.desc()).limit(limit)
        async with self.database.sessions() as session:
            models = (await session.scalars(statement)).all()
        return tuple(OperationRecord.from_model(model) for model in models)

    async def active(self) -> OperationRecord | None:
        statement = (
            select(OperationJob)
            .where(OperationJob.status.in_(ACTIVE_OPERATION_STATUSES))
            .order_by(OperationJob.id.desc())
            .limit(1)
        )
        async with self.database.sessions() as session:
            model = await session.scalar(statement)
        return OperationRecord.from_model(model) if model else None

    async def add_log(self, job_id: int, level: str, message: str) -> OperationLogRecord:
        normalized_level = level.upper()[:16]
        normalized_message = message.strip()[:4000] or "Operation update"
        async with self.database.sessions() as session, session.begin():
            model = OperationLog(
                job_id=job_id,
                level=normalized_level,
                message=normalized_message,
            )
            session.add(model)
            await session.flush()
            record = OperationLogRecord.from_model(model)
            stale_ids = (
                select(OperationLog.id)
                .where(OperationLog.job_id == job_id)
                .order_by(OperationLog.id.desc())
                .offset(self.max_logs_per_job)
            )
            await session.execute(delete(OperationLog).where(OperationLog.id.in_(stale_ids)))
        return record

    async def logs(self, job_id: int, limit: int = 100) -> tuple[OperationLogRecord, ...]:
        statement = (
            select(OperationLog)
            .where(OperationLog.job_id == job_id)
            .order_by(OperationLog.id.desc())
            .limit(limit)
        )
        async with self.database.sessions() as session:
            models = (await session.scalars(statement)).all()
        return tuple(OperationLogRecord.from_model(model) for model in reversed(models))

    async def mark_active_interrupted(self) -> int:
        """Close jobs orphaned by a previous web-process exit."""

        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(OperationJob.id).where(
                        OperationJob.status.in_(ACTIVE_OPERATION_STATUSES)
                    )
                )
            )
            if not ids:
                return 0
            await session.execute(
                update(OperationJob)
                .where(OperationJob.id.in_(ids))
                .values(
                    status="interrupted",
                    phase="interrupted",
                    detail="The web process exited before this operation finished",
                    error="Operation interrupted by application restart",
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                OperationLog(
                    job_id=job_id,
                    level="WARNING",
                    message="Marked interrupted while recovering web operation state",
                )
                for job_id in ids
            )
        return len(ids)
