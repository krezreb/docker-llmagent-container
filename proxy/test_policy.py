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


MIXED = """# a ruleset written by hand
name: Mixed
description: two styles in one file
enabled: true
rules:
  # the block form, which is what the shipped defaults use
  - match: a.example.com
    action: allow
    note: first
  - match: b.example.com
    action: deny
    note: second
  # the flow form, which is what a saved rule looks like
  - {match: c.example.com, action: allow, note: third}
"""


def test_a_disabled_rule_is_inert():
    p = Policy(state({"r.yml": """
name: r
rules:
  - {match: evil.example.com, action: deny, note: off for now, enabled: false}
  - {match: "*", action: allow, note: everything}
"""}))
    # The deny would win on order; disabled, it does not run at all.
    assert p.decide("evil.example.com", "/")[0] == "allow"
    assert p.snapshot()["effective"]["deny"] == []


def test_toggling_rules_keeps_both_styles_and_the_comments():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    assert [r.enabled for r in p.rulesets["m.yml"].rules] == [True, True, True]

    # A drag across the first and third: one call, one write.
    p.set_rules_enabled("m.yml", [0, 2], False)

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text
    assert "# the flow form" in text, text

    fresh = Policy(directory)
    assert [r.enabled for r in fresh.rulesets["m.yml"].rules] == [False, True, False]
    assert [r.match for r in fresh.rulesets["m.yml"].rules] == [
        "a.example.com", "b.example.com", "c.example.com"]
    assert fresh.decide("a.example.com", "/")[0] == "pending"
    assert fresh.decide("b.example.com", "/")[0] == "deny"

    # ...and back on again, without a second `enabled:` appearing anywhere.
    fresh.set_rules_enabled("m.yml", [0, 1, 2], True)
    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert text.count("enabled: true") == 4, text  # the ruleset's, plus three rules
    assert [r.enabled for r in Policy(directory).rulesets["m.yml"].rules] == [True] * 3


def test_set_rule_action():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    assert [r.action for r in p.rulesets["m.yml"].rules] == ["allow", "deny", "allow"]

    p.set_rule_field("m.yml", 0, "action", "deny")    # block style
    p.set_rule_field("m.yml", 2, "action", "deny")    # flow style
    p.set_rule_field("m.yml", 1, "action", "allow")

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text

    fresh = Policy(directory).rulesets["m.yml"]
    assert [r.action for r in fresh.rules] == ["deny", "allow", "deny"]
    assert [r.match for r in fresh.rules] == [
        "a.example.com", "b.example.com", "c.example.com"]
    assert [r.note for r in fresh.rules] == ["first", "second", "third"]

    # ...and the swap is what decides, not the order it used to have.
    assert Policy(directory).decide("a.example.com", "/")[0] == "deny"
    assert Policy(directory).decide("b.example.com", "/")[0] == "allow"

    for bad in ("maybe", "", None):
        try:
            p.set_rule_field("m.yml", 0, "action", bad)
            assert False, f"accepted {bad!r}"
        except ValueError:
            pass


def test_set_rule_note():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)

    p.set_rule_field("m.yml", 0, "note", "the block one, renamed")     # block style
    p.set_rule_field("m.yml", 2, "note", "the flow one, renamed")      # flow style
    # A note with the punctuation that would end a flow mapping.
    p.set_rule_field("m.yml", 1, "note", "commas, and a {brace}: kept")

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text
    assert "# the flow form" in text, text

    fresh = Policy(directory).rulesets["m.yml"]
    assert [r.note for r in fresh.rules] == [
        "the block one, renamed", "commas, and a {brace}: kept", "the flow one, renamed"]
    assert [r.match for r in fresh.rules] == [
        "a.example.com", "b.example.com", "c.example.com"]
    assert [r.action for r in fresh.rules] == ["allow", "deny", "allow"]

    # A note edit and a toggle do not tread on each other.
    p.set_rules_enabled("m.yml", [2], False)
    p.set_rule_field("m.yml", 2, "note", "off, and renamed again")
    again = Policy(directory).rulesets["m.yml"].rules[2]
    assert (again.note, again.enabled, again.match) == (
        "off, and renamed again", False, "c.example.com")

    for bad in (-1, 3, 99):
        try:
            p.set_rule_field("m.yml", bad, "note", "nowhere")
            assert False, f"accepted index {bad}"
        except IndexError:
            pass


