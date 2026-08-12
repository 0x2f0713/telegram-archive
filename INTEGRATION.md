# Integration Guide

This how-to guide is for developers and operators integrating Telegram Archiver into a local service stack or a controlled single-host deployment. It covers process topology, the dashboard API and account connection, terminal UI, persistence, and the shutdown contract. It does not describe multi-account hosting, public internet exposure, or replacing Telegram's access controls. The CLI remains the archive-worker lifecycle boundary; the Python classes are intentionally small and injectable for advanced integrations.

## Runtime contract

The application requires Python 3.12+, writable persistent storage, outbound access to Telegram, and this environment contract. `TG_API_HASH`, `WEB_SESSION_SECRET`, and the resulting Telegram session data are secrets in this example:

```dotenv
TG_API_ID=123456
TG_API_HASH=private_value
TG_SESSION_NAME=/persistent/data/telegram_session
DATABASE_URL=sqlite:////persistent/data/archive.db
DOWNLOAD_DIR=/persistent/downloads
TARGET_CHATS=-1001234567890
WEB_HOST=127.0.0.1
WEB_PORT=8686
WEB_HOST_PORT=8686
WEB_SESSION_SECRET=replace_with_a_long_random_value
WEB_REFRESH_SECONDS=15
TUI_REFRESH_SECONDS=5
```

Configuration precedence is: constructor arguments used by an embedding process, environment variables, values from `.env`, then code defaults. `TARGET_CHATS` and enabled entries in `CONFIG_FILE` are merged. YAML is only a chat-selection source and cannot carry credentials.

Chat selection has a separate durable policy layer. With no `archive_selection_policy` row, workers use the merged environment/YAML IDs. An operator can save `specific` or `all` mode from the web Chats page or TUI; that SQLite policy then overrides environment targets for every process using the database. Selecting **Environment defaults** deletes the override. `all` means all dialogs returned by Telegram to the authenticated account at each worker startup, not all cached rows and never inaccessible entities.

`WEB_HOST_PORT` is consumed by Docker Compose interpolation, not by `Settings`; direct Python runs use `WEB_PORT` or the `web --port` CLI option.

All relative paths are resolved from the process working directory. For supervisors and containers, use absolute paths to remove ambiguity. Persist the parent of `TG_SESSION_NAME`, the SQLite database and its WAL sidecars, and `DOWNLOAD_DIR` together with consistent ownership.

### Host-wide storage layout

This deployment keeps persistent state under `/mnt/disk2/telegram-archiver`:

```dotenv
DOWNLOAD_DIR=/mnt/disk2/telegram-archiver/downloads
DATABASE_URL=sqlite:////mnt/disk2/telegram-archiver/data/archive.db
TG_SESSION_NAME=/mnt/disk2/telegram-archiver/data/telegram_session
```

Stop every archiver process before copying an existing SQLite database and Telethon session. Copy the complete `data/` and `downloads/` contents, retain the original as a rollback copy, restrict the storage directories to the service account, and run `python -m app doctor` before restarting workers. The four slashes in an absolute SQLite URL are intentional; Telethon adds the `.session` suffix itself.

## Lifecycle integration

A standard deployment can run these phases from the CLI or from `/operations`:

1. One-time authorization: `python -m app login` with a TTY, or the local dashboard's official Telegram QR flow.
2. Operator discovery: `python -m app chats`.
3. Operator selection: use `/chats`, the TUI Chats tab, or `TARGET_CHATS`/YAML.
4. Configuration validation: `python -m app doctor`.
5. Initial/catch-up batch: `python -m app sync`.
6. Long-running worker: `python -m app listen`.
7. Periodic safety pass, optional: schedule `python -m app sync` when the listener is stopped or coordinate so only one repair/sync writer runs at a time.

Two operator surfaces are available:

- `python -m app web` starts the responsive HTML dashboard, JSON API, local Telegram account connection page, and the allowlisted operation controller.
- `python -m app tui` starts the keyboard-first terminal dashboard.

