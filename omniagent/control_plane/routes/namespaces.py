"""Auth management per tool namespace."""

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_scope
from omniagent.control_plane.crypto import decrypt_auth_context, encrypt_auth_context
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import NamespaceAuthSet, NamespaceRecord

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


@router.get("", response_model=list[NamespaceRecord])
async def list_namespaces(_=Depends(require_scope("tools:read"))) -> list[NamespaceRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT namespace, COUNT(*) AS tool_count FROM tools GROUP BY namespace ORDER BY namespace"
        )
        tool_rows = await rows.fetchall()
        if not tool_rows:
            return []
        namespaces = [r["namespace"] for r in tool_rows]
        auth_rows = await (
            await conn.execute(
                "SELECT namespace, auth_context FROM namespace_auth WHERE namespace = ANY(%s)",
                (namespaces,),
            )
        ).fetchall()
        auth_map: dict[str, list[str]] = {}
        for r in auth_rows:
            dec = decrypt_auth_context(r["auth_context"])
            auth_map[r["namespace"]] = list(dec.keys()) if isinstance(dec, dict) else []
        return [
            NamespaceRecord(
                namespace=r["namespace"],
                tool_count=r["tool_count"],
                auth_context_keys=auth_map.get(r["namespace"], []),
            )
            for r in tool_rows
        ]


@router.put("/{namespace}/auth", response_model=NamespaceRecord)
async def set_namespace_auth(
    namespace: str, body: NamespaceAuthSet, _=Depends(require_scope("tools:write"))
) -> NamespaceRecord:
    async with get_conn() as conn:
        count_row = await (
            await conn.execute("SELECT COUNT(*) AS c FROM tools WHERE namespace=%s", (namespace,))
        ).fetchone()
        if not count_row or count_row["c"] == 0:
            raise HTTPException(404, "Namespace not found")
        existing_row = await (
            await conn.execute(
                "SELECT auth_context FROM namespace_auth WHERE namespace=%s FOR UPDATE",
                (namespace,),
            )
        ).fetchone()
        existing = decrypt_auth_context(existing_row["auth_context"]) if existing_row else None
        merged = {**(existing or {}), **(body.auth_context or {})}
        await conn.execute(
            """INSERT INTO namespace_auth (namespace, auth_context, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (namespace) DO UPDATE
                 SET auth_context = EXCLUDED.auth_context, updated_at = NOW()""",
            (namespace, encrypt_auth_context(merged) if merged else None),
        )
        return NamespaceRecord(
            namespace=namespace,
            tool_count=count_row["c"],
            auth_context_keys=list(merged.keys()) if merged else [],
        )


@router.delete("/{namespace}/auth", status_code=204)
async def clear_namespace_auth(namespace: str, _=Depends(require_scope("tools:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute("DELETE FROM namespace_auth WHERE namespace=%s", (namespace,))
