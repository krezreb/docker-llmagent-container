"""The request record of SPEC section 10: JSON lines on stdout, a bounded ring
buffer for the UI to seed from, and an SSE fan-out to connected UIs.

The schema is a contract — downstream vector and Elasticsearch configurations
depend on it — so fields are added here, never renamed or repurposed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from datetime import datetime, timezone

RING = 5000
QUEUE = 1000


def now() -> str:
    """RFC 3339 UTC with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Log:
    def __init__(self, ring: int = RING):
        self.ring: deque[dict] = deque(maxlen=ring)
        self.subscribers: set[asyncio.Queue] = set()

    def record(self, rec: dict) -> None:
        self.ring.append(rec)
        print(json.dumps(rec, separators=(",", ":")), flush=True)
        self.event("request", rec)

    def event(self, kind: str, data) -> None:
        """Push to every connected UI. A subscriber that cannot keep up is
        dropped rather than allowed to grow without limit."""
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((kind, data))
            except asyncio.QueueFull:
                self.subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def tail(self, since=None, host=None, cat=None, decision=None, limit=500) -> list[dict]:
        out = []
        for rec in reversed(self.ring):
            if since and rec["ts"] <= since:
                break
            if host and host not in (rec.get("host") or ""):
                continue
            if cat and rec.get("cat") != cat:
                continue
            if decision and rec.get("decision") != decision:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


def warn(message: str) -> None:
    print(f"dev-agent-proxy: {message}", file=sys.stderr, flush=True)
