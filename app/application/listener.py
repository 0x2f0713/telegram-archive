"""Real-time Telegram new-message and edit listener."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from telethon import events

from app.application.archive import ArchiveService
from app.config import Settings
from app.domain import ChatInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ListenerProgress:
    chat_id: int
    chat_title: str
    message_id: int
    edited: bool
    downloaded: bool
    skipped: bool


ListenerProgressCallback = Callable[[ListenerProgress], Awaitable[None]]


class RealtimeListener:
    def __init__(
        self,
        client: Any,
        chats: Mapping[int, ChatInfo],
        archive: ArchiveService,
        settings: Settings,
        *,
        progress: ListenerProgressCallback | None = None,
        manage_signals: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.client = client
        self.chats = chats
        self.archive = archive
        self.settings = settings
        self.progress = progress
        self.manage_signals = manage_signals
        self.stop_event = stop_event or asyncio.Event()
        self.pending: set[asyncio.Task[Any]] = set()
        self._handlers_installed = False

    def request_stop(self) -> None:
        if not self.stop_event.is_set():
            logger.info("Shutdown requested; stopping new work")
            self.stop_event.set()

    def _schedule(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        self.pending.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self.pending.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Real-time message processing failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _new_message(self, event: Any) -> None:
        if self.stop_event.is_set():
            return
        chat = self.chats.get(event.chat_id)
        if chat and self.archive.matches_message(event.message):
            self._schedule(self._process_event(event.message, chat, edited=False))

    async def _edited_message(self, event: Any) -> None:
        if self.stop_event.is_set():
            return
        chat = self.chats.get(event.chat_id)
        if chat and self.archive.matches_message(event.message):
            self._schedule(self._process_event(event.message, chat, edited=True))

    async def _process_event(self, message: Any, chat: ChatInfo, *, edited: bool) -> None:
        result = await self.archive.process_message(message, chat, edited=edited)
        if self.progress:
            await self.progress(
                ListenerProgress(
                    chat_id=chat.telegram_chat_id,
                    chat_title=chat.title,
                    message_id=int(message.id),
                    edited=edited,
                    downloaded=result.downloaded,
                    skipped=result.skipped,
                )
            )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_stop)
            except (NotImplementedError, RuntimeError):
                # Windows and non-main-thread event loops may not support this;
                # KeyboardInterrupt is still handled by the CLI boundary.
                pass

    def install_handlers(self) -> None:
        """Begin accepting configured update events; safe to call more than once."""

        if self._handlers_installed:
            return
        if self.manage_signals:
            self._install_signal_handlers()
        entities = [chat.entity for chat in self.chats.values()]
        self.client.add_event_handler(self._new_message, events.NewMessage(chats=entities))
        self.client.add_event_handler(self._edited_message, events.MessageEdited(chats=entities))
        self._handlers_installed = True

    async def run(self) -> None:
        if not self.chats:
            raise ValueError("No enabled TARGET_CHATS are configured")
        chat_ids = list(self.chats)
        self.install_handlers()
        logger.info("Monitoring %s chats", len(chat_ids))

        reconnect_attempt = 0
        try:
            while not self.stop_event.is_set():
                disconnected = asyncio.create_task(self.client.run_until_disconnected())
                stopping = asyncio.create_task(self.stop_event.wait())
                done, pending = await asyncio.wait(
                    {disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopping in done and self.stop_event.is_set():
                    break
                error = disconnected.exception() if not disconnected.cancelled() else None
                reconnect_attempt += 1
                delay = min(60, 2 ** min(reconnect_attempt, 6))
                logger.warning(
                    "Telegram connection ended%s; reconnecting in %ss",
                    f": {error}" if error else "",
                    delay,
                )
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    break
                except TimeoutError:
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        raise RuntimeError("Telegram session authorization was revoked") from None
        finally:
            # Disconnect first so no new update handlers begin, then allow valid
            # in-flight transactions/downloads a bounded interval to finish.
            await self.client.disconnect()
            if self.pending:
                logger.info("Waiting for %s in-flight tasks", len(self.pending))
                done, pending = await asyncio.wait(
                    self.pending, timeout=self.settings.shutdown_timeout_seconds
                )
                if pending:
                    logger.warning("Cancelling %s unfinished downloads", len(pending))
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            logger.info("Listener stopped cleanly")
