"""Self-check for the evaluation order and the pending queue.

    docker run --rm dev-agent-proxy python3 /app/test_policy.py

Those are the two pieces where being wrong is silent: a rule that matches when
it should not opens a host nobody allowed, and a pending queue that asks once
per request is unusable the first time an `npm install` hits it.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from policy import Policy, Rule  # noqa: E402


def state(rulesets: dict, mode="default-deny", timeout=30) -> str:
    directory = tempfile.mkdtemp()
    os.makedirs(os.path.join(directory, "rulesets"))
    for name, body in rulesets.items():
        with open(os.path.join(directory, "rulesets", name), "w") as fh:
            fh.write(body)
    with open(os.path.join(directory, "categories.yml"), "w") as fh:
        fh.write(
            "categories:\n"
            "  - {pattern: 'api.anthropic.com', category: llm-api}\n"
            "  - {pattern: '*.ubuntu.com', category: os-pkg}\n"
        )
    with open(os.path.join(directory, "settings.json"), "w") as fh:
        fh.write('{"mode": "%s", "pending_timeout": %s}' % (mode, timeout))
    return directory


ALLOW_ALL = """
name: a
rules:
  - {match: "*", action: allow, note: everything}
"""

DENY_ONE = """
name: b
rules:
  - {match: evil.example.com, action: deny, note: nope}
"""


def test_deny_beats_allow_across_rulesets():
    p = Policy(state({"a-allow.yml": ALLOW_ALL, "b-deny.yml": DENY_ONE}))
    assert p.decide("evil.example.com", "/")[0] == "deny"
    assert p.decide("good.example.com", "/")[0] == "allow"


def test_glob_and_regex():
    p = Policy(
        state(
            {
                "r.yml": """
name: r
rules:
  - {match: "*.ubuntu.com", action: allow, note: apt}
  - {match: "/^(a|b)\\\\.example\\\\.com$/", action: allow, note: regex}
"""
            }
        )
    )
    assert p.decide("archive.ubuntu.com", "/")[0] == "allow"
    # A glob for subdomains is not a glob for the bare domain.
    assert p.decide("ubuntu.com", "/")[0] == "pending"
    assert p.decide("ARCHIVE.UBUNTU.COM", "/")[0] == "allow"
    assert p.decide("a.example.com", "/")[0] == "allow"
    assert p.decide("c.example.com", "/")[0] == "pending"


def test_path_rules():
    p = Policy(
        state(
            {
                "r.yml": """
name: r
rules:
  - {match: registry.npmjs.org, path: "/@internal/*", action: deny, note: private scope}
  - {match: registry.npmjs.org, action: allow, note: npm}
