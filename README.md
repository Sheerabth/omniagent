# OmniAgent

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Self-hosted platform for running AI agents across multiple LLM providers. Import any OpenAPI spec as tools and OmniAgent handles auth, routing, and execution. Use the built-in UI or hit the REST API.

---

## Architecture

### Identity & auth layers

OmniAgent authenticates at every hop — zero-trust even on internal networks.

```mermaid
flowchart LR
    subgraph Auth["Auth layers"]
        A1["API Key<br/>X-OmniAgent-Key<br/>(argon2, revocable)"]
        A2["Internal Key<br/>X-OmniAgent-Key<br/>(plain match, env)"]
        A3["Context Blob<br/>opaque Any<br/>(consumer-owned auth)"]
    end

    Client["Client UI"] -->|"API Key"| CP["Control Plane"]
    Worker -->|"Internal Key"| CP
    Client -->|"auth_context (blind-piped to tools)<br/>llm_context (injected into system prompt)"| CP
```

### Execution flow

```mermaid
flowchart TD
    UI["Client UI"] -->|"1. POST /sessions/X/run<br/>{prompt, auth_context, llm_context}"| CP["Control Plane"]
    CP -->|"UPDATE sessions<br/>(messages + context)"| DB["Postgres"]
    CP -->|"DEFER job (Procrastinate)<br/>{history, auth_context, llm_context}"| Q["Job Queue"]
    CP -->|"202 accepted"| UI
    UI -->|"2. GET /sessions/X/stream (SSE)"| CP
    CP <-->|"LISTEN/NOTIFY"| DB

    Q -->|"dequeue"| Worker["Worker"]
    Worker -->|"1. SELECT config"| DB
    DB -->|"agent, skills,<br/>tools snapshot"| Worker
    Worker -->|"2. build system prompt"| Worker
    Worker -->|"3. adapter.run()"| Harness["claude<br/>or antigravity"]
    Harness -->|"LLM decides<br/>to call tool"| Tool["tool_executor()"]
    Tool -->|"HTTP request<br/>+ auth headers"| Service["External API / Service"]
    Service -->|"output dict"| Tool
    Tool -->|"_emit_event()"| Event["/internal/sessions/X/event"]
    Event -->|"UPDATE tool_calls<br/>pg_notify()"| DB
    Harness -->|"final text"| Worker
    Worker -->|"POST /internal/sessions/X/result"| Result["Control Plane"]
    Result -->|"UPDATE status=complete<br/>pg_notify()"| DB
    DB -->|"SSE: complete"| UI
```

### Context forwarding (identity + personalization)

`auth_context` is blind-piped to tools only — LLM never sees it. `llm_context` is injected into the system prompt for personalization. OmniAgent never reads either.

```mermaid
flowchart LR
    Client["Client UI"] -->|"POST /sessions/X/run<br/>auth_context: {token, org}<br/>llm_context: {name, locale}"| CP["Control Plane"]
    CP -->|"defer job<br/>payload.auth_context<br/>payload.llm_context"| Worker["Worker"]
    Worker -->|"auth headers injected<br/>from auth_context + security scheme"| API["External API"]
    Worker -->|"_build_system_prompt<br/>llm_context injected"| LLM["LLM"]
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
    subgraph "Worker → External API"
        Req3["auth_context"] --> Sec["security scheme<br/>(bearer / apiKey / basic / oauth2 / oidc)"]
        Sec -->|"injected at call time"| Ext["External API ✓"]
    end
```

### Data flow by component

| Component | Reads from | Writes to |
|-----------|-----------|-----------|
| **UI** | Control Plane (REST + SSE) | Control Plane (REST) |
| **Control Plane** | Postgres (config, sessions) | Postgres, Procrastinate (jobs) |
| **Worker** | Postgres (config), Procrastinate (jobs) | External APIs, Control Plane (internal API) |
| **Postgres** | Control Plane (writes) | Control Plane (LISTEN/NOTIFY → SSE → UI) |

**Hierarchy:** `Tool` (code or OpenAPI) → `Skill` (config) → `Agent` (config) → `Session` (runtime)

---

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
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

Starts Postgres. Migrations auto-apply on control plane startup.

### 3. Environment variables

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://omniagent:omniagent@localhost:5432/omniagent` |
| `OMNIAGENT_INTERNAL_KEY` | ✅ | Shared secret for CP ↔ Worker + Worker → Service JWT assertion |
| `OMNIAGENT_API_KEY` | — | API key for services and external UIs (generate via `/settings/api-keys`) |
| `OMNIAGENT_{HARNESS}_API_KEY` | — | LLM API key per harness, e.g. `OMNIAGENT_CLAUDE_API_KEY`, `OMNIAGENT_ANTIGRAVITY_API_KEY` |
| `OMNIAGENT_CONTROL_PLANE` | — | URL the worker uses to reach the control plane (default: `http://localhost:8080`) |
| `MAX_HISTORY_TURNS` | — | Conversation history limit (default: `50`) |
| `TOOL_EXECUTION_TIMEOUT` | — | Default HTTP timeout in seconds for tool calls (default: `30`); overridden per-tool via the UI |
| `MONTY_EXECUTION_TIMEOUT` | — | Timeout in seconds for Monty sandbox execution (default: `30`) |
| `MONTY_EXECUTOR_WORKERS` | — | Thread pool size for Monty (default: `4`) |

### 4. Start the control plane

