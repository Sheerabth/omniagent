# OmniAgent — Claude Instructions

## Type Rules

Every function parameter and return type must be one of:

- **Primitive** — `str`, `int`, `float`, `bool`, `bytes`, `None`, `list`, `dict`, `tuple`, `set`, `uuid.UUID`, `datetime`
- **Pydantic model** — subclass of `pydantic.BaseModel`
- **Framework** — inherent to the library (FastAPI `Request`, `Response`, `psycopg.AsyncConnection`, `asyncio.Queue`, `httpx.AsyncClient`, `mcp.server.Server`, etc.)
- **Protocol** — behavioural interface defined via `typing.Protocol` (e.g. `ToolExecutor`, `EventEmitter`)

Never use raw `Callable` or `Awaitable` in signatures. Define a Protocol instead.

Never use `@dataclass` for data that crosses module boundaries. Use Pydantic `BaseModel`.

Plain `dict` params are only acceptable at framework boundaries (e.g. FastAPI path/query params). Internal functions must use Pydantic models.

## After Changes

After making code changes, run:

```
pre-commit run --all-files
```

Fix any issues it reports before considering the change complete.

## Project Context

- FastAPI control plane (`api/`) + Procrastinate worker (`worker/`)
- PostgreSQL via psycopg async, LISTEN/NOTIFY for SSE
- Agent harnesses: Claude (claude-agent-sdk) and Antigravity (Gemini)
- Monty sandbox for code execution (pydantic-monty)
- OpenAPI 3.x import → auto-generated tools
- `test_service.py` is a standalone mock API for manual testing — no pytest suite

## Architecture Invariants

These rules are load-bearing. Breaking them introduces subtle race conditions and data corruption.

**Session status is worker-owned.** Only the worker (`job.py`) writes `sessions.status`. The API sets it to `pending` on new runs and reads it, never writes terminal states. SSE streams trust status immediately because the worker is the single writer — no "unconfirmed" states.

**Row locks before state transitions.** Any state change on a session row must `SELECT ... FOR UPDATE` first. Concurrent `/run` calls, cancel vs complete, and defer catch-up all race on the same row. FOR UPDATE serializes them.

**Advisory locks for singleton operations.** Migrations, stuck-session reconciliation, and SSE hub (single shared LISTEN connection per process) all use `pg_advisory_lock` / `pg_try_advisory_lock`.

**SSE hub is one connection per process.** All `/stream` endpoints share a single Postgres connection via `sse_hub.py`. Never open a raw LISTEN connection per stream — it burns Postgres's `max_connections`. Subscribe/unsubscribe are async-queued operations that execute between notifies() polling windows.

**Auth context is encrypted at rest.** `namespace_auth.auth_context` stores Fernet-encrypted JSON at rest. Always use `encrypt_auth_context` / `decrypt_auth_context`. If the encryption key is unset, falls back to plaintext with a warning — this is backward compat, not a bug.

**Encryption key is a Fernet key.** Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Set as `OMNIAGENT_ENCRYPTION_KEY`.

## Conventions

- **`ponytail:` comments** mark deliberate shortcuts with known ceilings. They're tracked debt, not ignorance — the comment names what was skipped and when to upgrade. E.g. `# ponytail: dict cache, per-process. TTL 60s. Hard cap 1000 entries.`
- **Structured logging** via `logging_config.py`. Every log line is JSON with `trace_id` (request ID or session ID). Grep one ID to trace across both processes.
- **`_CH` lambda** converts session UUID to LISTEN channel: `"session_" + sid.replace("-", "_")`. Use consistently for both `pg_notify` and `sse_hub.subscribe`.
- **`_safe_tool_name` / `_safe_name`** converts `namespace.tool-name` to `namespace__tool_name` for Python identifiers.
