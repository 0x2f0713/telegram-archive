# Architecture

Telegram Archiver is a modular monolith. It deploys as one service, while its source follows an inward dependency direction:

```text
interfaces (CLI, TUI, web) ──┐
                             ├──> application ──> domain
infrastructure (SQLite,      │
Telethon, downloads) ────────┘
```

The domain contains stable archive concepts and state rules. The application layer owns use cases, ports, and the immutable records exchanged across those ports. Infrastructure implements persistence, download, and Telegram adapters. Interfaces compose those implementations and translate HTTP, terminal, and command-line input.

## Dependency rules

- `app/domain` may depend only on the standard library and other domain modules.
- `app/application` may depend on the domain, configuration values, and shared utilities. It must not import infrastructure, interfaces, or external framework APIs such as FastAPI, SQLAlchemy, or Telethon.
- `app/infrastructure` implements application-facing ports and translates external objects into application or domain records.
- `app/interfaces` is the composition boundary. Concrete repositories, Telegram functions, and workflow executors are wired here.
- SQLAlchemy models never cross into application use cases or templates.

`tests/test_architecture.py` enforces the first two rules by inspecting imports.

## Important boundaries

- `application/dashboard.py` owns dashboard queries, DTOs, and snapshot orchestration. `persistence/read_models.py` is its SQLAlchemy adapter.
- `application/operation_records.py` and `application/operations.py` own the durable operation vocabulary and state machine. `persistence/operations.py` stores it; `interfaces/web/commands.py` composes concrete Telegram/archive workflows.
- `application/archive_records.py` owns persistence and downloader result records used by archive use cases.
- `interfaces/web/presentation.py` owns the shared Jinja environment. Feature routers can be split without recreating filters or page context.
- Frontend behavior is separated into `core.js`, `chat-selection.js`, `operations.js`, and `media-player.js`; `dashboard.js` is only the browser composition entry point.

## SQLite concurrency

The service uses one SQLite file. Its async engine exposes a bounded three-connection pool with no overflow. Every write enters one application-level gate before checking out a connection, so SQLite's single writer never consumes the whole pool; remaining connections preserve web read responsiveness under archive concurrency. Progress events are coalesced, WAL permits concurrent reads, and network requests and media downloads remain outside transactions.

## Adding a feature

1. Put state rules and stable value objects in the domain.
2. Define the use case and the narrow protocol it needs in the application layer.
3. Implement that protocol in infrastructure.
4. Wire the adapter at the CLI, TUI, or web composition boundary.
5. Return application-owned records rather than ORM or Telethon objects.
6. Add focused tests plus a full architecture/test/build pass.
