# Security Policy

## Supported Versions

Only the latest release on `main` receives security patches.

## Reporting a Vulnerability

Do not open a public issue. Email `security@omniagent.dev` with:

- Description of the vulnerability
- Steps to reproduce
- Affected versions

You'll receive an acknowledgment within 48 hours and a timeline for resolution within 5 business days.

## Scope

OmniAgent is a self-hosted agent platform. Security issues may include:

- Unauthorized access to sessions, agents, or tools
- Authentication bypass (API keys, UI password)
- Auth context / credential leakage (encryption at rest, transmission)
- SSRF via `native.http_request` or OpenAPI tool execution
- Sandbox escape (Monty Python executor)

## Security Model

### Authentication

| Layer | Mechanism | Notes |
|-------|-----------|-------|
| API | `X-OmniAgent-Key` header | Argon2 hashed, prefix-keyed lookup, scoped (admin / read / write per resource) |
| UI | Password → session cookie | `UI_PASSWORD` env var, 24-hour session |
| Agent → external APIs | Encrypted auth context (Fernet) | OAuth2 / API keys / basic auth, stored per-namespace-scheme, never returned to frontend |

### Tool execution

- OpenAPI tools call external APIs with injected auth from encrypted context
- Python sandbox (Monty) is default-deny — no filesystem, no network, no `import`, explicit function allowlist only
- `native.http` (planned) will apply SSRF guards: RFC 1918, loopback, link-local blocking

### Cryptography

- API keys: argon2 hashed, never stored in plaintext
- Auth contexts: Fernet (AES-128-CBC + HMAC) encrypted at rest, decrypted only at execution time
- UI sessions: HMAC-signed cookies

### Dependencies

- `pip-audit` runs in CI and pre-commit against the PyPA advisory database
- Dependencies pinned via `uv.lock`

## Disclosure

Security advisories will be published as GitHub Security Advisories. Fixes will be included in the next release with credit to the reporter (unless anonymity is requested).