def test_set_rule_match_and_path():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)

    p.set_rule_field("m.yml", 0, "match", "*.example.com")   # block style
    p.set_rule_field("m.yml", 0, "path", "/api/*")
    p.set_rule_field("m.yml", 2, "path", "/flow/*")          # flow style

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text

    fresh = Policy(directory).rulesets["m.yml"]
    assert [r.match for r in fresh.rules] == [
        "*.example.com", "b.example.com", "c.example.com"]
    assert [r.path for r in fresh.rules] == ["/api/*", None, "/flow/*"]
    assert fresh.rules[0].note == "first"

    # An emptied path goes back to matching the whole host, rather than
    # matching on an empty string, in either style.
    p.set_rule_field("m.yml", 0, "path", "")      # block style
    p.set_rule_field("m.yml", 2, "path", "")      # flow style
    assert [r.path for r in Policy(directory).rulesets["m.yml"].rules] == [None] * 3
    assert Policy(directory).rulesets["m.yml"].rules[2].match == "c.example.com"
    assert Policy(directory).decide("www.example.com", "/anything")[0] == "allow"

    # A rule with nothing to match is refused, as is a field that is not one.
    for key, bad in (("match", ""), ("match", "   "), ("nonsense", "x")):
        try:
            p.set_rule_field("m.yml", 0, key, bad)
            assert False, f"accepted {key}={bad!r}"
        except ValueError:
            pass


def test_set_description():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    p.set_description("m.yml", "  what it is for now  ")
    assert p.rulesets["m.yml"].description == "what it is for now"

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text
    assert Policy(directory).rulesets["m.yml"].description == "what it is for now"

    # A description with YAML in it stays a description.
    p.set_description("m.yml", "rules: everything, {and more}")
    assert Policy(directory).rulesets["m.yml"].description == "rules: everything, {and more}"
    assert len(Policy(directory).rulesets["m.yml"].rules) == 3


def test_rename_keeps_the_file_and_the_comments():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    p.set_name("m.yml", "Corporate Hosts")

    assert p.rulesets["m.yml"].name == "Corporate Hosts"
    assert os.path.exists(os.path.join(directory, "rulesets", "m.yml"))  # file stays put

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text

    fresh = Policy(directory)
    assert fresh.rulesets["m.yml"].name == "Corporate Hosts"
    assert len(fresh.rulesets["m.yml"].rules) == 3

    for bad in ("", "   ", None):
        try:
            p.set_name("m.yml", bad)
            assert False, f"accepted {bad!r}"
        except ValueError:
            pass
    assert Policy(directory).rulesets["m.yml"].name == "Corporate Hosts"


def test_create_ruleset():
    directory = state({"r.yml": DENY_ONE})
    p = Policy(directory)

    filename = p.create_ruleset("Corporate Hosts")
    assert filename == "corporate-hosts.yml"
    assert p.rulesets[filename].name == "Corporate Hosts"
    assert p.rulesets[filename].enabled is True
    assert p.rulesets[filename].rules == []

    # The first rule appends as a line, so `rules:` has to start bare.
    p.append_rule(filename, Rule(match="intranet.example.com", action="deny", note="ours"))
    assert Policy(directory).decide("intranet.example.com", "/")[0] == "deny"

    for bad in ("", "   ", "///", "!!!"):
        try:
            p.create_ruleset(bad)
            assert False, f"accepted {bad!r}"
        except ValueError:
            pass

    # A name that slugs onto an existing file is refused rather than merged.
    try:
        p.create_ruleset("corporate hosts")
        assert False, "accepted a duplicate"
    except ValueError:
        pass

    # The filename never comes from the input itself.
    assert p.create_ruleset("../../etc/passwd") == "etc-passwd.yml"
    assert os.path.exists(os.path.join(directory, "rulesets", "etc-passwd.yml"))


