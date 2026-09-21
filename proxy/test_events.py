"""Self-check for the event stream.

    docker run --rm --entrypoint python3 dev-agent-proxy /app/test_events.py

The stream carries nothing until something happens, and Tornado holds the
headers back until the first write — so without an opening comment the browser
sits on 'connecting' for a whole keepalive period and the UI reads as dead.
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import sys
import tempfile
import time

import tornado.httpclient
import tornado.web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api import Events  # noqa: E402
from log import Log  # noqa: E402


class Ctx:
    # The stream is operator-only; the agent-facing listener gets a 403.
    trusted = ipaddress.ip_network("127.0.0.0/8")

    def __init__(self) -> None:
        self.log = Log(path=os.path.join(tempfile.mkdtemp(), "log.jsonl"))
        # Two connections from one container, one from another: what the
        # header counts is containers.
        self.clients = {"a": "agent-repo", "b": "agent-repo", "c": "agent-docs"}


async def main() -> None:
    ctx = Ctx()
    app = tornado.web.Application([(r"/api/events", Events, {"ctx": ctx, "full": True})])
    server = app.listen(0, address="127.0.0.1")
    port = list(server._sockets.values())[0].getsockname()[1]

    chunks: list[bytes] = []
    started = time.monotonic()
    opened = asyncio.get_running_loop().create_future()

    def on_chunk(chunk: bytes) -> None:
        chunks.append(chunk)
        if not opened.done():
            opened.set_result(time.monotonic() - started)

    request = tornado.httpclient.HTTPRequest(
        f"http://127.0.0.1:{port}/api/events",
        streaming_callback=on_chunk,
        request_timeout=30,
    )
    fetch = asyncio.ensure_future(tornado.httpclient.AsyncHTTPClient().fetch(request))

    # The stream opens without a single event having happened: what the browser
    # waits for before it calls itself connected.
    elapsed = await asyncio.wait_for(opened, 5)
    assert elapsed < 5, elapsed
    assert chunks[0].startswith(b":"), chunks[0]

    # The count is seeded on open: connects and disconnects only arrive as
    # events, and a tab opened between two of them would show nothing.
    for _ in range(50):
        if b"event: agents" in b"".join(chunks):
            break
        await asyncio.sleep(0.1)
    assert b'event: agents\ndata: ["agent-docs", "agent-repo"]' in b"".join(chunks), chunks

    # And an event that lands afterwards still arrives.
    await asyncio.sleep(0.1)
    ctx.log.record({"ts": "2026-09-18T10:22:30.000Z", "host": "api.anthropic.com"})
    for _ in range(50):
        await asyncio.sleep(0.1)
        if b"event: request" in b"".join(chunks):
            break
    assert b"event: request" in b"".join(chunks), chunks
    assert b"api.anthropic.com" in b"".join(chunks), chunks

    # Tearing down mid-stream is what the client hitting reload does, and
    # tornado logs the closed stream; nothing here is left to go wrong.
    logging.disable(logging.CRITICAL)
    server.stop()
    fetch.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait([fetch])


if __name__ == "__main__":
    asyncio.run(main())
