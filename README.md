# OmniAgent

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Self-hosted platform for running AI agents across multiple LLM providers. Define tools once in Pydantic — they work with any supported agent harness (Claude, Gemini/Antigravity).

Your microservices annotate functions with `@tool`. OmniAgent discovers them, routes calls, and manages agent sessions. Use the built-in UI or hit the REST API.

---

## Architecture

### E2E Flow

**Setup: services and UI configure the platform**

```mermaid
flowchart LR
    subgraph Service["Service"]
        T["@tool() fns<br/>omniagent.init()<br/>omniagent.router()"]
    end
    subgraph UI["UI"]
        S["Skills tab<br/>Agents tab"]
    end
    subgraph CP["Control Plane"]
        R["/tools/register<br/>/skills<br/>/agents"]
    end
    subgraph DB["Postgres"]
        D["tools<br/>skills<br/>agents<br/>sessions<br/>keys"]
    end

    T -- "POST /tools/register" --> R
    S -- "POST /skills" --> R
    S -- "POST /agents" --> R
    R -- "INSERT" --> D
```

**Execution: chat session run**

```mermaid
flowchart TD
    UI["UI"] -->|"1. POST /sessions"| CP["Control Plane"]
    CP -->|"INSERT session (status=active)"| DB["Postgres"]
    CP -->|"session_id"| UI
    UI -->|"2. POST /sessions/X/run"| CP
    CP -->|"UPDATE status=running"| DB
    CP -->|"DEFER job (Procrastinate)"| Q["Job Queue"]
    CP -->|"202 accepted"| UI
    UI -->|"3. GET /sessions/X/stream"| CP
    CP <-->|"pub/sub"| Redis["Redis<br/>channel: session_X"]
    Redis <-->|"SSE"| UI

    Q -->|"dequeue"| Worker["Worker"]
    Worker -->|"1. SELECT config"| DB
    DB -->|"agent, skills,<br/>tools snapshot"| Worker
    Worker -->|"2. build system prompt"| Worker
    Worker -->|"3. adapter.run()"| Harness["Claude SDK<br/>or Antigravity"]
    Harness -->|"LLM decides<br/>to call tool"| Tool["tool_executor()"]
    Tool -->|"POST /execute"| Service["Service"]
    Service -->|"output dict"| Tool
    Tool -->|"_emit_event()"| Event["/internal/sessions/X/event"]
    Event -->|"UPDATE tool_calls"| DB
    Event -->|"PUBLISH"| Redis
    Harness -->|"final text"| Worker
    Worker -->|"POST /internal/sessions/X/result"| Result["Control Plane"]
    Result -->|"UPDATE status=complete"| DB
    Result -->|"PUBLISH"| Redis
    Redis -->|"SSE: complete"| UI
```

**Internal events**

```mermaid
flowchart LR
    Worker["Worker"] -->|"POST /internal/sessions/X/event"| CP["Control Plane"]
    CP -->|"UPDATE sessions<br/>(tool_calls, messages)"| DB["Postgres"]
    CP -->|"PUBLISH"| Redis["Redis"]
    Redis -->|"SSE"| UI["UI"]

    subgraph Events["Event types"]
        E1["thinking"]
        E2["tool_call"]
        E3["tool_result"]
        E4["error"]
        E5["complete"]
    end
```

**Auth**

```mermaid
flowchart TD
    Req["Request + X-OmniAgent-Key"] --> Resolve["_resolve_key(key)"]
    Resolve -->|"key == OMNIAGENT_INTERNAL_KEY"| Internal["internal ✓"]
    Resolve -->|"argon2 match<br/>api_keys table"| Api["api ✓"]
    Resolve -->|"no match"| Deny["401"]
```

**Monty (use_monty=true)**

```mermaid
flowchart TD
    LLM["LLM"] -->|"execute_python(code, observation)"| Monty["run_monty_code()"]
    Monty -->|"Monty sandbox<br/>runs Python"| PyCall["get_weather(city='Tokyo')"]
    PyCall -->|"_make_sync_tool<br/>asyncio.run(tool_executor)"| Tool["POST /execute"]
    Tool -->|"result dict"| PyCall
    PyCall -->|"last expression"| LLM
```

### Data flow by component

| Component | Reads from | Writes to |
|-----------|-----------|-----------|
| **UI** | Control Plane (REST + SSE) | Control Plane (REST) |
| **Control Plane** | Postgres (config, sessions) | Postgres, Redis (pub), Procrastinate (jobs) |
| **Worker** | Postgres (config), Procrastinate (jobs) | Service HTTP, Control Plane (internal API) |
| **Redis** | Control Plane (publish) | Control Plane (subscribe → SSE → UI) |
| **Service** | Worker (HTTP POST /execute) | Worker (HTTP response) |
| **LLM** | Worker (system prompt + history + tools) | Worker (tool calls + final text) |

**Hierarchy:** `Tool` (code) → `Skill` (config) → `Agent` (config) → `Session` (runtime)

