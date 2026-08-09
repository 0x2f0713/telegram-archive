from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.config import ConfigurationError, Settings
from app.database.repository import ArchiveRepository
from app.database.selection import ChatSelection, ChatSelectionRepository
from app.database.session import Database
from app.services.chat_selection import ChatDiscovery
from app.services.operations import OperationContext, OperationManager
from app.web.application import create_web_app
from app.web.telegram_auth import TelegramAuthSnapshot, TelegramAuthStatus
from tests.helpers import make_chat, make_message


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'web.db'}",
        "download_dir": tmp_path / "downloads",
        "tg_session_name": tmp_path / "telegram_session",
        "target_chats": "-1001234567890",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def _seed(settings: Settings, *, outside_media: bool = False) -> tuple[int, Path]:
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="Release <Room>"))
    record, _ = await archive.upsert_message(
        make_message(
            text="Quarterly <script>alert(1)</script> package",
            sender_name="Alice & Bob",
            message_date=datetime.now(UTC),
        )
    )
    if outside_media:
        media_path = settings.download_dir.parent / "outside.pdf"
    else:
        media_path = settings.download_dir / "release" / "42_report.pdf"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"safe archive bytes")
    await archive.mark_download_completed(record.id, media_path, media_path.stat().st_size)
    await database.close()
    return record.id, media_path