The TUI can edit selection but does not initiate downloads. The web Chats page edits selection, while the web Operations page can run `sync`, `listen`, `retry-failed`, and `doctor` with durable progress, bounded logs, and a safe-stop signal. The web account page can create a short-lived MTProto QR authorization request and persist the same local Telethon session used by the CLI. These surfaces never accept phone numbers, OTP codes, 2FA passwords, API hashes, session uploads, or arbitrary shell commands.

Run only one Telegram worker workflow per session. A web operation is intentionally exclusive with other web operations. Operators must also avoid starting a CLI/container worker against the same `.session` file while the Operations page has an active job. On graceful web shutdown, active work receives a stop event and the process waits up to `SHUTDOWN_TIMEOUT_SECONDS`; jobs orphaned by a crash are marked `interrupted` during the next startup.

`listen` handles SIGINT and SIGTERM. Supervisors should send SIGTERM and allow at least `SHUTDOWN_TIMEOUT_SECONDS` plus a few seconds for client/database cleanup before SIGKILL. Docker Compose uses a 45-second grace period for the default 30-second application timeout.

Exit status is nonzero for invalid configuration, unauthorized sessions, inaccessible selected chats, failed doctor checks, and unhandled synchronization failures. Ordinary per-file failures are stored for `retry-failed` and do not abort the entire history stream.

## Docker and Compose

The included image has this interface:

```text
ENTRYPOINT: python -m app
default command: listen
persistent paths: /app/data, /app/downloads
runtime user: UID/GID 10001 by default (configurable with `ARCHIVER_UID`/`ARCHIVER_GID`)
```

The included Compose file uses configurable bind mounts. `ARCHIVER_DATA_DIR` stores the SQLite database, WAL sidecars, web state, and Telethon session; `ARCHIVER_DOWNLOADS_DIR` stores downloaded media:

```yaml
services:
  telegram-archiver:
    volumes:
      - ${ARCHIVER_DATA_DIR:-./data}:/app/data
      - ${ARCHIVER_DOWNLOADS_DIR:-./downloads}:/app/downloads
```

On Linux, set `ARCHIVER_UID` and `ARCHIVER_GID` to the host owner of these directories. Never mount a session file read-only: Telethon may update it. Do not put secret values in the Dockerfile or image build arguments.

When using `CONFIG_FILE` in Docker, mount that YAML file read-only and set its container path, for example `./config.yml:/app/config.yml:ro` with `CONFIG_FILE=/app/config.yml`. The default Compose stack relies on `TARGET_CHATS` and does not mount an optional YAML file automatically.

Use the same bind-mounted data directory during interactive login and the listener. A login container using a different mount creates a session the worker cannot see.

### Add the web dashboard to Compose

The included `telegram-web` service is behind the `web` profile. It shares the configured data and download bind mounts with the worker and publishes the configured host port on loopback only. Set a long random `WEB_SESSION_SECRET` in `.env` because its container bind is `0.0.0.0`:

```dotenv
WEB_SESSION_SECRET=replace_with_a_long_random_value
WEB_HOST_PORT=8686
```

```bash
docker compose --profile web up -d telegram-web
docker compose --profile web logs -f telegram-web
```

Open `http://127.0.0.1:8686`. Starting the named `telegram-web` service avoids also launching the default listener service, leaving worker lifecycle under the Operations page. To publish a different local port, set `WEB_HOST_PORT=9090` and open `http://127.0.0.1:9090`; the application still listens on port 8686 inside the container. Compose does not publish the dashboard on external host interfaces. If a remote operator must access it, prefer an SSH tunnel:

```bash
ssh -L 8686:127.0.0.1:8686 archive-host
```

Then browse to `http://127.0.0.1:8686` on the operator machine, approve or confirm the Telegram account, and continue into the archive. If you use a reverse proxy instead, keep the application port private, terminate HTTPS at the proxy, preserve cookies, and add a stronger identity-aware access layer when practical. The signed Telegram browser session is an access check, not transport encryption.

### Run the TUI in Compose

The terminal dashboard uses the main service's existing volumes:

```bash
docker compose run --rm telegram-archiver tui
```

Compose allocates an interactive terminal by default. Use `q` for a clean exit. In the Chats tab, `Space` toggles the focused chat, `A` selects all accessible dialogs, `C` clears selection, `E` restores environment/YAML defaults, and `G` refreshes Telegram. The TUI is best suited to local terminals and SSH; it is not a daemon or a replacement for `listen`.

