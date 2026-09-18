"""Modes, rulesets, categories and the pending queue — SPEC sections 8 and 9.

Everything here is plain objects shared by the addon and the API server; they
run in one process on one asyncio loop, so there is no locking and no IPC.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field

import yaml

MODES = ("lockdown", "default-deny", "default-allow", "log-only")

DEFAULT_SETTINGS = {"mode": "default-deny", "pending_timeout": 30}


def match_host(pattern: str, host: str) -> bool:
    """A glob against the hostname, or a regular expression written as /.../."""
    if len(pattern) > 2 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], host, re.IGNORECASE) is not None
    return fnmatch.fnmatchcase(host.lower(), pattern.lower())


def match_path(pattern: str, path: str | None) -> bool:
    # A rule carrying a path cannot match a request whose path is unknown —
    # a tunnelled connection, or a CONNECT. SPEC section 8.2.
    if path is None:
        return False
    return fnmatch.fnmatchcase(path, pattern)


@dataclass
class Rule:
    match: str
    action: str
    path: str | None = None
    note: str = ""
    enabled: bool = True

    def matches(self, host: str, path: str | None) -> bool:
        if not match_host(self.match, host):
            return False
        return self.path is None or match_path(self.path, path)


@dataclass
class Ruleset:
    name: str
    filename: str
    description: str = ""
    enabled: bool = True
    rules: list[Rule] = field(default_factory=list)


class Policy:
    """Rulesets, categories and settings, reloaded when their mtime changes."""

    def __init__(self, state_dir: str, emit=None):
        self.state = state_dir
        self.rulesets_dir = os.path.join(state_dir, "rulesets")
        self.categories_path = os.path.join(state_dir, "categories.yml")
        self.settings_path = os.path.join(state_dir, "settings.json")

        self.rulesets: dict[str, Ruleset] = {}
        self.categories: list[tuple[str, str]] = []
        self.settings: dict = dict(DEFAULT_SETTINGS)

        self._mtimes: dict[str, float] = {}
        self.pending = PendingQueue(self, emit)

        self.reload(force=True)

    # -- loading ---------------------------------------------------------

    def _stamp(self) -> dict[str, float]:
        stamp = {}
        for path in (self.categories_path, self.settings_path):
            stamp[path] = os.path.getmtime(path) if os.path.exists(path) else 0.0
        if os.path.isdir(self.rulesets_dir):
            for name in os.listdir(self.rulesets_dir):
                if name.endswith((".yml", ".yaml")):
                    full = os.path.join(self.rulesets_dir, name)
                    stamp[full] = os.path.getmtime(full)
        return stamp

    def reload(self, force: bool = False) -> bool:
        """Re-read anything whose mtime moved. Returns True if something did."""
        stamp = self._stamp()
        if not force and stamp == self._mtimes:
            return False
        self._mtimes = stamp

        rulesets: dict[str, Ruleset] = {}
        for path in sorted(p for p in stamp if p.startswith(self.rulesets_dir + os.sep)):
            try:
                doc = yaml.safe_load(open(path)) or {}
            except Exception as exc:  # a half-saved edit must not empty the policy
                print_stderr(f"ruleset {path}: {exc}")
                old = self.rulesets.get(os.path.basename(path))
                if old:
                    rulesets[old.filename] = old
                continue
            filename = os.path.basename(path)
            rulesets[filename] = Ruleset(
                name=doc.get("name") or os.path.splitext(filename)[0],
                filename=filename,
                description=doc.get("description", ""),
                enabled=bool(doc.get("enabled", True)),
                rules=[
                    Rule(
                        match=str(r["match"]),
                        action=r.get("action", "deny"),
                        path=r.get("path"),
                        note=r.get("note", ""),
                        enabled=bool(r.get("enabled", True)),
                    )
                    for r in doc.get("rules") or []
                    if r.get("match")
                ],
            )
        self.rulesets = rulesets

        cats = []
        if os.path.exists(self.categories_path):
            doc = yaml.safe_load(open(self.categories_path)) or {}
            for entry in doc.get("categories") or []:
                if entry.get("pattern"):
                    cats.append((str(entry["pattern"]), entry.get("category", "unknown")))
        self.categories = cats

        settings = dict(DEFAULT_SETTINGS)
        if os.path.exists(self.settings_path):
            try:
                settings.update(json.load(open(self.settings_path)))
            except Exception as exc:
                print_stderr(f"settings.json: {exc}")
        if settings.get("mode") not in MODES:
            settings["mode"] = DEFAULT_SETTINGS["mode"]
        self.settings = settings
        return True

    # -- settings --------------------------------------------------------

    @property
    def mode(self) -> str:
        return self.settings["mode"]

    @property
    def timeout(self) -> float:
        return float(self.settings.get("pending_timeout") or 30)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        self.settings["mode"] = mode
        self._write_settings()

    def _write_settings(self) -> None:
        _atomic_write(self.settings_path, json.dumps(self.settings, indent=2))
        self._mtimes = self._stamp()

    def set_enabled(self, filename: str, enabled: bool) -> None:
        # A line edit rather than a YAML round trip. These files are meant to
        # be hand-edited and kept in git, and safe_dump would silently drop
        # every comment in one the moment somebody flicked a toggle.
        ruleset = self.rulesets[filename]
        self._set_key(filename, "enabled", "true" if enabled else "false",
                      lambda doc: bool(doc.get("enabled", True)) is bool(enabled))
        ruleset.enabled = enabled
        self._mtimes = self._stamp()

    def set_name(self, filename: str, name: str) -> None:
        """Rename the ruleset, not the file.

        The filename is what a saved rule, an error message and the UI's
        picker all refer to, so it stays put; `name` is only what the operator
        calls it.
        """
        ruleset = self.rulesets[filename]
        name = (name or "").strip()
        if not name:
            raise ValueError("a ruleset needs a name")
        self._set_key(filename, "name", yaml.safe_dump(name, default_style='"').strip().rstrip("."),
                      lambda doc: (doc.get("name") or "") == name)
        ruleset.name = name
        self._mtimes = self._stamp()

    def set_description(self, filename: str, description: str) -> None:
        ruleset = self.rulesets[filename]
        description = (description or "").strip()
        self._set_key(filename, "description",
                      yaml.safe_dump(description, default_style='"').strip().rstrip("."),
                      lambda doc: (doc.get("description") or "") == description)
        ruleset.description = description
        self._mtimes = self._stamp()

    def _set_key(self, filename: str, key: str, raw: str, ok) -> None:
        """Replace one top-level scalar, or add it, without touching the rest."""
        path = os.path.join(self.rulesets_dir, filename)
        text = open(path).read()
        edited, count = re.subn(
            rf"^{key}:.*$", f"{key}: {raw}", text, count=1, flags=re.MULTILINE
        )
        if count == 0:
            edited = f"{key}: {raw}\n{text}"

        if not _write_checked(path, edited, ok):
            doc = yaml.safe_load(text) or {}
            doc[key] = yaml.safe_load(raw)
            _write_yaml(path, doc)

    def set_rules_enabled(self, filename: str, indexes: list[int], enabled: bool) -> None:
        """Flip `enabled` on some rules of one ruleset, comments intact.

        One call for a whole drag: the UI paints across a run of rules and
        commits once, so the file is written once and reloaded once.
        """
        path = os.path.join(self.rulesets_dir, filename)
        text = open(path).read()
        lines = text.split("\n")
        spans = _rule_spans(lines)

        indexes = sorted(set(indexes))
        if not indexes or indexes[-1] >= len(spans):
            raise IndexError(f"{filename} has {len(spans)} rules")

        # From the bottom, so an insertion never moves a span still to come.
        for index in reversed(indexes):
            _set_item_enabled(lines, *spans[index], enabled)

        def ok(doc):
            rules = doc.get("rules") or []
            return all(bool(rules[i].get("enabled", True)) is bool(enabled) for i in indexes)

        if not _write_checked(path, "\n".join(lines), ok):
            doc = yaml.safe_load(text) or {}
            for index in indexes:
                doc["rules"][index]["enabled"] = enabled
            _write_yaml(path, doc)
        self.reload(force=True)

    def create_ruleset(self, name: str) -> str:
        """A new, empty, enabled ruleset. Returns the filename it was given.

        The filename comes from a slug of the name and never from the input
        directly: this is the one place the operator's text would otherwise
        reach a path.
        """
        name = (name or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not slug:
            raise ValueError("a ruleset needs a name with a letter or digit in it")

        filename = f"{slug}.yml"
        path = os.path.join(self.rulesets_dir, filename)
        if os.path.exists(path):
            raise ValueError(f"{filename} already exists")

        # `rules:` bare rather than `rules: []`, so that the first saved rule
        # can be appended as a line instead of forcing a rewrite.
        header = yaml.safe_dump(
            {"name": name, "description": "created from the UI", "enabled": True},
            sort_keys=False, default_flow_style=False,
        )
        with open(path, "w") as fh:
            fh.write(header + "rules:\n")
        self.reload(force=True)
        return filename

    def append_rule(self, filename: str, rule: Rule) -> None:
        path = os.path.join(self.rulesets_dir, filename)
        entry = {"match": rule.match, "action": rule.action}
        if rule.path:
            entry["path"] = rule.path
        if rule.note:
            entry["note"] = rule.note

        if not os.path.exists(path):
            # Rulesets are made by create_ruleset, which names and describes
            # them. Conjuring one here would leave a file with no name in it.
            raise KeyError(filename)
        text = open(path).read()
        doc = yaml.safe_load(text) or {}
        before = len(doc.get("rules") or [])

        # Appended as a line, for the same reason set_enabled edits one: the
        # comments in these files are the user's. It only lands correctly when
        # `rules:` is the last key, so the result is parsed before it is kept
        # and a file shaped otherwise falls back to the rewrite.
        indent = re.findall(r"^(\s*)- ", text, re.MULTILINE)
        line = indent[-1] if indent else "  "
        flow = yaml.safe_dump(entry, default_flow_style=True, sort_keys=False).strip()
        candidate = text + ("" if text.endswith("\n") else "\n") + f"{line}- {flow}\n"

        if not _write_checked(path, candidate,
                              lambda new: len(new.get("rules") or []) == before + 1):
            doc.setdefault("rules", []).append(entry)
            _write_yaml(path, doc)
        self.reload(force=True)

    # -- decisions -------------------------------------------------------

    def category(self, host: str) -> str:
        for pattern, cat in self.categories:
            if match_host(pattern, host):
                return cat
        return "unknown"

    def decide(self, host: str, path: str | None) -> tuple[str, str]:
        """(action, rule label). action is allow, deny, tunnel or pending."""
        self.reload()
        mode = self.mode

        if mode == "lockdown":
            return "deny", "mode:lockdown"

        # Deny first, then tunnel, then allow: a deny in any enabled ruleset
        # cannot be overridden by an allow somewhere else. SPEC section 8.2.
        for action in ("deny", "tunnel", "allow"):
            for filename in sorted(self.rulesets):
                ruleset = self.rulesets[filename]
                if not ruleset.enabled:
                    continue
                for rule in ruleset.rules:
                    if not rule.enabled:
                        continue
                    if rule.action == action and rule.matches(host, path):
                        return action, f"{filename}:{rule.note or rule.match}"

        if mode in ("default-allow", "log-only"):
            return "allow", f"mode:{mode}"
        return "pending", f"mode:{mode}"

    def snapshot(self) -> dict:
        """What GET /api/policy serves, on both ports."""
        effective = {"allow": [], "deny": [], "tunnel": []}
        for filename in sorted(self.rulesets):
            ruleset = self.rulesets[filename]
            if not ruleset.enabled:
                continue
            for rule in ruleset.rules:
                if rule.enabled and rule.action in effective:
                    effective[rule.action].append(
                        {"match": rule.match, "path": rule.path, "note": rule.note,
                         "ruleset": filename}
                    )
        return {
            "mode": self.mode,
            "pending_timeout": self.timeout,
            "rulesets": [
                {"name": r.name, "file": r.filename, "description": r.description,
                 "enabled": r.enabled, "rules": len(r.rules)}
                for r in (self.rulesets[f] for f in sorted(self.rulesets))
            ],
            "effective": effective,
        }


# -- the pending queue — SPEC section 8.3 --------------------------------


def ask_key(method: str | None, host: str, path: str | None) -> str:
    raw = f"{method}\0{host}\0{path}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class PendingEntry:
    key: str
    method: str | None
    host: str
    path: str | None
    url: str
    cat: str
    created: float
    clients: set = field(default_factory=set)
    waiters: set = field(default_factory=set)

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "method": self.method,
            "host": self.host,
            "path": self.path,
            "url": self.url,
            "cat": self.cat,
            "clients": sorted(self.clients),
            "waiters": len(self.waiters),
            "waiting_ms": int((time.time() - self.created) * 1000),
        }


class PendingQueue:
    """One entry per ask key; every request on that key shares one decision."""

    def __init__(self, policy: Policy, emit=None):
        self.policy = policy
        self.entries: dict[str, PendingEntry] = {}
        self._emit = emit or (lambda event, data: None)

    async def ask(self, method, host, path, url, cat, client) -> tuple[str, str]:
        key = ask_key(method, host, path)
        entry = self.entries.get(key)
        if entry is None:
            entry = PendingEntry(key, method, host, path, url, cat, time.time())
            self.entries[key] = entry
        entry.clients.add(client)

        waiter = asyncio.get_running_loop().create_future()
        entry.waiters.add(waiter)
        self._emit("pending", entry.as_json())

        # Per waiter, not per entry: a flow that attached late gets its own
        # full timeout, and a long-lived entry does not kill a fresh request.
        try:
            decision = await asyncio.wait_for(waiter, self.policy.timeout)
            rule = f"pending:{decision}"
        except (asyncio.TimeoutError, TimeoutError):
            decision, rule = "deny", "pending:timeout"
        finally:
            entry.waiters.discard(waiter)
            if not entry.waiters:
                self.entries.pop(key, None)
                self._emit("resolved", {"key": key})

        return decision, rule

    def resolve(self, key: str, decision: str, save_to=None, scope="host") -> int:
        entry = self.entries.get(key)
        if entry is None:
            raise KeyError(key)

        if save_to:
            path = None
            if scope == "host+path" and entry.path:
                path = entry.path.rsplit("/", 1)[0] + "/*"
            self.policy.append_rule(
                save_to,
                Rule(match=entry.host, action=decision, path=path,
                     note="saved from the UI"),
            )

        waiters = list(entry.waiters)
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(decision)
        return len(waiters)

    def as_json(self) -> list[dict]:
        return [e.as_json() for e in self.entries.values()]


def _rule_spans(lines: list[str]) -> list[tuple[int, int]]:
    """The first and last line of each item under `rules:`.

    Line arithmetic rather than a YAML round trip, because these files carry
    the user's comments and safe_dump would drop every one of them.
    """
    spans: list[list[int]] = []
    inside = False
    for number, line in enumerate(lines):
        if re.match(r"^rules:", line):
            inside = True
            continue
        if not inside:
            continue
        if re.match(r"^\s*-\s", line):
            spans.append([number, number])
        elif line.strip() and not line[:1].isspace():
            break  # the next top-level key ends the list
        elif spans:
            spans[-1][1] = number
    return [(start, end) for start, end in spans]


def _set_item_enabled(lines: list[str], start: int, end: int, enabled: bool) -> None:
    value = "true" if enabled else "false"

    flow = re.match(r"^(\s*)-\s*\{(.*)\}\s*$", lines[start])
    if flow:
        indent, inner = flow.groups()
        inner = re.sub(r",?\s*enabled:\s*\S+", "", inner).strip().rstrip(",")
        lines[start] = f"{indent}- {{{inner}, enabled: {value}}}"
        return

    for number in range(start, end + 1):
        if re.match(r"^\s*(-\s+)?enabled:", lines[number]):
            lines[number] = re.sub(r"enabled:.*", f"enabled: {value}", lines[number])
            return

    content = [n for n in range(start, end + 1)
               if lines[n].strip() and not lines[n].lstrip().startswith("#")]
    indent = re.match(r"^(\s*)", lines[start]).group(1) + "  "
    lines.insert(content[-1] + 1, f"{indent}enabled: {value}")


def _write_checked(path: str, text: str, ok) -> bool:
    """Write only if the result parses and says what it was meant to say."""
    try:
        doc = yaml.safe_load(text) or {}
    except Exception:
        return False
    if not ok(doc):
        return False
    _atomic_write(path, text)
    return True


def _write_yaml(path: str, doc: dict) -> None:
    _atomic_write(path, yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))


def _atomic_write(path: str, text: str) -> None:
    """Replace a file's contents in one step, through a temporary of its own.

    A fixed `path + ".tmp"` would be shared by two writers, and two writes that
    overlapped would interleave into one file rather than one winning; the
    mode is set because these are read by an arbitrary host uid.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".write-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def print_stderr(message: str) -> None:
    import sys

    print(f"dev-agent-proxy: {message}", file=sys.stderr, flush=True)
