"""Self-check for the User-Agent override.

    docker run --rm --entrypoint python3 dev-agent-proxy /app/test_useragent.py

What would go wrong quietly: a sticky host that drifts to another agent, a bad
setting that half-applies, and a restart that forgets the setting.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from useragent import PRESETS, UserAgent  # noqa: E402

state = tempfile.mkdtemp()
ua = UserAgent(state)
assert ua.pick("a.com") is None, "passthrough by default"

ua.set({"mode": "single", "agents": ["one", "two"]})
assert {ua.pick("a.com") for _ in range(20)} == {"one"}

ua.set({"mode": "random", "agents": [p["ua"] for p in PRESETS], "sticky": False})
assert len({ua.pick("a.com") for _ in range(200)}) > 1, "random varies"

ua.set({"sticky": True})
first = ua.pick("a.com")
assert {ua.pick("a.com") for _ in range(200)} == {first}, "sticky per host"
assert len({ua.pick(f"h{i}.com") for i in range(200)}) > 1, "hosts differ"

for bad in ({"mode": "nope"}, {"mode": "single", "agents": []},
            {"agents": "not a list"}, {"mode": "random", "agents": ["", "\n"]}):
    try:
        ua.set(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
assert ua.mode == "random" and ua.sticky, "a refused setting leaves the old one"

ua.set({"mode": "single", "agents": ["  kept  ", "", "bad\r\nX-Injected: 1"]})
assert ua.agents == ["kept"], "blanks and line breaks dropped"

assert UserAgent(state).as_json() == ua.as_json(), "survives a restart"
assert all("\n" not in p["ua"] for p in PRESETS)
print("ok")