## Web integration reference

Start the dashboard locally with `python -m app web`. `--host` and `--port` override `WEB_HOST` and `WEB_PORT` for that invocation. An unauthenticated non-loopback bind fails at startup. When `WEB_SESSION_SECRET` is set, the signed Telegram browser session protects every route, including health checks, JSON, and media.

The HTML routes are:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Archive overview and recent activity |
| `GET` | `/messages` | Searchable, paginated archive explorer |
| `GET` | `/messages/{database_id}` | Message metadata and safe media preview |
| `GET` | `/chats` | Render cached selection controls; `refresh=true` refreshes Telegram dialogs |
| `POST` | `/chats/selection` | CSRF-protected selection mutation after current access validation |
| `GET` | `/operations` | Command launcher, live progress, safe stop, logs, and durable operation history |
| `POST` | `/operations/start` | CSRF-protected allowlisted start for sync, listen, retry-failed, or doctor; worker forms may repeat `content_type` |
| `POST` | `/operations/{job_id}/stop` | CSRF-protected cooperative stop request |
| `GET` | `/system` | Redacted runtime and security posture |
| `GET` | `/auth/telegram` | Existing-account status and official QR connection workflow |
| `POST` | `/auth/telegram/start` | CSRF-protected creation of one short-lived QR request |
| `POST` | `/auth/telegram/continue` | CSRF-protected creation of a Telegram-bound browser session |
| `POST` | `/auth/telegram/logout` | Clears the current browser session |
| `GET` | `/auth/telegram/qr.svg` | Current QR image while authorization is pending |
| `GET` | `/media/{database_id}` | Completed media constrained to `DOWNLOAD_DIR` |
| `GET` | `/exports/messages.csv` | Streamed CSV using the active message filters |

The programmatic routes are:

| Method | Path | Contract |
| --- | --- | --- |
| `GET` | `/healthz` | Returns `{"status":"ok"}` after a successful SQLite query |
| `GET` | `/api/v1/stats` | Returns aggregate stats, chats, attention records, state counts, media counts, and activity; accepts `days` from 1 to 90 |
| `GET` | `/api/v1/messages` | Returns a message page and accepts search, chat, state, media, date, sort, and paging filters |
| `GET` | `/api/v1/auth/telegram` | Returns secret-free QR lifecycle state for local polling |
| `GET` | `/api/v1/operations` | Returns the active job and recent durable operation history |
| `GET` | `/api/v1/operations/{job_id}` | Returns live job counters, state, parameters, and bounded logs |
| `GET` | `/openapi.json` | Machine-readable FastAPI schema |

`page_size` is capped at 100. Search is a case-insensitive match across message text, sender name, filename, and chat title. `sort` accepts `newest`, `oldest`, `largest`, or `most_retried`. `since` and `until` are inclusive calendar-day filters. The CSV exporter applies the same filters and prefixes cells that spreadsheet software could interpret as formulas. The JSON API is read-only and intended for local integrations, not as a stable public multi-tenant API.

### Chat-selection integration

`POST /chats/selection` accepts a bounded URL-encoded form containing the in-page `csrf_token`, a `mode` of `specific`, `all`, or `environment`, and repeated `chat_id` values for specific mode. It is an operator UI contract, not a public API. The handler opens the local authorized Telethon session and refreshes `iter_dialogs()` before committing. Specific IDs and environment defaults must all be present in that current list. All mode stores no IDs; every worker re-enumerates accessible dialogs when it starts.

`POST /operations/start` accepts repeated `content_type` fields when `content_types_present=1` for `sync`, `listen`, and `retry-failed`. Canonical values are `text`, `photo`, `video`, `video_note`, `voice`, `audio`, `animation`, `sticker`, `document`, and `other`. An empty or unknown selection is rejected. Omitting the sentinel preserves the all-types contract for older local integrations. Operation history stores only canonical category names, never arbitrary executable input.

