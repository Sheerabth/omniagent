"""OpenAPI 3.x spec parser — converts endpoints to tool definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    openapi_method: str
    openapi_path: str
    openapi_base_url: str
    openapi_security: dict | None = field(default=None)


def _resolve_refs(obj: Any, components: dict, _seen: frozenset = frozenset()) -> Any:
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref in _seen:
                return {}
            parts = ref.lstrip("#/").split("/")
            target: Any = {"components": components}
            for p in parts:
                p = p.replace("~1", "/").replace("~0", "~")
                target = target.get(p, {}) if isinstance(target, dict) else {}
            return _resolve_refs(target, components, _seen | {ref})
        return {k: _resolve_refs(v, components, _seen) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_refs(i, components, _seen) for i in obj]
    return obj


def _flatten(schema: dict) -> dict:
    if not schema:
        return {}
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for sub in schema["allOf"]:
            sub = _flatten(sub)
            merged["properties"].update(sub.get("properties", {}))
            merged["required"].extend(sub.get("required", []))
        if not merged["required"]:
            del merged["required"]
        return merged
    if "oneOf" in schema or "anyOf" in schema:
        subs = schema.get("oneOf") or schema.get("anyOf", [])
        for sub in subs:
            if sub.get("type") not in ("null",):
                return _flatten(sub)
        return _flatten(subs[0]) if subs else {}
    return schema


def _snake(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")


def _expand_server_vars(url: str, variables: dict) -> str:
    for name, var in variables.items():
        url = url.replace(f"{{{name}}}", var.get("default", ""))
    return url


def _resolve_security(sec_reqs: list, resolved_schemes: dict) -> dict | None:
    if not sec_reqs or not sec_reqs[0]:
        return None
    try:
        scheme_name = next(iter(sec_reqs[0]))
    except StopIteration:
        return None
    scheme = resolved_schemes.get(scheme_name, {})
    stype = scheme.get("type", "")
    if stype == "http":
        if scheme.get("scheme", "bearer").lower() == "basic":
            return {"type": "basic", "username_key": "username", "password_key": "password"}
        return {"type": "bearer", "token_key": "token"}
    if stype == "apiKey":
        return {
            "type": "apiKey",
            "in": scheme.get("in", "header"),
            "name": scheme.get("name", "X-Api-Key"),
            "token_key": scheme_name,
        }
    if stype == "oauth2":
        token_url = ""
        for flow in scheme.get("flows", {}).values():
            token_url = flow.get("tokenUrl", "")
            if token_url:
                break
        return {
            "type": "oauth2",
            "token_url": token_url,
            "client_id_key": "client_id",
            "client_secret_key": "client_secret",
            "refresh_token_key": "refresh_token",
            "scopes": list(sec_reqs[0].get(scheme_name, [])),
        }
    if stype == "openIdConnect":
        return {
            "type": "oidc",
            "issuer": scheme.get("openIdConnectUrl", ""),
            "client_id_key": "client_id",
            "client_secret_key": "client_secret",
            "refresh_token_key": "refresh_token",
            "scopes": list(sec_reqs[0].get(scheme_name, [])),
        }
    return None


def parse_spec(spec: dict, namespace: str, base_url: str | None = None) -> list[ParsedTool]:
    if "swagger" in spec:
        raise ValueError("OpenAPI 2.x (Swagger) not supported — convert to OpenAPI 3.x first")
    if spec.get("openapi", "3")[:1] not in ("3",):
        raise ValueError(f"Unsupported OpenAPI version: {spec.get('openapi')}")

    components = spec.get("components", {})
    servers = spec.get("servers", [])
    server = servers[0] if servers else {}
    server_url = server.get("url", "")
    if server_url and "{" in server_url:
        server_url = _expand_server_vars(server_url, server.get("variables", {}))
    server_url = server_url.rstrip("/")

    if base_url:
        host = base_url.rstrip("/")
        spec_base = host + server_url if server_url.startswith("/") else host
    else:
        spec_base = server_url
    if not spec_base.startswith(("http://", "https://")):
        raise ValueError(
            f"Cannot resolve absolute base URL (got {spec_base!r}). "
            "Provide base_url in the request body."
        )

    resolved_paths = _resolve_refs(spec.get("paths", {}), components)
    resolved_schemes = _resolve_refs(components.get("securitySchemes", {}), components)
    global_security = spec.get("security") or []

    tools: list[ParsedTool] = []
    for path, path_item in resolved_paths.items():
        path_params = path_item.get("parameters", [])
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op = path_item.get(method)
            if not op:
                continue

            op_id = op.get("operationId", "")
            summary_slug = _snake(op.get("summary", ""))
            raw_name = summary_slug or op_id or f"{method}_{_snake(path)}"
            name = f"{namespace}.{raw_name}"
            description = op.get("summary") or op.get("description") or ""

            properties: dict[str, Any] = {}
            required: list[str] = []
            for param in path_params + op.get("parameters", []):
                pname = param["name"]
                pschema = _flatten(param.get("schema", {"type": "string"}))
                if param.get("description"):
                    pschema = {**pschema, "description": param["description"]}
                pschema["x-param-in"] = param.get("in", "query")
                properties[pname] = pschema
                if param.get("required") or param.get("in") == "path":
                    required.append(pname)

            rb = op.get("requestBody", {})
            if rb:
                content = rb.get("content", {})
                rb_schema = _flatten(
                    content.get(
                        "application/json",
                        content.get(
                            "multipart/form-data",
                            content.get("application/x-www-form-urlencoded", {}),
                        ),
                    ).get("schema", {})
                )
                for prop_name, prop_schema in rb_schema.get("properties", {}).items():
                    tagged = dict(prop_schema)
                    tagged["x-param-in"] = "body"
                    properties[prop_name] = tagged
                required.extend(rb_schema.get("required", []))

            input_schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = list(dict.fromkeys(required))

            responses = op.get("responses", {})
            success_resp = next(
                (responses[c] for c in ("200", "201", "202", "204") if c in responses), {}
            )
            output_schema = _flatten(
                success_resp.get("content", {}).get("application/json", {}).get("schema", {})
            )

            sec_reqs = op.get("security") if "security" in op else global_security
            security = _resolve_security(sec_reqs or [], resolved_schemes)

            tools.append(
                ParsedTool(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    openapi_method=method.upper(),
                    openapi_path=path,
                    openapi_base_url=spec_base,
                    openapi_security=security,
                )
            )

    return tools