def test_append_rules_in_one_write():
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    p.append_rules("m.yml", [
        Rule(match="one.example.com", action="allow", note="first draft"),
        Rule(match="*.two.example.com", action="deny", note="second, with a comma"),
    ])

    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text

    fresh = Policy(directory)
    assert len(fresh.rulesets["m.yml"].rules) == 5
    assert fresh.decide("one.example.com", "/")[0] == "allow"
    assert fresh.decide("x.two.example.com", "/")[0] == "deny"
    assert fresh.rulesets["m.yml"].rules[4].note == "second, with a comma"

    # A draft with no host, or an action nobody defined, is refused whole:
    # nothing of the batch lands.
    for bad in ([Rule(match="  ", action="allow")],
                [Rule(match="ok.example.com", action="maybe")],
                [Rule(match="ok.example.com", action="allow"), Rule(match="", action="deny")]):
        try:
            p.append_rules("m.yml", bad)
            assert False, f"accepted {bad}"
        except ValueError:
            pass
    assert len(Policy(directory).rulesets["m.yml"].rules) == 5

    assert p.append_rules("m.yml", []) is None  # nothing to do, nothing written


def test_a_long_rule_stays_on_one_line():
    # Long enough that safe_dump would wrap it at its default width, which
    # would take the rule out of reach of the line edits below.
    directory = state({"m.yml": MIXED})
    p = Policy(directory)
    p.append_rules("m.yml", [Rule(
        match="registry.npmjs.org", action="deny", path="/@some-rather-long-scope/*",
        note="never fetch the private scope from the public registry, not once",
    )])

    lines = [l for l in open(os.path.join(directory, "rulesets", "m.yml")) if "npmjs" in l]
    assert len(lines) == 1, lines
    assert lines[0].rstrip().endswith("}"), lines

    # ...so toggling and retitling it still edit lines rather than falling back
    # to the rewrite that would drop the comments.
    p.set_rules_enabled("m.yml", [3], False)
    p.set_rule_field("m.yml", 3, "note", "off for now")
    text = open(os.path.join(directory, "rulesets", "m.yml")).read()
    assert "# a ruleset written by hand" in text, text

    rule = Policy(directory).rulesets["m.yml"].rules[3]
    assert (rule.enabled, rule.note, rule.path) == (
        False, "off for now", "/@some-rather-long-scope/*")


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


OFF_ALLOW = """
name: Sleeping
enabled: true
rules:
  - {match: pypi.org, action: allow, note: packages, enabled: false}
"""


def test_a_pending_ask_names_the_disabled_allow_rule_and_can_switch_it_on():
    directory = state({"r.yml": OFF_ALLOW})
    p = Policy(directory)

    async def run():
        ask = asyncio.create_task(
            p.pending.ask("GET", "pypi.org", "/simple/",
                          "https://pypi.org/simple/", "os-pkg", "c1")
        )
        await asyncio.sleep(0.05)
        entry = list(p.pending.entries.values())[0]
        assert entry.as_json()["disabled_allow"] == {
            "file": "r.yml", "ruleset": "Sleeping", "index": 0, "rule": "packages",
            "ruleset_off": False, "rule_off": True,
        }, entry.as_json()["disabled_allow"]
        p.pending.resolve(entry.key, "allow", enable=True)
        assert await ask == ("allow", "pending:allow")

    asyncio.run(run())
    # Switched on rather than duplicated: still one rule, and it decides now.
    fresh = Policy(directory)
    assert len(fresh.rulesets["r.yml"].rules) == 1
    assert fresh.decide("pypi.org", "/simple/")[0] == "allow"


def test_a_disabled_ruleset_is_reported_and_switched_on_too():
    directory = state({"r.yml": ALLOW_ALL.replace("name: a", "name: a\nenabled: false")})
    p = Policy(directory)

    async def run():
        ask = asyncio.create_task(
            p.pending.ask("GET", "example.com", "/", "https://example.com/", "x", "c1")
        )
        await asyncio.sleep(0.05)
        entry = list(p.pending.entries.values())[0]
        spot = entry.as_json()["disabled_allow"]
        assert (spot["ruleset_off"], spot["rule_off"]) == (True, False), spot
        p.pending.resolve(entry.key, "allow", enable=True)
        await ask

    asyncio.run(run())
    assert Policy(directory).decide("example.com", "/")[0] == "allow"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\n{len(tests)} passed")
