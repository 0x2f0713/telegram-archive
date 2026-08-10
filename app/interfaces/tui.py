"""Textual application for keyboard-first archive exploration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import ClassVar

from pydantic import ValidationError
from rich.text import Text
from telethon.errors import RPCError
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from app.application.chat_selection import ChatSelectionService
from app.config import ConfigurationError, Settings, apply_runtime_overrides
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.read_models import (
    DashboardRepository,
    DashboardService,
    MessageQuery,
    MessageView,
)
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelection, ChatSelectionRepository
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.infrastructure.telegram.client import TelegramAccessError
from app.utils.logging import format_bytes

logger = logging.getLogger(__name__)


async def _effective_settings(settings: Settings, database: Database) -> Settings:
    """Return settings with durable web overrides applied, or the originals."""
    try:
        overrides = await RuntimeSettingsRepository(database).overrides()
        return apply_runtime_overrides(settings, overrides)
    except (ValidationError, ValueError):
        return settings


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "Never"


def _preview(value: str | None, width: int = 68) -> str:
    if not value:
        return "Media message"
    compact = " ".join(value.split())
    return compact if len(compact) <= width else f"{compact[: width - 1]}…"


class Metric(Static):
    """Single dashboard number with a stable semantic label."""

    def set_value(self, label: str, value: str, note: str, *, alert: bool = False) -> None:
        content = Text()
        content.append(f"{label}\n", style="dim")
        content.append(f"{value}\n", style="bold #ff938f" if alert else "bold #9ee37d")
        content.append(note, style="dim")
        self.update(content)


class ArchiveTui(App[None]):
    """Archive explorer and durable chat-selection control surface."""

    TITLE = "Telegram Archiver"
    SUB_TITLE = "Private archive console"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "focus_search", "Search"),
        Binding("space", "toggle_chat", "Toggle chat", show=False),
        Binding("a", "select_all_chats", "All chats", show=False),
        Binding("c", "clear_chat_selection", "Clear chats", show=False),
        Binding("e", "use_environment", "Environment", show=False),
        Binding("g", "refresh_chats", "Refresh Telegram", show=False),
        Binding("1", "show_tab('overview-tab')", "Overview", show=False),
        Binding("2", "show_tab('messages-tab')", "Messages", show=False),
        Binding("3", "show_tab('chats-tab')", "Chats", show=False),
        Binding("4", "show_tab('downloads-tab')", "Downloads", show=False),
        Binding("5", "show_tab('attention-tab')", "Attention", show=False),
    ]

    CSS = """
    Screen {
        background: #081210;
        color: #edf5e9;
    }

    Header {
        background: #0e1a17;
        color: #edf5e9;
        border-bottom: solid #263b34;
    }

    Footer {
        background: #0e1a17;
        color: #b3c1b7;
        border-top: solid #263b34;
    }

    #workspace {
        height: 1fr;
        padding: 1 2;
    }

    #metric-grid {
        grid-size: 5;
        grid-gutter: 1;
        height: 7;
        margin-bottom: 1;
    }

    Metric {
        height: 7;
        padding: 1 2;
        background: #0e1a17;
        border: solid #263b34;
    }

    TabbedContent {
        height: 1fr;
    }

    Tabs {
        background: #081210;
        color: #b3c1b7;
    }

    Tab.-active {
        color: #9ee37d;
        text-style: bold;
    }

    TabPane {
        padding: 1 0 0 0;
    }

    .section-title {
        height: 2;
        color: #edf5e9;
        text-style: bold;
    }

    .section-note {
        height: 2;
        color: #8fa097;
    }

    #overview-body {
        layout: grid;
        grid-size: 2;
        grid-columns: 2fr 1fr;
        grid-gutter: 1;
        height: 1fr;
    }

    .pane {
        height: 1fr;
        padding: 1;
        background: #0e1a17;
        border: solid #263b34;
    }

    DataTable {
        height: 1fr;
        background: #0e1a17;
        color: #edf5e9;
        border: solid #263b34;
    }

    DataTable > .datatable--header {
        background: #14221e;
        color: #b3c1b7;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #203d27;
        color: #edf5e9;
    }

    Input {
        height: 3;
        margin-bottom: 1;
        background: #0e1a17;
        color: #edf5e9;
        border: solid #3b5149;
    }

    Input:focus {
        border: solid #9ee37d;
    }

    #message-detail, #attention-detail {
        min-height: 6;
        max-height: 10;
        margin-top: 1;
        padding: 1 2;
        background: #0e1a17;
        border: solid #263b34;
        overflow-y: auto;
    }

    #activity-summary, #state-summary {
        height: auto;
        min-height: 7;
        padding: 1 2;
        background: #0e1a17;
    }

    #refresh-state {
        dock: bottom;
        height: 1;
        color: #8fa097;
        text-align: right;
    }

    #chat-selection-state {
        height: auto;
        min-height: 3;
        padding: 0 1;
        color: #b3c1b7;
    }

    #chat-actions {
        height: 4;
        margin-bottom: 1;
    }

    #chat-actions Button {
        min-width: 16;
        margin-right: 1;
        background: #14221e;
        color: #edf5e9;
        border: solid #3b5149;
    }

    #chat-actions Button:focus {
        border: solid #9ee37d;
    }

    Screen.narrow #metric-grid {
        grid-size: 3;
        grid-rows: 7 7;
        height: 15;
    }

    Screen.narrow #overview-body {
        grid-size: 1;
        grid-rows: 1fr 11;
    }
    """

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.database: Database | None = None
        self.dashboard: DashboardRepository | None = None
        self.service: DashboardService | None = None
        self.archive: ArchiveRepository | None = None
        self.selections: ChatSelectionRepository | None = None
        self.selection_service: ChatSelectionService | None = None
        self.selection = ChatSelection("environment")
        self.selected_chat_ids: set[int] = set()
        self.discovery_note = "Telegram dialogs have not been refreshed"
        self.message_cache: dict[int, MessageView] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="workspace"):
            with Grid(id="metric-grid"):
                yield Metric(id="metric-messages")
                yield Metric(id="metric-files")
                yield Metric(id="metric-storage")
                yield Metric(id="metric-chats")
                yield Metric(id="metric-failed")
            with TabbedContent(initial="overview-tab"):
                with TabPane("Overview", id="overview-tab"):
                    with Grid(id="overview-body"):
                        with Vertical(classes="pane"):
                            yield Label("Chat coverage", classes="section-title")
                            yield DataTable(id="overview-chats", cursor_type="row")
                        with Vertical(classes="pane"):
                            yield Label("Fourteen-day activity", classes="section-title")
                            yield Static(id="activity-summary")
                            yield Label("Download states", classes="section-title")
                            yield Static(id="state-summary")
                with TabPane("Messages", id="messages-tab"):
                    yield Input(
                        placeholder="Search captions, senders, files, or chats. Press Enter.",
                        id="message-search",
                    )
                    yield DataTable(id="messages-table", cursor_type="row")
                    yield Static(
                        "Select a message to inspect its metadata and text.", id="message-detail"
                    )
                with TabPane("Chats", id="chats-tab"):
                    yield Static(
                        "Loading durable chat selection…",
                        id="chat-selection-state",
                    )
                    with Horizontal(id="chat-actions"):
                        yield Button("All accessible", id="select-all-chats")
                        yield Button("Clear", id="clear-chat-selection")
                        yield Button("Environment", id="environment-chat-selection")
                        yield Button("Refresh Telegram", id="refresh-telegram-chats")
                    yield DataTable(id="chats-table", cursor_type="row")
                with TabPane("Downloads", id="downloads-tab"):
                    yield DataTable(id="downloads-table", cursor_type="row")
                with TabPane("Attention", id="attention-tab"):
                    yield Label(
                        "Failed, interrupted, and pending media, ordered by recovery priority.",
                        classes="section-note",
                    )
                    yield DataTable(id="attention-table", cursor_type="row")
                    yield Static(
                        "Select a problem record to inspect its error and metadata.",
                        id="attention-detail",
                    )
            yield Static("Starting archive view", id="refresh-state")
        yield Footer()

    async def on_mount(self) -> None:
        self.database = Database(self.settings.database_url)
        await self.database.initialize()
        self.settings = await _effective_settings(self.settings, self.database)
        self.dashboard = DashboardRepository(self.database)
        self.archive = ArchiveRepository(self.database)
        self.selections = ChatSelectionRepository(self.database)
        self.selection_service = ChatSelectionService(self.settings, self.archive)
        self.service = DashboardService(self.database, self.settings.configured_chat_ids)
        self._setup_tables()
        if not await self.dashboard.chat_summaries():
            await self._discover_chats(notify=False)
        else:
            self.discovery_note = "Using cached dialogs; press G to refresh Telegram."
        await self.refresh_data()
        self.set_interval(self.settings.tui_refresh_seconds, self.refresh_data)

    def on_resize(self, event: events.Resize) -> None:
        """Switch to a stacked layout for narrower terminals."""

        self.screen.set_class(event.size.width <= 100, "narrow")

    async def on_unmount(self) -> None:
        if self.database:
            await self.database.close()

    def _setup_tables(self) -> None:
        overview = self.query_one("#overview-chats", DataTable)
        overview.add_columns("Archive", "Chat", "Type", "Messages", "Files", "Failed", "Latest")
        messages = self.query_one("#messages-table", DataTable)
        messages.add_columns("ID", "Chat", "Sender", "Message", "Media", "State", "Date")
        chats = self.query_one("#chats-table", DataTable)
        chats.add_columns(
            "Archive", "Chat ID", "Title", "Type", "Messages", "Media", "Checkpoint", "Latest"
        )
        downloads = self.query_one("#downloads-table", DataTable)
        downloads.add_columns("Message", "Chat", "Type", "Size", "State", "Attempts", "Filename")
        attention = self.query_one("#attention-table", DataTable)
        attention.add_columns("Message", "Chat", "State", "Attempts", "Error", "Filename")
        for table in (overview, messages, chats, downloads, attention):
            table.zebra_stripes = True

    async def refresh_data(self) -> None:
        if not self.service or not self.dashboard:
            return
        try:
            if self.selections:
                self.selection = await self.selections.policy()
                self.selected_chat_ids = set(
                    await self.selections.effective_known_ids(self.settings.configured_chat_ids)
                )
            overview = await self.service.overview()
            self.query_one("#metric-messages", Metric).set_value(
                "MESSAGES", f"{overview.stats.total_messages:,}", "metadata records"
            )
            self.query_one("#metric-files", Metric).set_value(
                "FILES", f"{overview.stats.downloaded_files:,}", "completed media"
            )
            self.query_one("#metric-storage", Metric).set_value(
                "STORAGE", format_bytes(overview.stats.downloaded_bytes), "downloaded bytes"
            )
            self.query_one("#metric-chats", Metric).set_value(
                "CHATS", str(len(overview.configured_chat_ids)), "selected targets"
            )
            self.query_one("#metric-failed", Metric).set_value(
                "FAILED",
                f"{overview.stats.failed_downloads:,}",
                "retryable media",
                alert=overview.stats.failed_downloads > 0,
            )
            self._populate_chat_tables(overview.chats)
            self._update_selection_state(len(overview.chats))
            self._populate_overview_summaries(overview.activity, overview.status_counts)
            await self._populate_messages()
            await self._populate_downloads()
            self._populate_attention(overview.attention_messages)
            self.query_one("#refresh-state", Static).update(
                f"Refreshed {_date(datetime.now())} | r refreshes | / searches"
            )
        except Exception as exc:
            self.query_one("#refresh-state", Static).update(
                Text(f"Refresh failed: {type(exc).__name__}: {exc}", style="bold #f08a87")
            )

    def _populate_chat_tables(self, chats: tuple) -> None:
        overview_table = self.query_one("#overview-chats", DataTable)
        chat_table = self.query_one("#chats-table", DataTable)
        overview_table.clear(columns=False)
        chat_table.clear(columns=False)
        for chat in chats:
            selected = chat.telegram_chat_id in self.selected_chat_ids
            state = (
                Text("● selected", style="bold #9ee37d") if selected else Text("○ off", style="dim")
            )
            overview_table.add_row(
                state,
                Text(chat.title),
                chat.type,
                f"{chat.message_count:,}",
                f"{chat.completed_count:,}",
                f"{chat.failed_count:,}",
                _date(chat.newest_message_date),
                key=str(chat.telegram_chat_id),
            )
            chat_table.add_row(
                state,
                str(chat.telegram_chat_id),
                Text(chat.title),
                chat.type,
                f"{chat.message_count:,}",
                f"{chat.media_count:,}",
                str(chat.last_synced_message_id or "Not started"),
                _date(chat.newest_message_date),
                key=str(chat.telegram_chat_id),
            )

    def _update_selection_state(self, known_count: int) -> None:
        label = {
            "all": "ALL ACCESSIBLE",
            "specific": "SPECIFIC CHATS",
            "environment": "ENVIRONMENT DEFAULTS",
        }[self.selection.mode]
        text = Text()
        text.append(f"{label}  ", style="bold #9ee37d")
        text.append(
            f"{len(self.selected_chat_ids)} of {known_count} known chats selected. ", style="bold"
        )
        text.append(
            "Space toggles the focused chat · A selects all · C clears · E restores env · "
            f"G refreshes Telegram. {self.discovery_note}",
            style="dim",
        )
        if self.selection.mode == "all":
            text.append(
                " All mode includes private dialogs and future dialogs discovered at worker startup.",
                style="bold #e9c873",
            )
        self.query_one("#chat-selection-state", Static).update(text)

    def _populate_overview_summaries(self, activity: tuple, status_counts: tuple) -> None:
        bars = "▁▂▃▄▅▆▇█"
        highest = max((point.count for point in activity), default=0)
        graph = "".join(
            bars[min(len(bars) - 1, round(point.count / highest * (len(bars) - 1)))]
            if highest
            else bars[0]
            for point in activity
        )
        activity_text = Text()
        activity_text.append(f"{graph}\n", style="#9ee37d")
        if activity:
            activity_text.append(
                f"{activity[0].day:%b %d}  ·  14 days  ·  {activity[-1].day:%b %d}",
                style="dim",
            )
        self.query_one("#activity-summary", Static).update(activity_text)

        state_text = Text()
        if status_counts:
            for status, count in status_counts:
                state_text.append(f"{status.replace('_', ' '):16}", style="dim")
                state_text.append(f"{count:,}\n", style="bold")
        else:
            state_text.append("No media states yet. Run sync to populate them.", style="dim")
        self.query_one("#state-summary", Static).update(state_text)

    async def _populate_messages(self) -> None:
        if not self.dashboard:
            return
        search = self.query_one("#message-search", Input).value
        page = await self.dashboard.messages(MessageQuery(search=search, page_size=75))
        table = self.query_one("#messages-table", DataTable)
        table.clear(columns=False)
        self.message_cache = {message.id: message for message in page.items}
        for message in page.items:
            table.add_row(
                str(message.telegram_message_id),
                Text(message.chat_title),
                Text(message.sender_name or "Unknown sender"),
                Text(_preview(message.text or message.filename)),
                message.media_type or "None",
                message.download_status.replace("_", " "),
                _date(message.message_date),
                key=str(message.id),
            )

    async def _populate_downloads(self) -> None:
        if not self.dashboard:
            return
        page = await self.dashboard.messages(MessageQuery(media_only=True, page_size=100))
        table = self.query_one("#downloads-table", DataTable)
        table.clear(columns=False)
        for message in page.items:
            table.add_row(
                str(message.telegram_message_id),
                Text(message.chat_title),
                message.media_type or "Unknown",
                format_bytes(message.media_size or 0),
                message.download_status.replace("_", " "),
                str(message.download_attempts),
                Text(message.filename or "Generated filename"),
                key=str(message.id),
            )

    def _populate_attention(self, messages: tuple[MessageView, ...]) -> None:
        table = self.query_one("#attention-table", DataTable)
        table.clear(columns=False)
        for message in messages:
            self.message_cache[message.id] = message
            table.add_row(
                str(message.telegram_message_id),
                Text(message.chat_title),
                message.download_status.replace("_", " "),
                str(message.download_attempts),
                Text(
                    _preview(message.download_error, 42)
                    if message.download_error
                    else "Awaiting work"
                ),
                Text(message.filename or "Generated filename"),
                key=str(message.id),
            )

    @on(Input.Submitted, "#message-search")
    async def search_submitted(self) -> None:
        await self._populate_messages()
        self.query_one("#messages-table", DataTable).focus()

    @on(DataTable.RowSelected, "#messages-table")
    def message_selected(self, event: DataTable.RowSelected) -> None:
        self._show_message_detail(event, "#message-detail")

    @on(DataTable.RowSelected, "#attention-table")
    def attention_selected(self, event: DataTable.RowSelected) -> None:
        self._show_message_detail(event, "#attention-detail")

    def _show_message_detail(self, event: DataTable.RowSelected, target: str) -> None:
        try:
            message_id = int(str(event.row_key.value))
        except (TypeError, ValueError):
            return
        message = self.message_cache.get(message_id)
        if not message:
            return
        detail = Text()
        detail.append(
            f"{message.chat_title} / message {message.telegram_message_id}\n", style="bold"
        )
        detail.append(
            f"Sender: {message.sender_name or message.sender_id or 'Unknown'}\n", style="dim"
        )
        detail.append(
            f"State: {message.download_status}  Media: {message.media_type or 'none'}  "
            f"Size: {format_bytes(message.media_size or 0)}\n",
            style="dim",
        )
        detail.append(message.text or message.filename or "No text or filename")
        if message.download_error:
            detail.append(f"\nError: {message.download_error}", style="#ff938f")
        self.query_one(target, Static).update(detail)

    async def action_refresh(self) -> None:
        await self.refresh_data()
        self.notify("Archive view refreshed", timeout=2)

    async def _discover_chats(self, *, notify: bool = True) -> None:
        if not self.selection_service:
            return
        try:
            discovery = await self.selection_service.discover()
            self.discovery_note = (
                f"Telegram refreshed: {len(discovery.dialogs)} accessible dialogs."
            )
            if notify:
                self.notify(self.discovery_note, timeout=3)
        except (ConfigurationError, TelegramAccessError) as exc:
            self.discovery_note = str(exc)
            if notify:
                self.notify(self.discovery_note, severity="warning", timeout=5)
        except (RPCError, OSError, TimeoutError) as exc:
            self.discovery_note = f"Telegram refresh unavailable: {type(exc).__name__}"
            if notify:
                self.notify(self.discovery_note, severity="warning", timeout=5)
        except Exception as exc:
            logger.exception("Unexpected Telegram dialog discovery failure")
            self.discovery_note = f"Telegram refresh failed unexpectedly: {type(exc).__name__}"
            if notify:
                self.notify(self.discovery_note, severity="error", timeout=5)

    def _focused_chat_id(self) -> int | None:
        tabs = self.query_one(TabbedContent)
        table = self.query_one("#chats-table", DataTable)
        if tabs.active != "chats-tab" or not table.row_count:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return int(str(cell_key.row_key.value))
        except (KeyError, TypeError, ValueError):
            return None

    async def action_toggle_chat(self) -> None:
        chat_id = self._focused_chat_id()
        if chat_id is None or not self.selections:
            return
        selected = set(self.selected_chat_ids)
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.add(chat_id)
        await self.selections.set_specific(selected)
        await self.refresh_data()
        self.notify(f"Chat {chat_id} {'selected' if chat_id in selected else 'removed'}", timeout=2)

    async def action_select_all_chats(self) -> None:
        if not self.selections:
            return
        await self.selections.set_all()
        await self.refresh_data()
        self.notify("All accessible chats will be archived at worker startup", timeout=3)

    async def action_clear_chat_selection(self) -> None:
        if not self.selections:
            return
        await self.selections.set_specific(())
        await self.refresh_data()
        self.notify("Chat selection cleared", timeout=2)

    async def action_use_environment(self) -> None:
        if not self.selections:
            return
        await self.selections.use_environment()
        await self.refresh_data()
        self.notify("TARGET_CHATS and YAML defaults restored", timeout=3)

    async def action_refresh_chats(self) -> None:
        await self._discover_chats()
        await self.refresh_data()

    @on(Button.Pressed, "#select-all-chats")
    async def select_all_button(self) -> None:
        await self.action_select_all_chats()

    @on(Button.Pressed, "#clear-chat-selection")
    async def clear_selection_button(self) -> None:
        await self.action_clear_chat_selection()

    @on(Button.Pressed, "#environment-chat-selection")
    async def environment_selection_button(self) -> None:
        await self.action_use_environment()

    @on(Button.Pressed, "#refresh-telegram-chats")
    async def refresh_chats_button(self) -> None:
        await self.action_refresh_chats()

    def action_focus_search(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "messages-tab"
        self.query_one("#message-search", Input).focus()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id
