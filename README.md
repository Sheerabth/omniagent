# OmniAgent

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Self-hosted platform for running AI agents across multiple LLM providers. Define tools once in Pydantic — they work with any supported agent harness (Claude, Gemini/Antigravity).

Your microservices annotate functions with `@tool`. OmniAgent discovers them, routes calls, passes `auth_context` (blind-piped) and `llm_context` (LLM-visible), and manages agent sessions. Use the built-in UI or hit the REST API.

---

## Architecture

### Identity & auth layers

OmniAgent authenticates at every hop — zero-trust even on internal networks.

```mermaid
flowchart LR
    subgraph Auth["Auth layers"]
        A1["API Key<br/>X-OmniAgent-Key<br/>(argon2, revocable)"]
        A2["Internal Key<br/>X-OmniAgent-Key<br/>(plain match, env)"]
        A3["Worker Assertion<br/>X-OmniAgent-Assertion<br/>(HS256 JWT, 60s TTL)"]
        A4["Context Blob<br/>opaque Any<br/>(consumer-owned auth)"]
    end

    Client["Client UI"] -->|"API Key"| CP["Control Plane"]
    Worker -->|"Internal Key"| CP
    Worker -->|"JWT Assertion"| Service["Service"]
    Client -->|"auth_context + llm_context<br/>via CP→Worker→Service"| Service
```

### Execution flow

```mermaid
flowchart TD
    UI["Client UI"] -->|"1. POST /sessions/X/run<br/>{prompt, auth_context, llm_context}"| CP["Control Plane"]
    CP -->|"UPDATE sessions<br/>(messages + context)"| DB["Postgres"]
    CP -->|"DEFER job (Procrastinate)<br/>{history, auth_context, llm_context}"| Q["Job Queue"]
    CP -->|"202 accepted"| UI
    UI -->|"2. GET /sessions/X/stream"| CP
    CP <-->|"pub/sub"| Redis["Redis<br/>channel: session_X"]
    Redis <-->|"SSE"| UI

    Q -->|"dequeue"| Worker["Worker"]
    Worker -->|"1. SELECT config"| DB
    DB -->|"agent, skills,<br/>tools snapshot"| Worker
    Worker -->|"2. build system prompt"| Worker
    Worker -->|"3. adapter.run()"| Harness["Claude SDK<br/>or Antigravity"]
    Harness -->|"LLM decides<br/>to call tool"| Tool["tool_executor()"]
    Tool -->|"POST /execute<br/>X-OmniAgent-Assertion: JWT<br/>{tool, input, auth_context, llm_context, session_id}"| Service["Service"]
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

### Context forwarding (identity + personalization)

`auth_context` is blind-piped to tools only — LLM never sees it. `llm_context` is injected into the system prompt for personalization. OmniAgent never reads either.

```mermaid
flowchart LR
    Client["Client UI"] -->|"POST /sessions/X/run<br/>auth_context: {token, org}<br/>llm_context: {name, locale}"| CP["Control Plane"]
    CP -->|"defer_async<br/>payload.auth_context<br/>payload.llm_context"| Worker["Worker"]
    Worker -->|"POST /execute<br/>body.auth_context<br/>body.llm_context"| Service["Service"]
    Worker -->|"_build_system_prompt<br/>llm_context injected"| LLM["LLM"]
    Service -->|"ToolInput.auth_context<br/>ToolInput.llm_context"| Tool["Tool Function"]
    Tool -->|"validate token<br/>call downstream APIs"| Downstream["Consumer's Services"]
```

### Auth verification

```mermaid
flowchart TD
    subgraph "Client → CP"
        Req1["X-OmniAgent-Key"] --> Resolve["_resolve_key(key)"]
        Resolve -->|"env match"| Internal["internal ✓"]
        Resolve -->|"argon2 match"| Api["api ✓"]
        Resolve -->|"no match"| Deny1["401"]
    end
    subgraph "Worker → Service"
        Req2["X-OmniAgent-Assertion"] --> JWT["jwt.decode(HS256)"]
        JWT -->|"valid + not expired"| Allow["200 ✓"]
        JWT -->|"invalid / expired"| Deny2["401"]
    end
```

### Internal events

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

### Monty (use_monty=true)

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
| `OMNIAGENT_INTERNAL_KEY` | ✅ | Shared secret for CP ↔ Worker + Worker → Service JWT assertion |
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
import os
from fastapi import FastAPI, HTTPException, Request

omniagent.init(
    service="payments",
    namespace="billing",
    control_plane="http://localhost:8080",
    api_key=os.environ["OMNIAGENT_API_KEY"],
    execute_url="http://payments-svc:8001/execute",
)

app = FastAPI()

@app.post("/execute")
async def execute(request: Request):
    try:
        output = await omniagent.handle_execute(
            await request.json(), dict(request.headers)
        )
        return {"output": output}
    except ValueError as e:
        raise HTTPException(401, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(500, detail=str(e)) from e
```

`handle_execute` validates the worker JWT assertion (when `OMNIAGENT_INTERNAL_KEY` is set) and injects `auth_context` and `llm_context` into `ToolInput`. Your tool functions receive both automatically — `auth_context` for downstream API calls, `llm_context` for personalization (name, locale, preferences).

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

# Send a message (auth_context blind-piped to tools, llm_context visible to LLM)
curl -X POST http://localhost:8080/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Charge $50 to card tok_visa", "auth_context": {"user_id": "u1", "token": "..."}, "llm_context": {"name": "Alice", "locale": "en-US"}}'
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
