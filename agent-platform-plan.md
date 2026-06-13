# OmniAgent — Design Plan

## What is this

OSS, self-hosted platform for running AI agents across multiple LLM providers.
Tools defined once in Pydantic, work across any agent harness (Claude, Antigravity, etc.).
Normal microservices annotate tools with `@tool()` — OmniAgent discovers, routes, and executes.
Users build their own chat UI and hit OmniAgent's REST API.

---

## Core Stack

| Component | Role |
|-----------|------|
| Python + FastAPI | Control plane + SDK language/framework |
| Pydantic | Tool input/output schema + validation |
| Monty (`pip install pydantic-monty`) | Sandboxed Python execution inside worker — no container needed |
| Claude Agent SDK (`pip install claude-agent-sdk`) | Anthropic agent harness, runs in worker |
| Antigravity SDK (`pip install antigravity`) | Google agent harness, runs in worker |
| Postgres | Skills, agents, sessions, job queue, secrets — single DB |
| Procrastinate | Python-native Postgres job queue |

---

## Auth

All API calls authenticated via shared secret in header `X-OmniAgent-Key`.
Three separate keys — each issued independently:

| Caller | Key type | Config |
|--------|----------|--------|
| Client (user's chat UI) | Client key | Created via `POST /settings/client-keys`, passed in header |
| Service (user's microservice) | Service key | Set in `omniagent.init(api_key=...)` |
| Worker | Worker key | Set via `OMNIAGENT_WORKER_SECRET` env var |

Control plane validates `X-OmniAgent-Key` on every request. Public endpoints — none.

---

## Hierarchy

```
Tool        code layer — @tool() in user's service, requires redeploy to change
Skill       config layer — runtime configurable, stored in DB, references tools by namespaced name
Agent       config layer — runtime configurable, stored in DB, references skills by name
Session     runtime — locked to one agent, identified by UUID
```

- **Tools** = code. Only layer requiring redeploy.
- **Skills + Agents** = data. Created/edited at runtime via API. No redeploy needed.
- Tools ↔ Skills: many-to-many (same tool in multiple skills)
- Skills ↔ Agents: many-to-many (same skill in multiple agents)

---

## Tool Definition

### Imports

```python
from omniagent import tool, ToolInput, ToolOutput
```

### Base classes

```python
class ToolInput(BaseModel):
    observation: str  # required — agent must state why it's calling this tool

class ToolOutput(BaseModel):
    pass
```

### Decorator

```python
@tool(description="Charge a card for a given amount")
def charge_card(input: ChargeInput) -> ChargeOutput:
    ...
```

- Input/output types inferred via `typing.get_type_hints()`
- Both must subclass `ToolInput` / `ToolOutput` — enforced at decoration time, fails at import
- Description falls back to docstring if not provided in decorator
- Sync and async both supported — sync auto-wrapped in `asyncio.run_in_executor`
- Populates library-internal local registry at import time

### Local registry (library-internal, never touched by user)

```python
_local_registry[fn.__name__] = {
    "fn": fn,
    "input": ChargeInput,
    "output": ChargeOutput,
    "description": "...",
    "schema": ChargeInput.model_json_schema(),
}
```

On `omniagent.init()` WS connect → full registry synced to control plane DB automatically.

**Tools must be stateless.** Control plane round-robins across all replicas in namespace pool — consecutive calls in same session may hit different replicas. In-process state silently breaks.

---

## Skill (runtime)

```python
POST /skills { name, tool_names[], instructions, system_prompt }
```

- `tool_names[]` — namespaced tool names e.g. `"billing.charge_card"`, `"auth.verify_token"`
- Validated at creation — all `tool_names` must exist in control plane registry
- `instructions` — tells agent when/how to use the tools in this skill
- `system_prompt` — skill-level system prompt, combined with agent system prompt at run time
- Many-to-many: same skill assignable to multiple agents
- Stored in DB, editable without code changes

---

## Agent (runtime)

```python
POST /agents { name, harness, skill_names[], system_prompt, use_monty }
```

- `harness`: `"claude"` | `"antigravity"`
- `skill_names[]` validated at creation — all must exist in DB
- `system_prompt` — agent-level behavior, personality, constraints
- `use_monty`: bool — whether worker activates Monty for sandboxed code execution (default: false)
- Many-to-many: same skill used across multiple agents
- Stored in DB, editable without code changes

### PATCH behaviour on active sessions
`PATCH /agents/{id}` and `PATCH /skills/{id}` both take effect on the **next** run of any session using that agent/skill.
Existing sessions' `tool_snapshot` is unchanged (tools locked at session creation).
`system_prompt`, `skill_names`, `instructions` are NOT snapshotted — they update on next run.
This cross-layer inconsistency is intentional: tool schemas are versioned, agent/skill behaviour is not.

### System prompt construction (at run time, by worker)

```
{agent.system_prompt}

Skills:
{for each skill: skill.system_prompt}
{for each skill: skill.instructions}

Available tools:
{for each tool in session.tool_snapshot:
    - name, description, input schema (with field descriptions), output schema}
```

Worker uses `job.agent_config.tool_snapshot` — NOT live registry — to build tool list in system prompt.
Agent never guesses what tools exist; full context injected before first prompt.
Field-level descriptions come from Pydantic `Field(description="...")` on user's models.

---

## Session

- UUID, created explicitly by user, locked to one agent
- Tool schemas snapshotted at creation from live registry — existing sessions immune to tool schema changes
- Stores conversation history for multi-turn
- Concurrent runs rejected: `POST /sessions/{id}/run` returns 409 if session status is `"running"`

### Schema

```
session
├── id: UUID
├── agent_id: UUID
├── created_at: datetime
├── status: "active" | "running" | "complete" | "failed"
├── tool_snapshot: dict                           ← full tool schemas at session creation time
├── messages: list[{ role, content, timestamp }]  ← role: "user" | "assistant" only
└── tool_calls: list[ToolCallEntry]               ← tool call audit log
```

### ToolCallEntry

```python
class ToolCallEntry(BaseModel):
    tool_name: str       # namespaced e.g. "billing.charge_card"
    input: dict          # full input dict including observation field
    output: dict
    harness: str         # e.g. "claude", "antigravity"
    timestamp: datetime
    success: bool
    error: str | None
```

Note: `observation` is inside `input` dict (it's part of `ToolInput`) — not duplicated at top level.

### Hard rules
- Session store has zero imports from any agent SDK
- No agent-specific types, IDs, message formats, model names, tokens, cost data
- Adding a new harness = zero changes to session store

### Session TTL / cleanup
No expiry policy in v1. Flag for v2.

### Conversation history size
`max_history_turns` config option on control plane (default: 50 turns).
Worker trims history to last N turns before building job payload.
Prevents unbounded Postgres row sizes on long sessions.

---

## Deployment Architecture

```
User's microservices                      OmniAgent
─────────────────────────────             ─────────────────────────────────────
Payment Service (3 replicas)              Control Plane (FastAPI)
├── business logic                        ├── REST API
├── own DB                                ├── Tool Registry (Postgres)
└── omniagent SDK                         ├── Skills / Agents / Sessions DB
    ├── @tool() definitions               ├── Secrets store (encrypted in Postgres)
    ├── omniagent.init(...)  ──────────── ├── WS server
    └── persistent WS ─────────────────► │   └── namespace pool: billing → [ws1, ws2, ws3]
                                          ├── Job queue (Procrastinate/Postgres)
Auth Service                              └── Postgres LISTEN/NOTIFY (SSE fan-out)
└── omniagent SDK (same pattern)
                                          Agent Workers (pool of Python processes)
                                          ├── stateless, always running
                                          ├── polls job queue via Procrastinate
                                          ├── runs harness (Claude/Antigravity SDK)
                                          ├── Monty (if agent.use_monty = true)
                                          ├── POST /internal/tools/execute → control plane
                                          └── POST /internal/sessions/{id}/result → control plane
```

---

## SDK Init & Tool Registration

```python
# tools.py
from omniagent import tool, ToolInput, ToolOutput

@tool(description="Charge a card")
def charge_card(input: ChargeInput) -> ChargeOutput: ...

# main.py — called once on service startup
import tools
from omniagent import init

init(
    service="payments",       # required — service identifier
    namespace="billing",      # optional — overrides tool prefix
                              # without namespace: payments.charge_card
                              # with namespace:    billing.charge_card
    control_plane="http://omniagent:8080",
    api_key="...",            # X-OmniAgent-Key for service ↔ control plane auth
)
```

`init()` flow (once on startup):
1. Stores config, opens persistent WebSocket to control plane (header: `X-OmniAgent-Key`)
2. On WS connect → reads `_local_registry` → sends `register` message with all tools
3. Control plane adds tools to DB, adds WS connection to namespace pool
4. WS stays open — heartbeat every 30s (ping/pong), control plane sends execute requests over it

### Namespace pool & multi-replica routing
Multiple replicas of same service → multiple WS connections for same namespace.
Control plane round-robins execution requests across all live connections in the namespace pool.
Registration is idempotent — same tool schema registered N times, N connections added to pool.
Namespace collision (two different services, same namespace) → error on registration.

---

## WebSocket Message Format

All WS messages are JSON with a `type` field.
Heartbeat: control plane sends `{ "type": "ping" }` every 30s, service responds `{ "type": "pong" }`.
If no pong within 10s, connection considered dead — removed from namespace pool, tools marked unavailable.

### Service → Control plane (on connect)
```json
{
  "type": "register",
  "service": "payments",
  "namespace": "billing",
  "tools": [
    {
      "name": "billing.charge_card",
      "description": "Charge a card for a given amount",
      "input_schema": { ... },
      "output_schema": { ... }
    }
  ]
}
```

### Control plane → Service (tool execution request)
```json
{
  "type": "execute",
  "request_id": "uuid",
  "tool_name": "billing.charge_card",
  "input": { "observation": "...", "amount": 100, "currency": "USD" }
}
```

### Service → Control plane (tool execution response)
```json
{
  "type": "execute_result",
  "request_id": "uuid",
  "success": true,
  "output": { ... },
  "error": null
}
```

### WS in-flight execution on disconnect
Control plane holds pending-request map: `{ request_id → (worker_callback, timeout) }`.
If WS drops before `execute_result` received → pending request times out (default: 30s) → worker gets clean error `{ "error": "tool_timeout", "tool": "billing.charge_card" }`.

---

## Tool Execution Flow

```
Worker needs billing.charge_card
  → POST /internal/tools/execute { tool_name, input, session_id } to control plane
  → control plane looks up namespace pool for "billing"
  → picks WS connection (round-robin)
  → adds to pending-request map with 30s timeout
  → sends execute message over WS
  → service executes locally (Pydantic validates input, Pydantic validates output)
  → service sends execute_result over WS
  → control plane removes from pending-request map
  → logs ToolCallEntry to session
  → returns result to worker
```

---

## Orchestration Flow

```
Client:
  POST /sessions/{id}/run { prompt }          ← X-OmniAgent-Key header required
  → 409 if session.status == "running"
  → 202 Accepted { session_id }               ← job_id removed, session_id sufficient

Control plane:
  → sets session.status = "running"
  → appends { role: "user", content: prompt, timestamp } to session messages
  → trims history to last max_history_turns turns
  → builds job payload:
    {
      session_id,
      agent_config: { harness, system_prompt, skills, tool_snapshot, use_monty },
      llm_api_key,       ← fetched from secrets store
      history: messages  ← trimmed conversation history
    }
  → pushes to Procrastinate queue

Worker picks up job:
  → builds final system prompt (agent + skills + tool_snapshot)
  → initialises harness with system prompt + history
  → runs harness agent loop:
      LLM responds with tool call
      → POST /internal/tools/execute to control plane
      → result returned → harness continues
      LLM produces final text response
  → POST /internal/sessions/{id}/result { result } to control plane

Control plane:
  → appends { role: "assistant", content: result, timestamp } to session messages
  → updates session.status = "complete"
  → PG NOTIFY on session channel → SSE subscribers receive complete event

Worker crash handling (Procrastinate):
  → job timeout configured (default: 10 min)
  → Procrastinate runs inside control plane process in monitor-only mode (no job execution — workers execute jobs, control plane only schedules and monitors)
  → on job timeout/crash → Procrastinate fires on_job_failure callback (registered at startup)
  → callback updates session.status = "failed", PG NOTIFY → SSE error event emitted

Control plane crash → stuck sessions:
  → on_job_failure callback never fires if control plane crashes mid-job
  → on control plane restart: reconciliation pass queries Procrastinate job table for failed/timed-out jobs, updates any orphaned sessions stuck in "running" to "failed"
  → reconciliation runs once at startup before accepting traffic
```

---

## Workers

- Fixed pool, always running, stateless Python processes
- Control plane never discovers, tracks, or contacts workers — completely unaware
- Workers reach OUT only: poll queue, call control plane for tool execution, post results
- Scale = run more worker processes; Procrastinate ensures one job = one worker, no double execution
- LLM API keys never stored on worker — injected per job from control plane secrets store

**Configuration (env vars only):**
```bash
OMNIAGENT_CONTROL_PLANE=http://omniagent:8080
OMNIAGENT_WORKER_SECRET=...     # X-OmniAgent-Key for worker ↔ control plane auth

python -m omniagent.worker
```

Scale = run more. Docker/k8s = increase replicas. No other config.

---

## Monty (inside worker)

- `pip install pydantic-monty`
- Activated only if `agent.use_monty = true`
- Handles agent-generated Python code execution — sandboxed, no container needed
- Registered tools (from tool_snapshot) exposed as Monty `external_functions`
- Monty type bridge: deserializes Monty args → dict → Pydantic model, serializes output back
- Runs inside worker process, <1μs startup

---

## Harness Adapter Contract

All harness adapters must implement this abstract base:

```python
from abc import ABC, abstractmethod
from typing import Callable, Awaitable

class HarnessAdapter(ABC):

    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        history: list[dict],                                    # [{ role, content }]
        tool_executor: Callable[[str, dict], Awaitable[dict]], # async fn(tool_name, input) -> output dict
        emit_event: Callable[[dict], Awaitable[None]],         # async fn({ type, ... }) -> None
        use_monty: bool,
    ) -> str:                                                   # final text response
        ...
```

- `tool_executor` and `emit_event` are injected by the worker — adapters never hold control plane URL, auth keys, or session IDs
- Worker creates these callables pre-configured before passing to adapter
- Community adapters implement only the LLM loop logic
- Worker discovers adapter by `agent.harness` string, instantiates correct class

---

## SSE Streaming

Polling alone insufficient — agent runs take minutes. SSE gives live visibility.
Additive — polling still works for final result.

### Fan-out mechanism
Control plane may run as multiple instances. SSE fan-out uses Postgres `LISTEN/NOTIFY` — zero extra infra.
Worker posts events to control plane → control plane does `PG NOTIFY session_{id} payload` → all control plane instances listening on that channel fan out to their SSE subscribers.

```
GET /sessions/{id}/stream                    ← X-OmniAgent-Key header required

→ data: { "type": "thinking", "content": "..." }
→ data: { "type": "tool_call", "tool": "billing.charge_card", "input": { ... } }
→ data: { "type": "tool_result", "tool": "billing.charge_card", "success": true }
→ data: { "type": "error", "reason": "..." }
→ data: { "type": "complete", "result": "..." }
```

Event types: `thinking`, `tool_call`, `tool_result`, `error`, `complete`

---

## Secrets / Encryption

### LLM API keys
Stored encrypted in Postgres. Algorithm: AES-256-GCM.
Encryption key source: `OMNIAGENT_SECRET_KEY` env var on control plane (32-byte hex string).
Key management: user's responsibility (Docker secrets, k8s secrets, etc.).
Never returned via API after creation — only `key_hint` (last 4 chars) on GET.

### Client keys
Generated as random 32-byte token, shown once to user on creation, never stored plaintext.
Stored as argon2 hash in Postgres. Verified on each request by hashing incoming key and comparing.
Same UX as GitHub personal access tokens — lost key = create new one.

---

## API

All endpoints require `X-OmniAgent-Key` header.

### Tools (discovery)
```
GET    /tools                    → all registered tools across all services
GET    /tools/{namespace}        → tools for namespace (matched on namespace field, not service name)
```

### Skills
```
POST   /skills                   { name, tool_names[], instructions, system_prompt }  → skill
GET    /skills                   → list[skill]
GET    /skills/{id}              → skill
PATCH  /skills/{id}              { tool_names?, instructions?, system_prompt? }
DELETE /skills/{id}
```

### Agents
```
POST   /agents                   { name, harness, skill_names[], system_prompt, use_monty }  → agent
GET    /agents                   → list[agent]
GET    /agents/{id}              → agent
PATCH  /agents/{id}              { skill_names?, harness?, system_prompt?, use_monty? }
DELETE /agents/{id}
```

### Sessions
```
POST   /sessions                 { agent_id }          → { session_id }
POST   /sessions/{id}/run        { prompt }             → 202 { session_id } | 409 if running
GET    /sessions/{id}/status     → { status, result, messages, tool_calls }
GET    /sessions/{id}/stream     → SSE stream
```

### Settings
```
POST   /settings/client-keys     { name }               → { key }  ← shown once, never stored plaintext (argon2 hash stored)
GET    /settings/client-keys     → list[{ id, name, created_at }]  ← key never returned again
DELETE /settings/client-keys/{id}

POST   /settings/keys            { harness, api_key }   → stored encrypted
GET    /settings/keys            → list[{ harness, key_hint }]
DELETE /settings/keys/{harness}
```

### Internal (worker + service only — validated by worker/service key)
```
POST   /internal/tools/execute          { tool_name, input, session_id }  → { output }
POST   /internal/sessions/{id}/result   { result }
POST   /internal/sessions/{id}/event    { type, ... }   ← worker SSE event emission
```

---

## Error Handling

### Pydantic validation errors
- `e.errors()` → structured JSON (field, message, type) returned to agent
- Agent reads field + reason, retries with corrected input

### Dead service / unavailable tool
- WS disconnect → namespace pool updated → tools marked unavailable
- Agent calls unavailable tool → `{ "error": "tool_unavailable", "tool": "billing.charge_card" }`
- Emitted as `error` SSE event

### Tool execution timeout
- 30s timeout per tool call (configurable)
- On timeout → `{ "error": "tool_timeout", "tool": "billing.charge_card" }`

### Worker crash / job timeout
- Procrastinate job timeout: 10 min (configurable)
- On crash/timeout → session.status = `"failed"`, SSE `error` event emitted

### Concurrent run rejection
- `POST /sessions/{id}/run` while status == `"running"` → 409 Conflict

### Antigravity Policy System
- Antigravity has a policy layer that blocks tool calls before execution
- Error format differs from normal tool exceptions — exact shape TBD (spike in step 0)
- Normalize in Antigravity adapter: wrap all tool calls, convert to `{ "error": "policy_blocked", "reason": "..." }`

---

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Language | Python | Both agent SDKs are Python-first |
| Control plane framework | FastAPI | Async, fast, Pydantic-native |
| Tool I/O | Single Pydantic model each | Clean schema, consistent across all adapters |
| `observation` field | Required on ToolInput, stored in input dict | Agent reasoning trace on every tool call |
| Tool namespacing | `namespace.tool_name` (defaults to service name) | Avoids conflicts, supports monolith modules |
| Namespace collision | Error on registration | Two different services, same namespace = explicit conflict |
| Multi-replica routing | Round-robin across namespace WS pool | Scales naturally, no sticky sessions needed |
| Tool ↔ Skill ↔ Agent | Many-to-many by name | Reuse without copying, update propagates instantly |
| Session ownership | Platform, not agent | Agent-agnostic |
| Skills/Agents | Runtime configurable | No redeploy to change agent behavior |
| System prompt | Agent + Skills combined at run time, uses tool_snapshot | Versioned tool context, flexible config |
| Conversation history | `"user"` / `"assistant"` roles only | Tool calls stored in tool_calls[] |
| History trimming | `max_history_turns` (default 50) | Prevents unbounded job payload size |
| Concurrent runs | 409 reject | Prevents session state corruption |
| Worker model | Fixed pool, stateless | Simple, predictable, horizontally scalable |
| Auth | Shared secret `X-OmniAgent-Key`, three key types | Simple inter-service auth, no OAuth overhead |
| LLM API keys | AES-256-GCM encrypted in Postgres, key from env var | Centralized, never on workers/services |
| Service ↔ Control plane | WebSocket + 30s heartbeat | No inbound port on service, detects dead connections |
| Worker ↔ Control plane | HTTP + Procrastinate polling | Stateless workers, natural job queue fit |
| SSE fan-out | Postgres LISTEN/NOTIFY | Zero extra infra, works across multiple control plane instances |
| WS in-flight timeout | Pending-request map + 30s timeout | Clean error on disconnect mid-execution |
| Monty activation | Per-agent `use_monty` flag | Only pay overhead when needed |
| Harness adapter | Abstract base class `HarnessAdapter` | Community-buildable, consistent interface |
| Session TTL | Not in v1 | Flag for v2 |
| MCP | Optional export, not required | Direct registration sufficient |
| OSS | Yes | Self-hosted, community builds adapters |

---

## Build Order

0. **Spike** — run Antigravity SDK, trigger a policy block, document exact error format. Unblocks step 7.
1. `ToolInput` / `ToolOutput` base models + `@tool()` decorator + local registry
2. Control plane — FastAPI skeleton + Postgres schema (tools, skills, agents, sessions, secrets, client-keys)
3. Auth middleware — `X-OmniAgent-Key` validation, three key types, client key management endpoints
4. Secrets store — LLM API keys, AES-256-GCM, `OMNIAGENT_SECRET_KEY` env var
5. SDK — `omniagent.init()` + WS connection + tool sync on connect + 30s heartbeat
6. WS server on control plane — register/execute/execute_result/ping/pong + namespace pool + pending-request map + timeout
7. Procrastinate job queue + worker skeleton + crash/timeout → session `"failed"` handler
8. Antigravity SDK adapter in worker (implements `HarnessAdapter`)
9. Tool execution routing: worker → `/internal/tools/execute` → control plane → WS → service
10. Session polling API + conversation history + concurrent run guard (409)
11. SSE streaming — `/internal/sessions/{id}/event` + Postgres LISTEN/NOTIFY fan-out + `GET /sessions/{id}/stream`
12. Claude Agent SDK adapter in worker (implements `HarnessAdapter`) — validates harness-agnostic claim
13. Monty adapter inside worker (activated by `use_monty` flag)
14. MCP export (optional, later)

---

## Open Questions

- Antigravity Policy System error format — resolve in step 0 spike
- Multi-tenant (multiple teams, one control plane) — v2
- Worker spin-up-on-demand — v2
- Session TTL / cleanup — v2