---

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
- Redis 7+
- [uv](https://docs.astral.sh/uv/)

---

## Quick Start

### 1. Install

```bash
git clone <repo>
cd omniagent
uv sync
```

### 2. Start infrastructure

```bash
docker compose up -d
```

Starts Postgres and Redis. Migrations auto-apply on control plane startup.

### 3. Environment variables

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://omniagent:omniagent@localhost:5432/omniagent` |
| `OMNIAGENT_INTERNAL_KEY` | ✅ | Shared secret for CP ↔ Worker communication |
| `OMNIAGENT_API_KEY` | — | API key for services and external UIs (generate via `/settings/api-keys`) |
| `OMNIAGENT_{HARNESS}_API_KEY` | — | LLM API key per harness, e.g. `OMNIAGENT_CLAUDE_API_KEY` |
| `MAX_HISTORY_TURNS` | — | Conversation history limit (default: `50`) |

### 4. Start the control plane

```bash
uv run uvicorn omniagent.control_plane.main:app --host 0.0.0.0 --port 8080
```

API docs at `http://localhost:8080/docs`. UI at `http://localhost:8080/`.

> **Bootstrap:** on first start, the control plane seeds a built-in UI key from `OMNIAGENT_API_KEY` into the `api_keys` table. The UI auto-authenticates — no manual setup. Create additional API keys for services or external UIs via the Settings tab.

### 5. Start workers

```bash
uv run python -m omniagent.worker
```

Scale horizontally — run more instances. Each polls the job queue independently.

### 6. Create an API key

On first run, use the internal key (from `.env`) to bootstrap:

```bash
curl -X POST http://localhost:8080/settings/api-keys \
  -H "X-OmniAgent-Key: $OMNIAGENT_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-service"}'
```

Returns `{ "key": "..." }` — shown once. Pass this to your service as `OMNIAGENT_API_KEY`.

---

## Instrument your service

Define tools with the `@tool` decorator:

```python
from omniagent import tool, ToolInput, ToolOutput
from pydantic import Field

class ChargeInput(ToolInput):
    amount: float = Field(description="Amount in USD")
    card_token: str = Field(description="Stripe card token")

class ChargeOutput(ToolOutput):
    transaction_id: str

@tool(description="Charge a card for a given amount")
async def charge_card(inp: ChargeInput) -> ChargeOutput:
    return ChargeOutput(transaction_id="txn_123")
```

Register at startup and mount the execute route:

```python
import omniagent
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

omniagent.init(
    service="payments",
    namespace="billing",
    control_plane="http://localhost:8080",
    api_key=os.environ["OMNIAGENT_API_KEY"],
    execute_url="http://payments-svc:8001/execute",
)

app = FastAPI()

class ExecuteRequest(BaseModel):
    tool: str
    input: dict

@app.post("/execute")
async def execute(body: ExecuteRequest):
    try:
        output = await omniagent.handle_execute(body.tool, body.input)
        return {"output": output}
    except KeyError as e:
        raise HTTPException(404, detail=str(e)) from e
```

Tools must be stateless — consecutive calls in the same session may hit different replicas.

---

## Configure skills and agents

Via UI (`http://localhost:8080/`) or API:

```bash
# Create a skill
curl -X POST http://localhost:8080/skills \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "billing",
    "version": "v1",
    "tool_names": ["billing.charge_card"],
    "instructions": "Use charge_card when the user wants to make a payment.",
    "system_prompt": "You have access to billing tools."
  }'

# Create an agent
curl -X POST http://localhost:8080/agents \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "support-bot",
    "version": "v1",
    "harness": "claude",
    "skill_refs": {"billing": "v1"},
    "system_prompt": "You are a helpful support assistant."
  }'
```

Supported harnesses: `"claude"` (Claude Code SDK), `"antigravity"` (Gemini).

---

## Run a session

```bash
# Create session
SESSION=$(curl -s -X POST http://localhost:8080/sessions \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "support-bot"}' | jq -r .id)

# Send a message
curl -X POST http://localhost:8080/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Charge $50 to card tok_visa"}'
# → 202 Accepted

# Stream events (SSE)
curl -N http://localhost:8080/sessions/$SESSION/stream \
  -H "X-OmniAgent-Key: <key>"
```

SSE event types: `thinking`, `tool_call`, `tool_result`, `error`, `complete`.

---

## Monty (sandboxed code execution)

Set `use_monty: true` on an agent to enable sandboxed Python execution. The agent gains an `execute_python` tool — code runs in Monty's interpreter with your registered tools available as Python functions. The LLM writes Python, calls tools, and returns the result — all in a single turn. No containers needed.

---

## Key management

| Endpoint | Purpose |
|---|---|
| `POST /settings/api-keys` | Create API key for services, custom UIs, bots |
| `GET /settings/api-keys` | List API keys |
| `DELETE /settings/api-keys/{id}` | Revoke an API key |

LLM API keys are set via environment variables: `OMNIAGENT_CLAUDE_API_KEY` and `OMNIAGENT_ANTIGRAVITY_API_KEY`.

---

## Docker / production tips

- Run multiple workers by increasing replicas — Procrastinate ensures one job = one worker.
- Control plane can run multiple instances — `pg_try_advisory_lock` prevents duplicate startup reconciliation.
- Secrets come from environment variables — use Docker secrets or k8s secrets, not env files.
- The `.venv` is created by `uv sync` — mount it in your image or use `uv run` directly.
