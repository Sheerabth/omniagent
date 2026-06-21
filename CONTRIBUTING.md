# Contributing

## Local setup

**Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16, Docker (for compose)

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

# Start everything: postgres → server (runs migrations) → worker
docker compose up --build
```

Startup order: `postgres` healthy → `api` healthy (migrations done, `/health` returns 200) → `worker` starts.

**Linux only:** tools that call services on the host (e.g. `http://host.docker.internal:8001`) require a one-time firewall rule so Docker bridge networks can reach the host:

```bash
sudo iptables -I INPUT -i br+ -p tcp --dport <port> -j ACCEPT
```

This rule resets on reboot. To persist it, use `iptables-save` / your distro's firewall manager.

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
| `OMNIAGENT_API_KEY` | no | Fixed UI API key. Auto-generated + printed on startup if unset |
| `OMNIAGENT_CLAUDE_API_KEY` | no | Anthropic API key (Claude harness) |
| `OMNIAGENT_ANTIGRAVITY_API_KEY` | no | Antigravity API key |
| `MAX_HISTORY_TURNS` | no | Default `50` |
| `TOOL_EXECUTION_TIMEOUT` | no | Seconds. Default `30` |
| `MONTY_EXECUTION_TIMEOUT` | no | Seconds. Default `30` |
| `MONTY_EXECUTOR_WORKERS` | no | Default `4` |
