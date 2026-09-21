"""Self-check for the event stream.

    docker run --rm --entrypoint python3 dev-agent-proxy /app/test_events.py

The stream carries nothing until something happens, and Tornado holds the
headers back until the first write — so without an opening comment the browser
sits on 'connecting' for a whole keepalive period and the UI reads as dead.
"""

import asyncio
import contextlib
import json
import ipaddress
import logging
import os
import sys
import tempfile
import time

import tornado.httpclient
import tornado.web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
from api import Events, Heartbeat, Mode  # noqa: E402
from log import Log  # noqa: E402


class Ctx:
    # The stream is operator-only; the agent-facing listener gets a 403.
    trusted = ipaddress.ip_network("127.0.0.0/8")

    def __init__(self) -> None:
        self.log = Log(path=os.path.join(tempfile.mkdtemp(), "log.jsonl"))
        # Two connections from one container, one from another: what the
        # header counts is containers.
        self.clients = {"a": "agent-repo", "b": "agent-repo", "c": "agent-docs"}
        self.agents: dict[str, float] = {}  # last heartbeat, by container name
        self.roster: list[str] = []


def roster(chunks: list[bytes]) -> list[str]:
    """The agent list the last `agents` event carried."""
    blocks = b"".join(chunks).decode().split("\n\n")
    sent = [b for b in blocks if b.startswith("event: agents")]
    return json.loads(sent[-1].split("data: ")[1]) if sent else []


async def main() -> None:
    ctx = Ctx()
    app = tornado.web.Application(
        [(r"/api/events", Events, {"ctx": ctx, "full": True})]
    )
    server = app.listen(0, address="127.0.0.1")
    port = list(server._sockets.values())[0].getsockname()[1]

    # The second listener is the one agent containers reach: read-only, but
    # for the heartbeat.
    agent_app = tornado.web.Application(
        [
            (r"/api/heartbeat", Heartbeat, {"ctx": ctx, "full": False}),
            (r"/api/mode", Mode, {"ctx": ctx, "full": False}),
        ]
    )
    agent_server = agent_app.listen(0, address="127.0.0.1")
    agent_port = list(agent_server._sockets.values())[0].getsockname()[1]

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

    # A heartbeat puts its sender on the roster, and every open UI is told
    # without having asked. Shortened here; a real agent beats once a minute.
    api.STALE, api.SWEEP = 0.5, 0.1
    sweeper = asyncio.ensure_future(api.sweep(ctx))
    client = tornado.httpclient.AsyncHTTPClient()
    reply = await client.fetch(
        f"http://127.0.0.1:{agent_port}/api/heartbeat", method="POST", body=b""
    )
    beating = json.loads(reply.body)["agent"]
    for _ in range(50):
        await asyncio.sleep(0.1)
        if beating in roster(chunks):
            break
    assert beating in roster(chunks), chunks

    # And it leaves once the beats stop: a container is killed rather than
    # asked to leave, so silence is the only thing that can remove it.
    for _ in range(50):
        await asyncio.sleep(0.1)
        if beating not in roster(chunks):
            break
    assert roster(chunks) == ["agent-docs", "agent-repo"], chunks

    # The heartbeat is the only write that listener takes.
    try:
        await client.fetch(
            f"http://127.0.0.1:{agent_port}/api/mode", method="PUT",
            body=b'{"mode": "default-deny"}',
        )
        raise AssertionError("the agent-facing listener accepted a mode change")
    except tornado.httpclient.HTTPClientError as exc:
        assert exc.code == 403, exc

    sweeper.cancel()

    # Tearing down mid-stream is what the client hitting reload does, and
    # tornado logs the closed stream; nothing here is left to go wrong.
    logging.disable(logging.CRITICAL)
    server.stop()
    agent_server.stop()
    fetch.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait([fetch])


if __name__ == "__main__":
    asyncio.run(main())