Selection changes do not hot-reconfigure an already running listener. Restart `sync`, `listen`, or `retry-failed` after a change. This gives each worker one stable target set while it is processing. Web/TUI and environment modes share the same database, so all processes must use the same `DATABASE_URL`.

Avoid simultaneous Telethon writers against one `.session` file: stop the listener before a manual web save or Telegram dialog refresh, then restart it with the new policy. Ordinary cached dashboard and TUI reads can continue beside the listener.

Example local health and filtered query:

```bash
curl --fail http://127.0.0.1:8686/healthz
curl --get http://127.0.0.1:8686/api/v1/messages \
  --data-urlencode 'q=release' \
  --data-urlencode 'media_only=true' \
  --data-urlencode 'since=2026-01-01' \
  --data-urlencode 'sort=largest' \
  --data-urlencode 'page_size=25'
```

With Telegram browser authentication enabled:

```bash
curl --cookie "telegram_archiver_session=$TELEGRAM_ARCHIVER_SESSION" --fail \
  http://127.0.0.1:8686/api/v1/stats
```

Obtain the browser cookie through the web login flow. Avoid copying it into shell history or logs because it grants access until it expires.

## Telegram QR authorization integration

The `/auth/telegram` flow implements Telegram's official QR login protocol through Telethon. A fresh request is held only in process memory, rendered as SVG, and removed after success, expiry, failure, or shutdown. The QR token is never returned by the JSON status endpoint or written to application logs. The POST that creates a request accepts only a small URL-encoded body and validates an in-memory CSRF token.

Authorization sequence:

1. Start the web service with `TG_API_ID`, `TG_API_HASH`, and a writable persistent `TG_SESSION_NAME`.
2. Open `/auth/telegram` through loopback, an SSH tunnel, or a TLS-protected reverse proxy.
3. Submit the start form and scan the code in Telegram under **Settings → Devices → Link Desktop Device**.
4. Poll `/api/v1/auth/telegram` only for `pending`, `connected`, `expired`, `two_factor`, `failed`, or `unavailable` state.
5. When the account is connected, choose **Continue to archive**. The server signs an HttpOnly cookie bound to the Telegram user ID in the local session.
6. If `two_factor` is returned, stop the web attempt and run `python -m app login` in a trusted terminal. The web process does not accept the Telegram password.
7. Run `python -m app chats` and configure only IDs shown for that authenticated account.

Do not proxy or cache `/auth/telegram/qr.svg`, capture it in observability tools, or embed it in another origin. Security middleware sends `Cache-Control: no-store`, a same-origin Content Security Policy, frame denial, and no-referrer headers. Do not start simultaneous CLI and web authorization attempts against the same session file. Existing authorized sessions are detected and reused; no new QR is created. A browser must still choose **Continue to archive** before protected pages are available.

This connection is single-account and local. "Register" means initializing the archive with an existing Telegram account. New Telegram account registration stays in Telegram's official applications.

## Python service composition

For an in-process integration, construct one settings/database/repository/service graph and keep the Telegram client lifecycle explicit:

```python
from app.config import Settings
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.database import Database
from app.application.archive import ArchiveService
from app.application.chat_selection import ChatSelectionService
from app.infrastructure.download import MediaDownloader
from app.infrastructure.telegram.client import connect_authorized, create_client
from app.application.sync import sync_history

settings = Settings()
database = Database(settings.database_url)
repository = ArchiveRepository(database)
downloader = MediaDownloader(settings, repository)
content_types = frozenset({"photo", "video", "voice"})
archive = ArchiveService(settings, repository, downloader, content_types)
client = create_client(settings)

await database.initialize()
try:
    await connect_authorized(client)
    chats = await ChatSelectionService(settings, repository).resolve_with_client(client)
    await sync_history(
        client,
        chats,
        archive,
        repository,
        content_types=content_types,
    )
finally:
    await client.disconnect()
    await database.close()
```

Do not share a SQLAlchemy `AsyncSession`; repository operations intentionally create short-lived transactions. One `ArchiveService` may process concurrent listener events safely: a per-message lock prevents duplicate work and `MediaDownloader` applies the configured global semaphore. Historical sync also uses `DOWNLOAD_CONCURRENCY` as its bounded in-flight window. It settles tasks and advances the per-chat checkpoint in source order, so a newer completed transfer can never make restart recovery skip older in-flight work.

