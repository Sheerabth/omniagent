"""Auth management per tool namespace."""

from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import NamespaceAuthSet, NamespaceRecord, SchemeRecord
from omniagent.api.queries import (
    delete_namespace_auth,
    select_namespace_auth_by_namespace_list,
    select_namespace_auth_for_update,
    select_namespaces_with_tool_count,
    select_tool_count_by_namespace,
    upsert_namespace_auth,
)
from omniagent.crypto import decrypt_auth_context, encrypt_auth_context
from omniagent.db import get_conn

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


@router.get("", response_model=list[NamespaceRecord])
async def list_namespaces(_=Depends(require_scope("auth:read"))) -> list[NamespaceRecord]:
    async with get_conn() as conn:
        tool_rows = await (await conn.execute(select_namespaces_with_tool_count)).fetchall()
        if not tool_rows:
            return []
        namespaces = [r["namespace"] for r in tool_rows]
        auth_rows = await (
            await conn.execute(
                select_namespace_auth_by_namespace_list,
                (namespaces,),
            )
        ).fetchall()
        schemes_map: dict[str, list[SchemeRecord]] = {}
        for r in auth_rows:
            dec = decrypt_auth_context(r["auth_context"])
            schemes_map.setdefault(r["namespace"], []).append(
                SchemeRecord(
                    scheme_name=r["scheme_name"],
                    auth_context_keys=list(dec.keys()) if isinstance(dec, dict) else [],
                )
            )
        return [
            NamespaceRecord(
                namespace=r["namespace"],
                tool_count=r["tool_count"],
                schemes=schemes_map.get(r["namespace"], []),
            )
            for r in tool_rows
        ]


@router.put("/auth/{namespace}/{scheme_name}", response_model=SchemeRecord)
async def set_namespace_auth(
    namespace: str,
    scheme_name: str,
    body: NamespaceAuthSet,
    _=Depends(require_scope("auth:write")),
) -> SchemeRecord:
    async with get_conn() as conn:
        count_row = await (
            await conn.execute(select_tool_count_by_namespace, (namespace,))
        ).fetchone()
        if not count_row or count_row["c"] == 0:
            raise HTTPException(404, "Namespace not found")
        existing_row = await (
            await conn.execute(
                select_namespace_auth_for_update,
                (namespace, scheme_name),
            )
        ).fetchone()
        existing = decrypt_auth_context(existing_row["auth_context"]) if existing_row else None
        merged = {**(existing or {}), **(body.auth_context or {})}
        await conn.execute(
            upsert_namespace_auth,
            (namespace, scheme_name, encrypt_auth_context(merged) if merged else None),
        )
        return SchemeRecord(
            scheme_name=scheme_name,
            auth_context_keys=list(merged.keys()) if merged else [],
        )


@router.delete("/auth/{namespace}/{scheme_name}", status_code=204)
async def clear_namespace_auth(
    namespace: str,
    scheme_name: str,
    _=Depends(require_scope("auth:write")),
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            delete_namespace_auth,
            (namespace, scheme_name),
        )
