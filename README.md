# Telegram Archiver

Telegram Archiver is a local, resumable message and media archive for chats your own Telegram user account can already access. It uses Telegram's official MTProto API through Telethon, records message metadata in SQLite, downloads allowed media through crash-safe temporary files, and can continue listening for new and edited messages.

It does not bypass access controls, discover inaccessible private chats, scrape members, send messages, or circumvent Telegram rate limits. Every selected chat must appear in the authenticated account's dialog list or startup fails clearly.

## Requirements

- Python 3.12 or newer
- A normal Telegram user account
- A Telegram `api_id` and `api_hash`
- Local disk space for SQLite, the Telethon session, and downloaded media
- Docker with Compose v2 (optional)

## Obtain Telegram API credentials

Telegram calls these values `api_id` and `api_hash`. They identify your local API application; they are not a bot token.

1. Make sure the account is active in an official Telegram mobile or desktop application.
2. Open [my.telegram.org](https://my.telegram.org) in a browser.
3. Enter the phone number for that account in international format and select **Next**.
4. Enter the confirmation code Telegram sends to the Telegram app. Treat this code as a secret. It may not arrive by SMS.
5. Select **API development tools**.
6. If no application exists, complete the application form. Use a descriptive title such as `Personal Telegram Archiver`, choose a short name, select the closest platform, and describe the legitimate personal-archive use case. The exact optional fields can change.
7. After submission, copy **App api_id** and **App api_hash**. `api_id` is numeric; `api_hash` is a longer string.
8. Put the values in `.env` without committing the file:

   ```dotenv
   TG_API_ID=123456
   TG_API_HASH=replace_with_your_private_api_hash
   ```

Telegram currently permits one `api_id` per phone number. If the page already shows an application, use its existing values instead of trying to create another. Telegram documents the process in [Creating your Telegram Application](https://core.telegram.org/api/obtaining_api_id).

The `API_HASH` is a secret. Telegram login codes, a 2FA password, and the resulting Telethon `.session` file are also secrets. Never paste them into logs, issue trackers, shell history, or source control.

## Local installation

```bash
python3 -m venv .venv
source .venv/bin/activate             # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env                  # Windows: copy .env.example .env
```

Edit `.env` and set at least:

```dotenv
TG_API_ID=123456
TG_API_HASH=replace_with_your_private_api_hash
```

All commands are available as either `python -m app COMMAND` or, after installation, `telegram-archiver COMMAND`. Run `python -m app --help` for the current option list.

## First login

```bash
python -m app login
```

Telethon may prompt for the account phone number, a one-time Telegram login code, and the 2FA password when enabled. Input is handled by Telethon and is never stored by this application. Successful authorization is stored in `data/telegram_session.session` by default, so later commands do not require another code.

Do not share or commit that session file: possession of it may grant access to the account.

## Discover and select chats

List only the dialogs available to the authenticated account:

```bash
python -m app chats
```

The table includes the Telegram ID, entity type, title, public username when present, and whether the chat is selected. You can manage archive coverage in any of three ways:

If Telegram no longer supplies a name for a deleted dialog, the CLI and dashboard show a type-specific label such as **Deleted account**, **Deleted channel**, or **Deleted group**. The numeric Telegram ID remains visible as secondary metadata and is never used as the chat name.

- Open the web dashboard's **Chats** page and choose specific dialogs, **All accessible chats**, or **Environment defaults**.
- Run `python -m app tui`, open the Chats tab, and use the selection controls or keyboard shortcuts.
- Keep the default environment mode and copy IDs exactly, including the `-100` prefix commonly used for channels and supergroups:

```dotenv
TARGET_CHATS=-1001234567890,-1009876543210
```

Alternatively, copy `config.example.yml`, edit it, and point `.env` to it:

```dotenv
CONFIG_FILE=config.yml
```

```yaml
chats:
  - id: -1001234567890
    enabled: true
  - id: -1009876543210
    enabled: false
```

Enabled YAML IDs and `TARGET_CHATS` are combined and deduplicated. Secrets are deliberately not read from YAML. They remain the selection source until a database-backed choice is saved in the web dashboard or TUI; choosing **Environment defaults** later removes that override.

**Specific chats** stores only the checked Telegram IDs. **All accessible chats** deliberately includes every dialog Telegram exposes to the account—including private chats, groups, and channels—and also includes newly accessible dialogs the next time a worker starts. It never probes usernames or inaccessible entities. Every target is re-resolved from the current account's dialog list at `sync`, `listen`, `retry-failed`, and `doctor` startup. An inaccessible or mistyped specific/environment ID causes a safe error. Restart a running sync or listener after changing selection.

For clean Telethon session-file ownership, stop a running listener before explicitly refreshing Telegram dialogs or saving a web selection, then restart it afterward. Browsing cached web/TUI archive data does not open Telegram.

## Historical synchronization

Archive all selected chats, oldest to newest:

```bash
python -m app sync
```

Useful bounded forms are:

```bash
python -m app sync --chat -1001234567890
python -m app sync --limit 100
python -m app sync --since 2026-01-01 --until 2026-01-31
python -m app sync --types text,photo,video,voice
```

`--limit` applies per chat. Date boundaries use Telegram message timestamps interpreted in UTC; `--until` includes the entire named day. A range sync deduplicates against SQLite but intentionally does not move the normal full-sync checkpoint, preventing a partial date range from creating a history gap.

`--types` accepts `text`, `photo`, `video`, `video_note`, `voice`, `audio`, `animation`, `sticker`, `document`, and `other`. Common aliases such as `image`, `gif`, `pdf`, and `round-video` are accepted. A captioned video matches both `text` and `video`: a text-only run stores its message/caption metadata but does not download the unselected video. `other` archives the available metadata for polls, contacts, locations, service events, and non-downloadable media.

Explicit category selections maintain independent per-chat checkpoints. A later photo-only run therefore does not lose photos merely because an earlier video-only run scanned beyond them. Selecting all categories, or omitting `--types`, uses the normal full-archive checkpoint. Existing message/file deduplication remains authoritative in both modes.

Each message is committed separately and a per-chat high-water mark is advanced only after valid metadata is stored. Restarting a full sync begins after that mark. The unique `(telegram_chat_id, telegram_message_id)` database constraint is the final deduplication guarantee. Albums remain separate message rows sharing the same `grouped_id`, so every album item is preserved.

Historical sync keeps up to `DOWNLOAD_CONCURRENCY` messages in flight so independent media transfers can overlap; the default is a conservative `2`. Results are settled oldest-first and the checkpoint advances in that same order, even if a newer download finishes first. This preserves crash-safe resume while avoiding the previous one-file-at-a-time bottleneck. Increase the value gradually only when the network and Telegram account remain stable—small values such as `2` to `4` are recommended, and every `FloodWait` is still honored.

At the beginning of a normal sync, failed/in-progress downloads and completed records whose files are missing are repaired before new history is fetched. A failed download can also be retried explicitly:

```bash
python -m app retry-failed
```

## Real-time monitoring

```bash
python -m app listen
python -m app listen --types photo,video,video_note
```

The listener handles `NewMessage` and `MessageEdited` events only for chats selected when the listener starts. Edits update text and edit timestamps; existing completed media is not downloaded twice. Telethon automatically reconnects transient sessions, and the outer listener adds bounded exponential reconnect delay. Restart the listener after changing chat selection.

At listener startup, handlers are installed first and then incomplete/failed media is repaired, so new events can still arrive during recovery. A later `sync` remains the authoritative catch-up pass after a prolonged outage.

On `SIGINT` or `SIGTERM`, new work stops, Telegram disconnects, and in-flight tasks receive up to `SHUTDOWN_TIMEOUT_SECONDS` to finish. Work still incomplete is cancelled and marked failed. A partially written file keeps its `.part` suffix and is never recorded as completed.

Run a catch-up `sync` after a prolonged listener outage if you want an explicit history consistency pass.

## Media policy

Media categories and limits are controlled in `.env`:

```dotenv
DOWNLOAD_PHOTOS=true
DOWNLOAD_VIDEOS=true
DOWNLOAD_DOCUMENTS=true
DOWNLOAD_AUDIO=true
MAX_FILE_SIZE_MB=500
DOWNLOAD_CONCURRENCY=2
DOWNLOAD_RETRIES=3
ALLOWED_EXTENSIONS=.jpg,.jpeg,.png,.mp4,.pdf,.zip
IGNORED_EXTENSIONS=.exe
KEYWORDS=release,invoice
```

Animations and video notes use the global video switch; voice messages use the global audio switch; stickers use the document switch. An empty allow-list permits all extensions except ignored ones. The ignore-list takes precedence. Keywords are comma-separated, case-insensitive substrings checked against message text/captions. Extension, size, keyword, and global media-policy decisions suppress only the file transfer for messages included by the active content selection. Unknown file size is allowed because Telethon may not expose a size before download; the final byte count is always recorded.

The environment switches are global policy boundaries. Per-operation `--types` and the web content picker narrow them further; they cannot enable a category disabled globally. `retry-failed --types voice,audio` similarly limits a repair run to those failed media categories.

Downloads are deliberately low-concurrency and retry with exponential delay. Telegram `FloodWait` durations are honored exactly rather than bypassed. Unsupported media is recorded and skipped without crashing the sync.

## Storage layout

The default database is `data/archive.db`. Automatic schema initialization creates:

- `chats`: Telegram identity, title, username, entity type, timestamps, and the full-sync checkpoint.
- `content_sync_checkpoints`: independent high-water marks for explicit per-chat content-type syncs.
- `messages`: Telegram identity, sender/text/timestamps/reply/album fields, media metadata, local path, and download state/error/attempts.
- `archive_selection_policy`: the optional singleton override (`specific` or `all`). No row means environment/YAML mode.
- `selected_chats`: explicit Telegram IDs used by `specific` mode.
- `operation_jobs`: durable web-command status, parameters, progress counters, timestamps, and terminal errors.
- `operation_logs`: a bounded per-job operator event log; older entries are trimmed automatically.

Downloaded files use portable, traversal-safe names:

```text
downloads/
└── -1001234567890_Example News/
    └── 2026/08/09/
        ├── 15293_clip.mp4
        └── 15294_photo.jpg
```

The message ID makes paths deterministic and prevents accidental overwrite. Downloads first target `<filename>.part`; only a successful completion is atomically renamed to the final filename and marked `completed`.

## Status and diagnostics

```bash
python -m app stats
python -m app doctor
```

`stats` reports selected chats, archived messages, downloaded files/bytes, skipped and failed media, and the newest archived message per known chat. `doctor` checks required environment values, SQLite access, download-directory writes, session authorization, and access to every selected chat. It never prints the API hash or session content.

## Web dashboard and Telegram account connection

Start the private dashboard as the operator process:

```bash
python -m app web
```

Open [http://127.0.0.1:8686](http://127.0.0.1:8686). The dashboard provides archive health, 14/30/90-day activity, an attention queue, chat selection, date and state filters, sorting, message details, album context, safe media previews, storage diagnostics, filtered CSV export, and an **Operations** console.

The Operations page exposes the application workflows that previously required a terminal:

- **Sync** supports all selected chats or one selected chat, an optional per-chat limit, inclusive `since`/`until` dates, and a Telegram content-type picker. It shows the active chat, chats completed, messages processed, downloads, repairs, elapsed time, and an operator event log.
- **Listen** runs the real-time listener inside the web process, applies its selected content categories, reports new messages/downloads as they arrive, reconnects normally, and remains active until safely stopped.
- **Retry failed** can narrow recovery to selected media categories and reports candidate and completed counts.
- **Doctor** reports configuration, SQLite, download storage, authorization, and selected-chat checks without exposing credentials.

Only one web operation runs at a time. Start and stop actions are allowlisted application calls protected by the same Basic Auth and CSRF boundary as chat selection; the browser cannot supply a shell command. Job state and bounded logs are stored in SQLite, so completed history survives page reloads. If the web process exits during a job, that job is marked `interrupted` at restart; the next sync resumes from message checkpoints and completed files. Do not run a separate CLI `sync`, `listen`, `retry-failed`, or `doctor` against the same Telethon session while a web operation is active.

The **Account** page adds a safe first-run alternative to `python -m app login`:

1. Open `/auth/telegram` and select **Create secure QR code**.
2. In the official Telegram mobile app, open **Settings → Devices → Link Desktop Device**.
3. Scan the short-lived QR code and approve the connection.
4. The resulting Telethon session is stored at `TG_SESSION_NAME`, exactly as with CLI login.

This flow connects an existing Telegram account. It does not create a Telegram account; use an official Telegram app to register a new account first. The web application never asks for a phone number, OTP, Telegram password, API hash, or uploaded session. If the account has Telegram 2FA enabled, complete the process with `python -m app login` so the password stays in Telethon's interactive terminal flow.

The default bind is loopback-only and does not require a web password. Any non-loopback `WEB_HOST`, including `0.0.0.0`, is rejected unless `WEB_PASSWORD` is set:

```dotenv
WEB_HOST=127.0.0.1
WEB_PORT=8686
WEB_USERNAME=archiver
WEB_PASSWORD=
WEB_REFRESH_SECONDS=15
```

Use a long, unique password if the dashboard is exposed through a private network. Basic Auth does not provide transport encryption, so terminate TLS in a trusted reverse proxy before allowing remote access. Do not expose it directly to the public internet. Media delivery is limited to completed database records whose resolved files remain inside `DOWNLOAD_DIR`.

Read-only integration endpoints are:

- `GET /healthz`: SQLite health check.
- `GET /api/v1/stats`: totals, chat summaries, states, media counts, and configurable activity.
- `GET /api/v1/messages`: paginated messages with search, chat, status, media, date, sort, and paging filters.
- `GET /api/v1/auth/telegram`: secret-free state for the active local account connection.
- `GET /api/v1/operations`: active and recent web-operation state.
- `GET /api/v1/operations/{id}`: one operation with its bounded event log.
- `GET /exports/messages.csv`: a streamed, filtered export with spreadsheet-formula protection.

The operator-only `POST /chats/selection` form saves the durable selection policy after refreshing Telegram's accessible dialogs. `POST /operations/start` and `POST /operations/{id}/stop` control allowlisted jobs. Every mutation rejects invalid CSRF tokens; chat selection also rejects IDs absent from the authenticated account's current dialog list.

When `WEB_PASSWORD` is configured, the same Basic Auth credentials protect HTML, JSON, static assets, health checks, and archived media.

## Terminal dashboard

For an SSH session or keyboard-first workflow, run:

```bash
python -m app tui
```

The Textual interface refreshes SQLite every `TUI_REFRESH_SECONDS` and can run beside the listener. It refreshes the authenticated account's accessible dialog list when it opens and when requested, but never starts archive downloads. Use `1` through `5` to switch between Overview, Messages, Chats, Downloads, and Attention; `/` to focus message search; `r` to refresh archive data; and `q` to quit.

On the Chats tab, focus a row and press `Space` to toggle it. Press `A` for **All accessible**, `C` to select none, `E` to restore `TARGET_CHATS`/YAML defaults, or `G` to refresh Telegram dialogs. Equivalent buttons are provided above the table. Changes are durable and take effect after restarting a running sync or listener. Attention prioritizes failed, interrupted, and pending media. The layout stacks automatically on narrow terminals. Message text is rendered as plain terminal content, so Telegram markup cannot be interpreted as Rich markup.

## Docker Compose

Create `.env` first, then build and authenticate interactively:

```bash
docker compose build
docker compose run --rm telegram-archiver login
docker compose run --rm telegram-archiver chats
docker compose run --rm telegram-archiver sync
docker compose up -d
docker compose logs -f telegram-archiver
```

The service defaults to `listen`. Compose overrides internal paths to `/app/data` and `/app/downloads` and persists them in named volumes `archive-data` and `archive-downloads`; secrets remain runtime environment values and are never baked into the image. `docker compose down` preserves volumes. `docker compose down -v` permanently deletes the database, session, and downloads and should be used only intentionally.

Run the TUI against the same volumes with:

```bash
docker compose run --rm telegram-archiver tui
```

The optional web service binds only to host loopback. Set a strong `WEB_PASSWORD` in `.env` because the container listens on `0.0.0.0` internally, then start its profile:

```bash
docker compose --profile web up -d telegram-web
```

Visit [http://127.0.0.1:8686](http://127.0.0.1:8686) and enter `WEB_USERNAME` plus `WEB_PASSWORD`. Starting only `telegram-web` lets the Operations page own sync/listener lifecycle. Do not also start `telegram-archiver` while a web worker is active; both services share the same sensitive session and persistent volumes.

To use a custom host port without changing the container port, add this to `.env`:

```dotenv
WEB_HOST_PORT=9090
```

Then open `http://127.0.0.1:9090`. The Compose mapping is `127.0.0.1:${WEB_HOST_PORT}:8686`. For a direct local process, set `WEB_PORT=9090` or run `python -m app web --port 9090`.

For bind-mounted files or embedding into another service, see [INTEGRATION.md](INTEGRATION.md).

## Recovery and troubleshooting

- **Not authenticated:** run `python -m app login` in the same environment and with the same `TG_SESSION_NAME` used by other commands.
- **Chat ID cannot be resolved:** run `python -m app chats`; confirm the account is still a member and copy the exact ID. The archiver will not request an inaccessible entity.
- **A chat shows as deleted:** Telegram no longer returns a usable name for that dialog. Its Telegram ID is retained so existing archive records remain identifiable, but inaccessible content is never requested.
- **Database is locked:** ensure only a small number of archiver processes use the same SQLite file. WAL and a 30-second busy timeout are enabled, but SQLite is not a multi-host database.
- **A `.part` file remains:** the previous attempt did not complete. The next sync/retry removes the stale partial before starting a fresh transfer; it never treats it as archived media.
- **Completed database row but missing file:** `sync` detects it during the repair pass and downloads it again when Telegram still exposes the message.
- **FloodWait message:** leave the application running. It sleeps for Telegram's required duration. Do not run many parallel instances against the same account.
- **Permission error in Docker:** named volumes work with the image's non-root user. For bind mounts, make the host directories writable by the selected container UID.
- **No messages in a range:** Telegram dates are compared in UTC, and the configured chat must contain messages in that inclusive day range.

Start with `python -m app doctor`, then set `LOG_LEVEL=DEBUG` for additional application diagnostics. Secret values are not logged even at debug level.

## Development

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Tests use temporary SQLite databases and fake Telegram messages; they do not need a Telegram account or network access.

Dashboard JavaScript, GSAP, CSS, and Geist fonts are bundled locally, so the browser makes no CDN requests. Node.js is needed only when editing frontend assets:

```bash
npm install
npm run build:web
```

For a non-editable dependency install, `requirements.txt` contains runtime packages and `requirements-dev.txt` adds the test/lint tools.

## Security checklist

- Keep `.env`, `API_HASH`, OTP/login codes, 2FA passwords, phone details, and `.session` files secret.
- Keep `data/` and `downloads/` out of Git and protect backups as private account data.
- Do not run session files obtained from anyone else.
- Restrict filesystem access to the OS user running the archiver.
- Review enabled chat IDs and media policy before starting a long-running listener.
- Revoke the Telegram session from Telegram's active-sessions settings if the host or session file is compromised.
- Treat an on-screen Telegram QR code as a short-lived login credential; generate it only on a trusted local or TLS-protected dashboard.
- Use the tool only for chats and content you are authorized to access and retain.