```bash
uv run uvicorn omniagent.api.main:app --host 0.0.0.0 --port 8080
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

Returns `{ "key": "..." }` — shown once.

---

## Import tools from an OpenAPI spec

Point OmniAgent at any OpenAPI 3.x spec and it becomes a set of callable tools. No code changes to the target service.

```bash
curl -X POST http://localhost:8080/tools/import-openapi \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "spec": "<YAML or JSON string, or parsed JSON object>",
    "namespace": "weather",
    "base_url": "https://api.example.com"
  }'
```

`base_url` is optional — used when the spec has no `servers` entry or you want to override it. Tool names are derived from the operation `summary` (slugified), falling back to `operationId`, then `{method}_{path}`.

Delete tools after import:

```bash
# Delete one tool
curl -X DELETE http://localhost:8080/tools/weather.get_weather -H "X-OmniAgent-Key: <key>"

# Delete all tools in a namespace
curl -X DELETE http://localhost:8080/tools/namespace/weather -H "X-OmniAgent-Key: <key>"
```

---

## Auth for OpenAPI tools

OmniAgent reads security schemes from the OpenAPI spec and injects credentials at call time from the agent's `auth_context`. All standard OpenAPI auth types are supported:

| Scheme | How | `auth_context` keys |
|--------|-----|---------------------|
| `http bearer` | `Authorization: Bearer <token>` | `token` |
| `http basic` | `Authorization: Basic <b64>` | `username`, `password` |
| `apiKey` (header) | Custom header (name from spec) | scheme name from spec |
| `apiKey` (query) | Query param (name from spec) | scheme name from spec |
| `apiKey` (cookie) | Cookie (name from spec) | scheme name from spec |
| `oauth2` client credentials | Exchanges `client_id`/`client_secret` for token | `client_id`, `client_secret` |
| `oauth2` refresh token | Exchanges refresh token, caches access token | `client_id`, `client_secret`, `refresh_token` |
| `oauth2` auth code | Browser redirect → token exchange → stored refresh | `client_id`, `client_secret` *(after connect)* |
| `openIdConnect` | Discovery → token exchange | `client_id`, `client_secret` |

**Token caching:** OAuth2 and OIDC access tokens are cached in memory and refreshed 30s before expiry. OIDC discovery documents are cached per issuer.

**OAuth2 authorization code** requires a one-time browser connect flow. OmniAgent provides `GET /oauth2/connect` (redirects to provider) and `GET /oauth2/callback` (exchanges code, stores tokens into agent's `auth_context`). After connecting, the worker handles token refresh automatically — works with Google, Slack, GitHub, Notion, and any OAuth2-compliant provider.

Set `auth_context` on the agent (stored, used as default for all sessions) or pass it per-run (overrides agent default):

```bash
curl -X POST http://localhost:8080/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What is the weather in Tokyo?",
    "auth_context": {
      "token": "my-bearer-token",
      "APIKeyHeader": "my-api-key",
      "username": "admin",
      "password": "secret",
      "client_id": "my-client",
      "client_secret": "my-secret"
    }
  }'
```

---

## Configure skills and agents

Via UI (`http://localhost:8080/`) or API:

```bash
# Create a skill
curl -X POST http://localhost:8080/skills \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "weather",
    "version": "v1",
    "tool_names": ["weather.get_weather", "weather.get_uv_index"],
    "instructions": "Use these tools to answer weather-related questions.",
    "system_prompt": "You have access to weather tools."
  }'

# Create an agent
curl -X POST http://localhost:8080/agents \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "weather-bot",
    "version": "v1",
    "harness": "claude",
    "skill_refs": {"weather": "v1"},
    "system_prompt": "You are a helpful weather assistant."
  }'
```

Supported harnesses: `"claude"`, `"antigravity"` (Gemini).

---

## Run a session

```bash
# Create session
SESSION=$(curl -s -X POST http://localhost:8080/sessions \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "weather-bot"}' | jq -r .id)

# Send a message
curl -X POST http://localhost:8080/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Whats the weather in Tokyo?", "auth_context": {"token": "..."}, "llm_context": {"name": "Alice"}}'
# → 202 Accepted

# Stream events (SSE)
curl -N http://localhost:8080/sessions/$SESSION/stream \
  -H "X-OmniAgent-Key: <key>"
```

SSE event types: `thinking`, `tool_call`, `tool_result`, `system_prompt`, `error`, `complete`.

---

## Monty (sandboxed code execution)

Set `use_monty: true` on an agent to enable sandboxed Python execution. The agent gains an `execute_python` tool — code runs in Monty's interpreter with your registered tools available as plain Python functions. The LLM writes a single Python block, calls tools, and returns the result. No containers needed, 0.004ms sandbox startup.

---

## Key management

| Endpoint | Purpose |
|---|---|
| `POST /settings/api-keys` | Create API key for services, custom UIs, bots |
| `GET /settings/api-keys` | List API keys |
| `DELETE /settings/api-keys/{id}` | Revoke an API key |

LLM API keys are set via environment variables: `OMNIAGENT_{HARNESS}_API_KEY` (e.g. `OMNIAGENT_ANTIGRAVITY_API_KEY`).

---

## Docker / production tips

- Run multiple workers by increasing replicas — Procrastinate ensures one job = one worker.
- Control plane can run multiple instances — `pg_try_advisory_lock` prevents duplicate startup reconciliation.
- Secrets come from environment variables — use Docker secrets or k8s secrets, not env files.
- The `.venv` is created by `uv sync` — mount it in your image or use `uv run` directly.
- SSE uses PostgreSQL `LISTEN/NOTIFY` — no Redis required.
