"""Shared httpx.AsyncClient singleton — one per event loop."""

import asyncio

import httpx

_http_client: httpx.AsyncClient | None = None
_http_client_loop: asyncio.AbstractEventLoop | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client, _http_client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _http_client is None or _http_client_loop is not loop:
        _http_client = httpx.AsyncClient()
        _http_client_loop = loop
    return _http_client
