# Contributing

## Local setup

**Requirements:** Python 3.14, [uv](https://docs.astral.sh/uv/), PostgreSQL 16, Docker (for compose)

```bash
# 1. Clone and install deps
git clone <repo>
cd omniagent
uv sync

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set OMNIAGENT_ENCRYPTION_KEY:
#   OMNIAGENT_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 3. Start Postgres (or use Docker: docker compose up postgres -d)
# 4. Run control plane (runs migrations on startup)
uv run uvicorn omniagent.api.main:app --reload --port 8080

# 5. Run worker (separate terminal)
uv run python -m omniagent.worker
```

UI served at `http://localhost:8080`.

## Docker Compose (single command)

```bash
# Copy and fill in secrets
cp .env.example .env

# Start everything: Postgres → API → worker → nginx
docker compose up --build
```

Startup order: `postgres` healthy → `api` healthy (migrations done, `/health` 200) → `worker` starts.

### With test service

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build
```

Mounts `test_service.py` into a worker-derivative container on a shared network.
Worker tools reach it at `http://test_service:8001` — no `host.docker.internal`
or iptables tricks needed. Test service OpenAPI spec at `http://localhost:8001/openapi.json`.

### With Langfuse (self-hosted)

```bash
docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up -d
```

Then set `LANGFUSE_BASE_URL=http://langfuse:3000` in `.env`.

Health endpoint: `GET /health` — returns `{"status": "ok", "db": "ok"}` or 503 on DB failure.

## Running tests

```bash
uv run pytest
```

## PR process

1. Branch from `main`
2. Keep PRs focused — one feature or fix per PR
3. Run `uv run ruff check .` and `uv run black --check .` before pushing
4. PRs require passing CI (lint + tests)

## Env vars reference

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL DSN |
| `OMNIAGENT_ENCRYPTION_KEY` | yes | Fernet key for auth_context encryption. See `.env.example` |
| `UI_PASSWORD` | yes | Admin password for the web UI |
| Claude provider | no | See `.env.claude.example` — copy to `.env.claude` |
| Antigravity provider | no | See `.env.antigravity.example` — copy to `.env.antigravity` |
| `MAX_HISTORY_TURNS` | no | Default `50` |
| `TOOL_EXECUTION_TIMEOUT` | no | Seconds. Default `30` |
| `MONTY_EXECUTION_TIMEOUT` | no | Seconds. Default `30` |
| `MONTY_EXECUTOR_WORKERS` | no | Default `4` |
| `LANGFUSE_SECRET_KEY` | no | Langfuse tracing (no-op if unset) |
| `LANGFUSE_PUBLIC_KEY` | no | Langfuse public key |
| `LANGFUSE_BASE_URL` | no | Langfuse instance URL (e.g. `https://cloud.langfuse.com`). Use `LANGFUSE_BASE_URL`, not `LANGFUSE_HOST` |