`ChatInfo` and `MessageData` in `app.domain` are the pure business value objects. `app.infrastructure.telegram.translation` adapts raw Telethon objects into them at the adapter boundary. Integrations should prefer these data types and repository methods over direct ORM mutation, because explicit repository transitions preserve retry and deduplication invariants.

The package layout follows the business model: `app.domain` holds the artifact hierarchy (Chat -> Message -> MediaArtifact) with no framework imports, `app.application` holds the workflows (archive, sync, listener, operations, media policy), `app.infrastructure` holds the Telethon, persistence, and download adapters, and `app.interfaces` holds the CLI, TUI, and web surfaces.

## Database and file consistency contract

The database identity is `(telegram_chat_id, telegram_message_id)`. Do not generate a new identity from filenames, document IDs, album IDs, or timestamps. Album items intentionally have different message IDs and a shared `grouped_id`.

Download state transitions are:

```text
pending -> downloading -> completed
                      \-> failed
pending/filter decision -> skipped
no media -> not_applicable
```

An explicit content-type sync uses `content_sync_checkpoints` instead of the legacy all-content mark on `chats`. Each selected category has its own per-chat high-water mark. The worker starts from the oldest selected mark, ignores already-covered category/message combinations, and advances every selected mark only as source-ordered tasks settle. This preserves both later category expansion and crash-safe resume.

Before a transfer begins, the final path is committed with `downloading`. Bytes are written to `<final>.part` and atomically renamed. Only then is the row marked `completed`. A crash can therefore leave either a `.part`, or a final file with a non-completed row; the next repair pass handles both. Never publish or index `.part` files as completed assets.

External consumers should treat only `download_status = 'completed'` plus an existing `media_path` as a usable file. SQLite WAL mode means backup tooling should use SQLite's backup mechanism or stop all archiver processes before copying the database and sidecars.

Schema creation is automatic and idempotent for this release. For application upgrades that change the schema, take a protected backup of the session, database, and downloads and review release migration notes before deploying.

The `archive_selection_policy` singleton stores only `specific` or `all`; absence means environment mode. `selected_chats` stores IDs only for specific mode. The web and TUI replace these rows transactionally. The `chats` table is a metadata cache, not an authorization source: workers always resolve effective IDs against the current Telegram dialog list before reading messages.

## Scheduling and horizontal scale

The design targets one account and one SQLite writer host. Do not horizontally scale listeners against one session/database. It adds no useful download throughput, creates update races, and increases FloodWait risk. Low internal concurrency is configurable and bounded.

If integrating with systemd, Kubernetes, Nomad, or another supervisor:

- model `login` as a manual administrative job, never an unattended init container;
- model `doctor` as a deployment validation job rather than a liveness probe (it calls Telegram);
- use process/container health for the long-running listener;
- retain `/data` and `/downloads` on the same host or storage boundary;
- send SIGTERM for planned shutdown;
- run `sync` as an operator job after outages rather than alongside `listen` by default.

## Observability

Logs are human-readable structured messages on stdout/stderr and are suitable for supervisor collection. Configure `LOG_LEVEL`; do not add environment dumps or Telethon session serialization to diagnostic pipelines. `stats` is the stable operator-facing summary command. The included dashboard and TUI use short-lived SQLAlchemy sessions against the same WAL database. Other SQLite consumers should use read-only connections and the status semantics above.

Suggested alerts are repeated `download_status='failed'`, unexpected listener process exit, rapid disk growth, low free space, or authorization failure. A `FloodWait` log is not itself an error; the worker intentionally sleeps for Telegram's required interval.

## Security boundary

An integration must not accept arbitrary session uploads, OTPs, 2FA passwords, or chat IDs from untrusted users. Authorization must remain an operator-controlled interactive action through either Telethon's terminal flow or the local official QR flow. Treat a displayed QR as a short-lived credential. Protect persistent volumes and backups as account credentials and private content. The resolver only accepts entities returned in the authenticated account's dialog list; do not replace it with username probing or access-control workarounds.
