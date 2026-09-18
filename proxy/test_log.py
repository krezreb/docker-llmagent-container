"""Self-check for the persisted record.

    docker run --rm --entrypoint python3 dev-agent-proxy /app/test_log.py

The two things that are silent when they break: a restart that seeds the ring
from a file it cannot parse, leaving the UI blank with no error anywhere, and a
purge that takes records it was not asked for.
"""

import json
import os
import sys
import tempfile
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from log import Log  # noqa: E402


def main() -> None:
    path = os.path.join(tempfile.mkdtemp(), "log.jsonl")

    first = Log(path=path)
    for i in range(3):
        first.record({"ts": f"2026-09-18T10:22:3{i}.000Z", "host": "api.anthropic.com"})
    assert len(first.ring) == 3

    # A line cut off mid-write by a kill, and a blank one.
    with open(path, "a") as fh:
        fh.write('{"ts":"2026-09-18T10:22:34.0\n\n')

    second = Log(path=path)
    assert len(second.ring) == 3, second.ring
    assert second.ring[-1]["ts"] == "2026-09-18T10:22:32.000Z"
    assert second.tail(host="anthropic", limit=2)[0]["ts"] == "2026-09-18T10:22:32.000Z"

    # The seeded instance appends rather than truncating.
    second.record({"ts": "2026-09-18T10:22:40.000Z", "host": "example.com"})
    with open(path) as fh:
        kept = [json.loads(line) for line in fh if line.strip().endswith("}")]
    assert len(kept) == 4, kept

    # No path: memory only, as before.
    assert Log().file is None

    purge(path)

    print("ok")


def purge(path: str) -> None:
    log = Log(path=path)
    rotated = path + ".1"
    with open(rotated, "w") as fh:
        fh.write(json.dumps({"ts": "2026-09-18T09:00:00.000Z", "host": "old.example"}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-18T10:22:41.000Z", "host": "new.example"}) + "\n")

    dropped = log.purge("2026-09-18T10:22:35.000Z")
    assert dropped == 3, dropped
    assert [r["ts"] for r in log.ring] == ["2026-09-18T10:22:40.000Z"]
    assert lines(path) == ["2026-09-18T10:22:40.000Z"]
    # The rotated file is pruned too: rotation can leave records in it that are
    # newer than the cutoff, and they have to survive.
    assert lines(rotated) == ["2026-09-18T10:22:41.000Z"]

    # Purging everything takes both files, and the next record reopens one.
    assert log.purge() == 1
    assert log.ring == deque()
    assert not os.path.exists(path) and not os.path.exists(rotated)
    log.record({"ts": "2026-09-18T11:00:00.000Z", "host": "after.example"})
    assert lines(path) == ["2026-09-18T11:00:00.000Z"]
    assert len(Log(path=path).ring) == 1

    # Purging an empty log is not an error.
    assert log.purge("2026-09-18T12:00:00.000Z") == 1
    assert log.purge() == 0


def lines(path: str) -> list:
    with open(path) as fh:
        return [json.loads(line)["ts"] for line in fh if line.strip()]


if __name__ == "__main__":
    main()
