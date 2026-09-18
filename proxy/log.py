"""The request record of SPEC section 10: JSON lines on stdout and in
/state/log.jsonl, a bounded ring buffer for the UI to seed from, and an SSE
fan-out to connected UIs.

The schema is a contract — downstream vector and Elasticsearch configurations
depend on it — so fields are added here, never renamed or repurposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
from collections import deque
from datetime import datetime, timezone

RING = 5000
QUEUE = 1000
ROTATE = 64 * 1024 * 1024  # per file, two files kept


def now() -> str:
    """RFC 3339 UTC with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Log:
    def __init__(self, ring: int = RING, path: str | None = None):
        self.ring: deque[dict] = deque(maxlen=ring)
        self.subscribers: set[asyncio.Queue] = set()
        self.file = None
        if path:
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=ROTATE, backupCount=1
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            # Constructed, not getLogger(): the registry is global, and a
            # second Log on the same name would double every line.
            self.file = logging.Logger("dev-agent-proxy.record", logging.INFO)
            self.file.addHandler(handler)
            self._seed(path)

    def _seed(self, path: str) -> None:
        """Refill the ring from the tail of the log so a restart does not blank
        the UI. Only the current file: the rotated one is older than the ring
        would hold anyway. A line written half-way through a kill is skipped."""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - ROTATE // 8), 0)
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError as e:
            warn(f"cannot read {path}: {e}")
            return
        for line in lines[-self.ring.maxlen:]:
            try:
                self.ring.append(json.loads(line))
            except ValueError:
                continue

    def record(self, rec: dict) -> None:
        self.ring.append(rec)
        line = json.dumps(rec, separators=(",", ":"))
        print(line, flush=True)
        if self.file:
            self.file.info(line)
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
