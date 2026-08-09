from pathlib import Path

from textual.widgets import DataTable, Input, Static, TabbedContent

from app.config import Settings
from app.database.repository import ArchiveRepository
from app.database.selection import ChatSelection
from app.database.session import Database
from app.tui import ArchiveTui
from tests.helpers import make_chat, make_message


async def test_tui_launches_refreshes_and_searches(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'tui.db'}",
        download_dir=tmp_path / "downloads",
        target_chats="-1001234567890",
        tui_refresh_seconds=3600,
    )
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="Terminal Room"))
    await archive.upsert_message(make_message(text="searchable release note"))
    await database.close()

    app = ArchiveTui(settings)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#metric-messages", Static).render().plain.splitlines()[1] == "1"
        assert app.query_one("#overview-chats", DataTable).row_count == 1
        assert app.query_one("#attention-table", DataTable).row_count == 1

        app.action_focus_search()
        search = app.query_one("#message-search", Input)
        search.value = "searchable"
        await search.action_submit()
        await pilot.pause()

        assert app.query_one(TabbedContent).active == "messages-tab"
        assert app.query_one("#messages-table", DataTable).row_count == 1


async def test_tui_can_select_all_clear_and_restore_environment(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'tui-selection.db'}",
        target_chats="-1001234567890",
        tui_refresh_seconds=3600,
    )
    database = Database(settings.database_url)
    await database.initialize()
    await ArchiveRepository(database).upsert_chat(make_chat(title="Terminal Room"))
    await database.close()

    app = ArchiveTui(settings)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "chats-tab"

        await pilot.press("a")
        await pilot.pause()
        assert app.selections is not None
        assert await app.selections.policy() == ChatSelection("all")

        await pilot.press("c")
        await pilot.pause()
        assert await app.selections.policy() == ChatSelection("specific", ())

        app.query_one("#chats-table", DataTable).focus()
        await pilot.press("space")
        await pilot.pause()
        assert await app.selections.policy() == ChatSelection("specific", (-1001234567890,))

        await pilot.press("e")
        await pilot.pause()
        assert await app.selections.policy() == ChatSelection("environment")