"""
            }
        )
    )
    assert p.decide("registry.npmjs.org", "/@internal/thing")[0] == "deny"
    assert p.decide("registry.npmjs.org", "/lodash")[0] == "allow"
    # A rule carrying a path cannot decide a connection whose path is unknown.
    assert p.decide("registry.npmjs.org", None)[0] == "allow"


def test_modes():
    files = {"r.yml": DENY_ONE}
    assert Policy(state(files, mode="lockdown")).decide("github.com", "/")[0] == "deny"
    assert Policy(state(files, mode="default-deny")).decide("github.com", "/")[0] == "pending"
    assert Policy(state(files, mode="default-allow")).decide("github.com", "/")[0] == "allow"
    assert Policy(state(files, mode="log-only")).decide("github.com", "/")[0] == "allow"
    # A deny rule still decides under the permissive modes.
    assert Policy(state(files, mode="default-allow")).decide("evil.example.com", "/")[0] == "deny"
    # ...and lockdown short-circuits before any rule, including an allow.
    assert Policy(state({"a.yml": ALLOW_ALL}, mode="lockdown")).decide("x.com", "/")[0] == "deny"


def test_disabled_ruleset_is_inert():
    disabled = "name: d\nenabled: false\n" + ALLOW_ALL.split("\n", 2)[2]
    p = Policy(state({"d.yml": disabled}))
    assert p.decide("anything.example.com", "/")[0] == "pending"


def test_categories():
    p = Policy(state({"r.yml": ALLOW_ALL}))
    assert p.category("api.anthropic.com") == "llm-api"
    assert p.category("archive.ubuntu.com") == "os-pkg"
    assert p.category("example.com") == "unknown"


def test_reload_on_mtime():
    directory = state({"r.yml": DENY_ONE})
    p = Policy(directory)
    assert p.decide("late.example.com", "/")[0] == "pending"
    with open(os.path.join(directory, "rulesets", "r.yml"), "w") as fh:
        fh.write("name: r\nrules:\n  - {match: late.example.com, action: allow, note: added}\n")
    os.utime(os.path.join(directory, "rulesets", "r.yml"), (0, 0))  # any change of mtime
    assert p.decide("late.example.com", "/")[0] == "allow"


def test_pending_coalesces_and_resolves_together():
    async def run():
        p = Policy(state({"r.yml": DENY_ONE}))
        asks = [
            asyncio.create_task(
                p.pending.ask("GET", "registry.npmjs.org", "/lodash",
                              "https://registry.npmjs.org/lodash", "lang-pkg", "c1")
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0.05)

        # Five requests, one question.
        assert len(p.pending.entries) == 1, p.pending.entries
        entry = list(p.pending.entries.values())[0]
        assert entry.as_json()["waiters"] == 5

        # A different path is a different question, since a rule can be saved
        # at host+path scope.
        other = asyncio.create_task(
            p.pending.ask("GET", "registry.npmjs.org", "/express",
                          "https://registry.npmjs.org/express", "lang-pkg", "c1")
        )
        await asyncio.sleep(0.05)
        assert len(p.pending.entries) == 2

        assert p.pending.resolve(entry.key, "allow") == 5
        assert [await a for a in asks] == [("allow", "pending:allow")] * 5
        assert len(p.pending.entries) == 1  # the entry is gone with its last waiter

        other.cancel()

    asyncio.run(run())


def test_pending_times_out_per_waiter():
    async def run():
        p = Policy(state({"r.yml": DENY_ONE}, timeout=0.3))
        early = asyncio.create_task(
            p.pending.ask("GET", "x.example.com", "/", "https://x.example.com/", "unknown", "c1")
        )
        await asyncio.sleep(0.2)
        late = asyncio.create_task(
            p.pending.ask("GET", "x.example.com", "/", "https://x.example.com/", "unknown", "c2")
        )

        assert await early == ("deny", "pending:timeout")
        # The late waiter got its own full timeout rather than the entry's.
        assert not late.done()
        assert await late == ("deny", "pending:timeout")
        assert p.pending.entries == {}

    asyncio.run(run())


def test_saved_rule_scope():
    directory = state({"r.yml": DENY_ONE})
    p = Policy(directory)

    async def run():
        ask = asyncio.create_task(
            p.pending.ask("GET", "s3.example.com", "/bucket/key",
                          "https://s3.example.com/bucket/key", "cloud", "c1")
        )
        await asyncio.sleep(0.05)
        key = list(p.pending.entries)[0]
        p.pending.resolve(key, "allow", save_to="r.yml", scope="host+path")
        await ask

    asyncio.run(run())
    assert p.decide("s3.example.com", "/bucket/other")[0] == "allow"
    assert p.decide("s3.example.com", "/elsewhere/key")[0] == "pending"


def test_a_saved_rule_carries_the_decision_that_was_made():
    directory = state({"r.yml": DENY_ONE})
    p = Policy(directory)

    async def run():
        ask = asyncio.create_task(
            p.pending.ask("GET", "tracker.example.com", "/beacon",
                          "https://tracker.example.com/beacon", "telemetry", "c1")
        )
        await asyncio.sleep(0.05)
        key = list(p.pending.entries)[0]
        p.pending.resolve(key, "deny", save_to="r.yml", scope="host")
        assert await ask == ("deny", "pending:deny")

    asyncio.run(run())
    # Saved as a deny, so it decides on its own next time rather than asking.
    action, rule = Policy(directory).decide("tracker.example.com", "/anything")
    assert (action, rule) == ("deny", "r.yml:saved from the UI"), (action, rule)


def test_append_rule_keeps_the_file_loadable():
    directory = state({"r.yml": DENY_ONE})
    p = Policy(directory)
    p.append_rule("r.yml", Rule(match="new.example.com", action="allow", note="by hand"))
    assert Policy(directory).decide("new.example.com", "/")[0] == "allow"
    assert Policy(directory).decide("evil.example.com", "/")[0] == "deny"  # original kept


def test_writes_keep_the_comments():
    commented = """# the top of the file, which the user wrote
name: r
enabled: true
rules:
  # why this one is here
  - {match: evil.example.com, action: deny, note: nope}
"""
    directory = state({"r.yml": commented})
    p = Policy(directory)
    p.set_enabled("r.yml", False)
    p.append_rule("r.yml", Rule(match="new.example.com", action="allow", note="added"))

    text = open(os.path.join(directory, "rulesets", "r.yml")).read()
    assert "# the top of the file, which the user wrote" in text, text
    assert "# why this one is here" in text, text

    fresh = Policy(directory)
    assert fresh.rulesets["r.yml"].enabled is False
    assert len(fresh.rulesets["r.yml"].rules) == 2


def test_append_falls_back_when_rules_is_not_last():
    # A line appended to the end of this file would land under `description`,
    # so the write has to notice and rewrite instead.
    awkward = """name: r
rules:
  - {match: evil.example.com, action: deny, note: nope}
description: rules are not the last key here
"""
    directory = state({"r.yml": awkward})
    p = Policy(directory)
    p.append_rule("r.yml", Rule(match="new.example.com", action="allow", note="added"))

    fresh = Policy(directory)
    assert len(fresh.rulesets["r.yml"].rules) == 2
    assert fresh.decide("new.example.com", "/")[0] == "allow"
    assert fresh.rulesets["r.yml"].description == "rules are not the last key here"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\n{len(tests)} passed")
