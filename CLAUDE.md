# OmniAgent — Claude Instructions

## Type Rules

Every function parameter and return type must be one of:

- **Primitive** — `str`, `int`, `float`, `bool`, `bytes`, `None`, `list`, `dict`, `tuple`, `set`, `uuid.UUID`, `datetime`
- **Pydantic model** — subclass of `pydantic.BaseModel`
- **Framework** — inherent to the library (FastAPI `Request`, `Response`, `sqlalchemy.ext.asyncio.AsyncConnection`, `psycopg.AsyncConnection`, `asyncio.Queue`, `httpx.AsyncClient`, `mcp.server.Server`, etc.)
- **Protocol** — behavioural interface defined via `typing.Protocol` (e.g. `ToolExecutor`, `EventEmitter`)

Never use raw `Callable` or `Awaitable` in signatures. Define a Protocol instead.

Never use `@dataclass` for data that crosses module boundaries. Use Pydantic `BaseModel`.

Plain `dict` params are only acceptable at framework boundaries (e.g. FastAPI path/query params). Internal functions must use Pydantic models.

**DB table rows must be Pydantic models.** Never pass raw `dict[str, Any]` rows from `conn.execute()` across function boundaries. Define a `BaseModel` for each table (or reuse an existing one from `api/models.py`) and call `model_validate(dict(row._mapping))` at the query site. Partial queries that select non-standard columns can use a purpose-specific model in the same module. One-off expression queries (e.g. `jsonb_array_length`) are exempt.

**Avoid `typing.Any`.** Use it only where the type is genuinely unknowable: LLM-generated tool arguments (`dict[str, Any]`), decrypted/auth JSON blobs, external API responses before parsing, and generic wrappers (`_safe_lf`). Everywhere else, use the narrowest type possible — `object`, `str | int | None`, a Pydantic model, or a Protocol.

## Dependencies

Use `uv add --dev <package>` for dev tools. Never `pip install` directly — it bypasses `pyproject.toml` and `uv.lock`.

## After Changes

After making code changes, run:

```
pre-commit run --all-files
```

Fix any issues it reports before considering the change complete.

## Project Context

- FastAPI control plane (`api/`) + Procrastinate worker (`worker/`)
- PostgreSQL via SQLAlchemy Core (main pool) + psycopg async (sse_hub LISTEN/NOTIFY)
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

**All env vars go through `config.py`.** Never use `os.environ` or `os.getenv` directly. Add every env var as a field on the `Settings` class in `omniagent/config.py` with a sensible default. Import the singleton: `from omniagent.config import settings`. The only exception is `config.py` itself (pydantic-settings reads from env).

**SQLAlchemy Core, not full ORM.** Database queries use SQLAlchemy Core expressions (`select`, `insert`, `update`, `delete`) against `Table` definitions in `omniagent/tables.py`. We intentionally chose Core over full ORM because: (1) ~12% of queries need raw SQL for PostgreSQL-specific features (jsonb operators, `pg_notify`, advisory locks) that no ORM can express; (2) Core avoids duplicating the Pydantic models already in `api/models.py`; (3) the existing `conn.execute(expression, params)` pattern maps directly to Core without adding session/unit-of-work overhead; (4) Core is ~40% faster than ORM for equivalent queries. Raw SQL that Core cannot express stays in `api/queries.py` / `worker/queries.py` wrapped with `sqlalchemy.text()`.

**No raw magic strings.** Never scatter string literals for status values, notification types, event types, harness names, header names, security/auth types, or queue names across the codebase. Define them once in `omniagent/constants.py` (enums or module-level constants) and import from there. Tuneable magic numbers (timeouts, intervals, cache caps) belong in `config.py`, not as bare literals. The only exceptions: OpenAPI/JSON Schema specification keywords, comments, and SQL column names used as dict keys from query results.

**No cross-module imports between `api/` and `worker/`.** `api/` must not import from `omniagent.worker`, and `worker/` must not import from `omniagent.api`. Shared code lives at the `omniagent/` package level (`omniagent.db`, `omniagent.crypto`, `omniagent.migrations`, `omniagent.config`). The only allowed cross-reference is `api/` importing `worker.job.run_agent_job` inside a function body (lazy import) to defer jobs — the procrastinate task queue requires this.

**API and worker are both horizontally scalable — many processes, no shared memory.** Every change must hold under N api instances and N worker instances running concurrently, not just one of each.

- No in-process state (module-level dict/list/queue, singleton object) as the source of truth for anything that outlives one request or one job. Postgres is the only shared state. A `ponytail:`-tagged per-process cache is fine only as a TTL'd optimization on top of that, never the record of truth.
- Never assume the process that scheduled a job, opened a connection, or handled a prior request is the one handling the next one. No sticky sessions, no "the worker that started this turn finishes it."
- Locks and singleton behavior (SSE hub connection, migrations, stuck-session sweep) must use Postgres advisory locks or row locks — see above. An in-process `asyncio.Lock` or `threading.Lock` only serializes within one instance; it does nothing across replicas.
- Coordination between requests/jobs goes through the database (status columns, `FOR UPDATE`, `pg_notify`), never through direct process-to-process calls or in-memory pub/sub.

## Conventions

- **`ponytail:` comments** mark deliberate shortcuts with known ceilings. They're tracked debt, not ignorance — the comment names what was skipped and when to upgrade. E.g. `# ponytail: dict cache, per-process. TTL 60s. Hard cap 1000 entries.`
- **Structured logging** via `logging_config.py`. Every log line is JSON with `trace_id` (request ID or session ID). Grep one ID to trace across both processes.
- **`session_channel()`** from `omniagent.constants` converts session UUID to LISTEN channel. Use consistently for both `pg_notify` and `sse_hub.subscribe`.
- **`_safe_tool_name` / `_safe_name`** converts `namespace.tool-name` to `namespace__tool_name` for Python identifiers.