async def test_web_pages_api_and_media_delivery(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    message_id, _ = await _seed(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            dashboard = await client.get("/")
            messages = await client.get("/messages", params={"q": "quarterly"})
            detail = await client.get(f"/messages/{message_id}")
            media = await client.get(f"/media/{message_id}")
            stats = await client.get("/api/v1/stats")
            api_messages = await client.get("/api/v1/messages", params={"q": "Alice"})

    assert dashboard.status_code == 200
    assert "Keep every message" in dashboard.text
    assert dashboard.headers["content-security-policy"].startswith("default-src 'self'")
    assert messages.status_code == 200
    assert "Quarterly &lt;script&gt;" in messages.text
    assert "<script>alert(1)</script>" not in messages.text
    assert detail.status_code == 200
    assert media.status_code == 200
    assert media.content == b"safe archive bytes"
    assert media.headers["content-disposition"].startswith("attachment")
    assert stats.json()["stats"]["total_messages"] == 1
    assert api_messages.json()["total"] == 1


async def test_web_rejects_media_outside_download_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    message_id, _ = await _seed(settings, outside_media=True)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/media/{message_id}")

    assert response.status_code == 403


def test_remote_bind_requires_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_host="0.0.0.0")

    with pytest.raises(ConfigurationError, match="WEB_PASSWORD is required"):
        create_web_app(settings)


async def test_basic_auth_protects_every_route(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_host="0.0.0.0", web_password="correct horse")
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.get("/healthz")
            accepted = await client.get("/healthz", auth=(settings.web_username, "correct horse"))

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic")
    assert denied.headers["content-security-policy"].startswith("default-src 'self'")
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok"}


class _FakeTelegramAuth:
    def __init__(self) -> None:
        self.snapshot = TelegramAuthSnapshot(
            TelegramAuthStatus.IDLE,
            "Ready for a local connection.",
        )
        self.started = False

    async def inspect_session(self) -> TelegramAuthSnapshot:
        return self.snapshot

    async def start(self) -> TelegramAuthSnapshot:
        self.started = True
        self.snapshot = TelegramAuthSnapshot(
            TelegramAuthStatus.PENDING,
            "Waiting for approval.",
            expires_at=datetime.now(UTC),
        )
        return self.snapshot

    async def qr_svg(self) -> bytes | None:
        if self.snapshot.status != TelegramAuthStatus.PENDING:
            return None
        return b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    async def close(self) -> None:
        return None


async def test_telegram_account_page_and_csrf_protection(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        await application.state.telegram_auth.close()
        fake_auth = _FakeTelegramAuth()
        application.state.telegram_auth = fake_auth
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            account = await client.get("/auth/telegram")
            rejected = await client.post(
                "/auth/telegram/start",
                data={"csrf_token": "incorrect"},
            )
            accepted = await client.post(
                "/auth/telegram/start",
                data={"csrf_token": application.state.csrf_token},
            )
            status = await client.get("/api/v1/auth/telegram")
            qr_image = await client.get("/auth/telegram/qr.svg")
            alias = await client.get("/login")

    assert account.status_code == 200
    assert "Connect your Telegram account" in account.text
    assert rejected.status_code == 403
    assert accepted.status_code == 303
    assert fake_auth.started is True
    assert status.json()["status"] == "pending"
    assert qr_image.headers["content-type"].startswith("image/svg+xml")
    assert alias.status_code == 307
    assert alias.headers["location"] == "/auth/telegram"


async def test_web_chat_selection_saves_specific_and_all_modes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _seed(settings)
    application = create_web_app(settings)
    chat = make_chat(title="Release Room")

    async with application.router.lifespan_context(application):

        async def fake_discover() -> ChatDiscovery:
            await application.state.archive.upsert_chat(chat)
            policy = await application.state.chat_selections.policy()
            effective = policy.effective_ids(
                legacy_ids=settings.configured_chat_ids,
                available_ids=(chat.telegram_chat_id,),
            )
            return ChatDiscovery((chat,), policy, effective)

        application.state.chat_selection_service.discover = fake_discover
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            page = await client.get("/chats", params={"refresh": "true"})
            specific = await client.post(
                "/chats/selection",
                data={
                    "csrf_token": application.state.csrf_token,
                    "mode": "specific",
                    "chat_id": str(chat.telegram_chat_id),
                },
            )
            specific_policy = await application.state.chat_selections.policy()
            all_chats = await client.post(
                "/chats/selection",
                data={"csrf_token": application.state.csrf_token, "mode": "all"},
            )
            all_policy = await application.state.chat_selections.policy()
            environment = await client.post(
                "/chats/selection",
                data={
                    "csrf_token": application.state.csrf_token,
                    "mode": "environment",
                },
            )
            environment_policy = await application.state.chat_selections.policy()

    assert page.status_code == 200
    assert "All accessible chats" in page.text
    assert "Release Room" in page.text
    assert specific.status_code == 303
    assert specific.headers["location"] == "/chats?saved=true"
    assert specific_policy == ChatSelection("specific", (chat.telegram_chat_id,))
    assert all_chats.status_code == 303
    assert all_policy == ChatSelection("all")
    assert environment.status_code == 303
    assert environment_policy == ChatSelection("environment")


async def test_web_chat_selection_rejects_csrf_and_inaccessible_ids(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _seed(settings)
    application = create_web_app(settings)
    chat = make_chat()

    async with application.router.lifespan_context(application):

        async def fake_discover() -> ChatDiscovery:
            return ChatDiscovery(
                (chat,),
                await application.state.chat_selections.policy(),
                (chat.telegram_chat_id,),
            )

        application.state.chat_selection_service.discover = fake_discover
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            csrf_rejected = await client.post(
                "/chats/selection",
                data={"csrf_token": "wrong", "mode": "all"},
            )
            inaccessible = await client.post(
                "/chats/selection",
                data={
                    "csrf_token": application.state.csrf_token,
                    "mode": "specific",
                    "chat_id": "-9999",
                },
            )
        policy = await ChatSelectionRepository(application.state.database).policy()

    assert csrf_rejected.status_code == 403
    assert inaccessible.status_code == 400
    assert policy == ChatSelection("environment")


async def test_unknown_page_has_custom_safe_404(tmp_path: Path) -> None:
    application = create_web_app(_settings(tmp_path))
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/missing/<script>alert(1)</script>")

    assert response.status_code == 404
    assert "This path is not in the index" in response.text
    assert "<script>alert(1)</script>" not in response.text


async def test_web_operations_run_with_progress_csrf_and_safe_stop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _seed(settings)
    application = create_web_app(settings)
    captured: dict[str, object] = {}

    async def fake_sync(context: OperationContext) -> None:
        captured.update(context.parameters)
        await context.progress(
            force=True,
            phase="syncing",
            detail="Processed the selected test chat",
            progress_current=1,
            progress_total=1,
            chats_completed=1,
            chats_total=1,
            messages_processed=12,
            downloads_completed=2,
        )

    async def fake_listener(context: OperationContext) -> None:
        await context.progress(
            force=True,
            phase="listening",
            detail="Monitoring the selected test chat",
            chats_total=1,
        )
        await context.stop_event.wait()

    async with application.router.lifespan_context(application):
        application.state.operations = OperationManager(
            settings,
            application.state.database,
            executors={"sync": fake_sync, "listen": fake_listener},
        )
        await application.state.operations.startup()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            page = await client.get("/operations")
            rejected = await client.post(
                "/operations/start",
                data={"csrf_token": "wrong", "command": "sync"},
            )
            invalid_range = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "sync",
                    "since": "2026-08-10",
                    "until": "2026-08-09",
                },
            )
            empty_types = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "sync",
                    "content_types_present": "1",
                },
            )
            unknown_type = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "sync",
                    "content_types_present": "1",
                    "content_type": "executable",
                },
            )
            started = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "sync",
                    "chat": "-1001234567890",
                    "limit": "50",
                    "since": "2026-08-01",
                    "until": "2026-08-09",
                    "content_types_present": "1",
                    "content_type": ["voice", "photo"],
                },
            )
            sync_id = int(started.headers["location"].split("job=")[1].split("&")[0])
            for _ in range(100):
                sync_status = await client.get(f"/api/v1/operations/{sync_id}")
                if sync_status.json()["operation"]["terminal"]:
                    break
                await asyncio.sleep(0.01)
            await application.state.operations._progress(
                sync_id,
                force=True,
                status="cancelled",
                phase="cancelled",
                detail="Stopped safely by the operator",
            )
            sync_page = await client.get(f"/operations?job={sync_id}")

            listener_started = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "listen",
                },
            )
            listener_id = int(listener_started.headers["location"].split("job=")[1].split("&")[0])
            for _ in range(100):
                listener_status = await client.get(f"/api/v1/operations/{listener_id}")
                if listener_status.json()["operation"]["phase"] == "listening":
                    break
                await asyncio.sleep(0.01)
            stopped = await client.post(
                f"/operations/{listener_id}/stop",
                data={"csrf_token": application.state.csrf_token},
            )
            for _ in range(100):
                final_listener = await client.get(f"/api/v1/operations/{listener_id}")
                if final_listener.json()["operation"]["terminal"]:
                    break
                await asyncio.sleep(0.01)
            resumed = await client.post(
                f"/operations/{sync_id}/resume",
                data={"csrf_token": application.state.csrf_token},
            )

    assert page.status_code == 200
    assert "Run the archive" in page.text
    assert "Start sync" in page.text
    assert "Text &amp; captions" in page.text
    assert "Photos &amp; images" in page.text
    assert "Voice messages" in page.text
    assert "Documents &amp; PDFs" in page.text
    assert rejected.status_code == 403
    assert invalid_range.status_code == 400
    assert empty_types.status_code == 400
    assert "Select at least one" in empty_types.text
    assert unknown_type.status_code == 400
    assert "Unknown content type" in unknown_type.text
    assert started.status_code == 303
    assert "Resume sync" in sync_page.text
    assert resumed.status_code == 303
    assert captured == {
        "chat": -1001234567890,
        "limit": 50,
        "since": "2026-08-01",
        "until": "2026-08-09",
        "content_types": ["photo", "voice"],
    }
    assert sync_status.json()["operation"]["status"] == "completed"
    assert sync_status.json()["operation"]["messages_processed"] == 12
    assert "Processed the selected test chat" in sync_page.text
    assert stopped.status_code == 303
    assert final_listener.json()["operation"]["status"] == "cancelled"


async def test_filtered_csv_export_neutralizes_spreadsheet_formulas(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="=Injected title"))
    await archive.upsert_message(
        make_message(
            sender_name="+Formula sender",
            text="=2+3",
            original_filename="@payload.pdf",
        )
    )
    await database.close()
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/exports/messages.csv", params={"media_only": "true"})

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert rows[0]["chat_title"] == "'=Injected title"
    assert rows[0]["sender_name"] == "'+Formula sender"
    assert rows[0]["text"] == "'=2+3"
    assert rows[0]["filename"] == "'@payload.pdf"
