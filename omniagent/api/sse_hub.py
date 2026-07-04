"""One shared LISTEN connection multiplexed across all open SSE streams.

Previously each /stream request opened its own raw Postgres connection and
held it for the stream's entire lifetime — every open browser tab ate one of
Postgres's max_connections just to listen, with connections now potentially
long-lived (across chained turns, long defers). This hub keeps exactly one
connection per control-plane process, dynamically LISTEN/UNLISTEN per
channel, fanning notifications out to per-stream asyncio.Queues instead.

LISTEN/UNLISTEN can't just be `execute()`d on that connection whenever a
stream subscribes/unsubscribes: psycopg's `notifies()` holds the connection's
internal lock for the generator's entire life, so a concurrent execute() on
the same connection would hang forever waiting for it. Passing `timeout=` to
notifies() makes it return (releasing the lock) after a short quiet period —
the supervisor uses that gap to drain any pending LISTEN/UNLISTEN requests
before resuming.
"""

import asyncio
import contextlib
import logging
import os

import psycopg

logger = logging.getLogger(__name__)

_conn: psycopg.AsyncConnection | None = None
_supervisor_task: asyncio.Task | None = None
_subscribers: dict[str, set[asyncio.Queue]] = {}
_lock = asyncio.Lock()
_stopped = False

# (action, channel, done) requests waiting for a gap between notifies()
# polling windows, when the connection's lock is free to execute() on.
_pending: "asyncio.Queue[tuple[str, str, asyncio.Event]]" = asyncio.Queue()

_POLL_INTERVAL = 0.5  # max added latency for subscribe()/unsubscribe()


async def start() -> None:
    global _supervisor_task, _stopped
    _stopped = False
    _supervisor_task = asyncio.create_task(_supervisor())


async def stop() -> None:
    global _supervisor_task, _stopped
    _stopped = True
    if _supervisor_task:
        _supervisor_task.cancel()
        _supervisor_task = None
    if _conn:
        await _conn.close()


async def _supervisor() -> None:
    """Own the single connection; reconnect with backoff if it ever drops.

    A dropped connection here would otherwise silently kill every open SSE
    stream at once — this is a shared resource now, unlike the old
    one-connection-per-stream design, so losing it needs to self-heal.
    """
    global _conn
    dsn = os.environ.get("DATABASE_URL", "")
    backoff = 1
    while not _stopped:
        try:
            _conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
            for channel in list(_subscribers):
                await _conn.execute(f"LISTEN {channel}")
            backoff = 1
            while not _stopped:
                async for notify in _conn.notifies(timeout=_POLL_INTERVAL):
                    for q in list(_subscribers.get(notify.channel, ())):
                        q.put_nowait(notify.payload or "update")
                # notifies() returns after _POLL_INTERVAL of silence, which
                # releases the connection's lock -- safe to execute() here.
                while not _pending.empty():
                    action, channel, done = _pending.get_nowait()
                    try:
                        await _conn.execute(f"{action} {channel}")
                    finally:
                        done.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sse_hub: listener connection lost, reconnecting in %ds", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _listen_action(action: str, channel: str) -> None:
    done = asyncio.Event()
    await _pending.put((action, channel, done))
    await done.wait()


async def subscribe(channel: str) -> asyncio.Queue:
    async with _lock:
        is_new = channel not in _subscribers
        _subscribers.setdefault(channel, set())
        q: asyncio.Queue = asyncio.Queue()
        _subscribers[channel].add(q)
    if is_new and _conn is not None:
        await _listen_action("LISTEN", channel)
    return q


async def unsubscribe(channel: str, q: asyncio.Queue) -> None:
    async with _lock:
        subs = _subscribers.get(channel)
        if not subs:
            return
        subs.discard(q)
        now_empty = not subs
        if now_empty:
            del _subscribers[channel]
    if now_empty and _conn is not None:
        with contextlib.suppress(Exception):
            await _listen_action("UNLISTEN", channel)
