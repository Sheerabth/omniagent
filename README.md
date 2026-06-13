# OmniAgent

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Self-hosted platform for running AI agents across multiple LLM providers. Define tools once in Pydantic — they work with any supported agent harness (Claude, Gemini/Antigravity).

Your microservices annotate functions with `@tool()`. OmniAgent discovers them, routes calls, and manages agent sessions. You bring your own chat UI and hit the REST API.

---

## Architecture

```
Your services          OmniAgent
──────────────         ──────────────────────────────
Payment Service        Control Plane (FastAPI)
└── @tool() fns  ────► WS server → namespace pool
                       REST API
                       Job queue (Procrastinate/Postgres)
                       Secrets store (AES-256-GCM)

                       Workers (stateless pool)
                       └── Claude / Antigravity harness
                           └── calls tools via control plane
```

**Hierarchy:** `Tool` (code) → `Skill` (config) → `Agent` (config) → `Session` (runtime)

---

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

---

## 1. Install

```bash
git clone <repo>
cd omniagent
uv sync
```

---

## 2. Postgres setup

Create a database and apply the schema:

```bash
createdb omniagent
psql omniagent < migrations/001_init.sql
```

---

## 3. Environment variables

Copy `.env.example` and fill in values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://user:pass@localhost:5432/omniagent` |
| `OMNIAGENT_SECRET_KEY` | ✅ | 32-byte hex string for AES-256-GCM. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `OMNIAGENT_WORKER_SECRET` | ✅ | Shared secret between workers and control plane. Generate same way. |
| `MAX_HISTORY_TURNS` | — | Conversation history limit per session (default: `50`) |
| `TOOL_EXECUTION_TIMEOUT` | — | Seconds before tool call times out (default: `30`) |

---

## 4. Start the control plane

```bash
uv run uvicorn omniagent.control_plane.main:app --host 0.0.0.0 --port 8080
```

On first start it runs a reconciliation pass (marks any orphaned `running` sessions as `failed`) then begins accepting traffic.

API docs available at `http://localhost:8080/docs`.

---

## 5. Start workers

Workers are stateless — run as many as you need. Each polls the job queue independently.

```bash
# terminal 2
uv run python -m omniagent.worker

# scale horizontally — just run more
uv run python -m omniagent.worker
```

Workers need `DATABASE_URL`, `OMNIAGENT_WORKER_SECRET`, and `OMNIAGENT_CONTROL_PLANE` (defaults to `http://localhost:8080`).

---

## 6. Create a service key

Before connecting a service you need a service key:

```bash
curl -X POST http://localhost:8080/settings/service-keys \
  -H "X-OmniAgent-Key: <any-existing-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "payments-service"}'
```

Returns `{ "key": "..." }` — shown once, store it securely.

> **Bootstrap problem:** on a fresh install there are no keys yet. Set `OMNIAGENT_WORKER_SECRET` on the control plane and use it as the initial `X-OmniAgent-Key` to create the first service/client key.

---

## 7. Instrument your service

Install the SDK into your service:

```bash
uv add omniagent  # or: pip install omniagent
```

Define tools and call `init()` at startup:

```python
# tools.py
from omniagent import tool, ToolInput, ToolOutput
from pydantic import Field

class ChargeInput(ToolInput):
    amount: float = Field(description="Amount in USD")
    card_token: str = Field(description="Stripe card token")

class ChargeOutput(ToolOutput):
    transaction_id: str

@tool(description="Charge a card for a given amount")
def charge_card(input: ChargeInput) -> ChargeOutput:
    # your business logic here
    return ChargeOutput(transaction_id="txn_123")
```

```python
# main.py — called once at service startup
import tools  # import before init() so @tool decorators run
from omniagent import init

init(
    service="payments",
    namespace="billing",        # optional — tool names become billing.charge_card
    control_plane="http://omniagent:8080",
    api_key="<service-key>",
)
```

`init()` opens a persistent WebSocket to the control plane and registers all tools. The connection stays open; the control plane routes execution requests to your service over it.

**Tools must be stateless.** Consecutive calls in the same session may hit different replicas.

---

## 8. Configure skills and agents

Skills group tools with instructions. Agents combine skills and pick a harness.

```bash
# Store your LLM API key
curl -X POST http://localhost:8080/settings/keys \
  -H "X-OmniAgent-Key: <key>" \
  -d '{"harness": "claude", "api_key": "sk-ant-..."}'

# Create a skill
curl -X POST http://localhost:8080/skills \
  -H "X-OmniAgent-Key: <key>" \
  -d '{
    "name": "billing",
    "tool_names": ["billing.charge_card"],
    "instructions": "Use charge_card when the user wants to make a payment.",
    "system_prompt": "You have access to billing tools."
  }'

# Create an agent
curl -X POST http://localhost:8080/agents \
  -H "X-OmniAgent-Key: <key>" \
  -d '{
    "name": "support-bot",
    "harness": "claude",
    "skill_names": ["billing"],
    "system_prompt": "You are a helpful support assistant."
  }'
```

Supported harnesses: `"claude"` (Claude Code SDK), `"antigravity"` (Gemini).

---

## 9. Run a session

```bash
# Create session (tool schemas are snapshotted here)
SESSION=$(curl -s -X POST http://localhost:8080/sessions \
  -H "X-OmniAgent-Key: <key>" \
  -d '{"agent_id": "<agent-uuid>"}' | jq -r .id)

# Send a message
curl -X POST http://localhost:8080/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: <key>" \
  -d '{"prompt": "Charge $50 to card tok_visa"}'
# → 202 Accepted

# Poll for result
curl http://localhost:8080/sessions/$SESSION/status \
  -H "X-OmniAgent-Key: <key>"

# Or stream events (SSE)
curl -N http://localhost:8080/sessions/$SESSION/stream \
  -H "X-OmniAgent-Key: <key>"
```

SSE event types: `thinking`, `tool_call`, `tool_result`, `error`, `complete`.

---

## 10. Create a client key (for your chat UI)

```bash
curl -X POST http://localhost:8080/settings/client-keys \
  -H "X-OmniAgent-Key: <key>" \
  -d '{"name": "web-ui"}'
```

Returns `{ "key": "..." }` — shown once. Pass it as `X-OmniAgent-Key` from your frontend.

---

## Monty (sandboxed code execution)

Set `use_monty: true` on an agent to enable sandboxed Python execution. The agent gains an `execute_python` tool — code runs in Monty's interpreter with your registered tools available as Python functions. No containers needed.

```bash
curl -X POST http://localhost:8080/agents \
  -d '{"name": "coder", "harness": "antigravity", "skill_names": [...], "use_monty": true}'
```

---

## Key management reference

| Endpoint | Purpose |
|---|---|
| `POST /settings/client-keys` | Create key for chat UI |
| `POST /settings/service-keys` | Create key for a microservice |
| `POST /settings/keys` | Store LLM API key (encrypted) |
| `GET /settings/keys` | List harnesses with stored keys (hint only) |

---

## Docker / production tips

- Run multiple workers by increasing replicas — Procrastinate ensures one job = one worker.
- Control plane can also run as multiple instances — SSE fan-out uses Postgres `LISTEN/NOTIFY`, no extra infra.
- `OMNIAGENT_SECRET_KEY` and `OMNIAGENT_WORKER_SECRET` should come from Docker secrets or k8s secrets, not env files.
- The `.venv` is created by `uv sync` — mount it in your image or use `uv run` directly.
