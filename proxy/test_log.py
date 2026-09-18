"""Self-check for the persisted record.

    docker run --rm --entrypoint python3 dev-agent-proxy /app/test_log.py

The one thing that is silent when it breaks: a restart that seeds the ring from
a file it cannot parse, leaving the UI blank with no error anywhere.
"""

import json
import os
import sys
import tempfile

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

    print("ok")


if __name__ == "__main__":
    main()
