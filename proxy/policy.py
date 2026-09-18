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
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.settings, fh, indent=2)
        os.replace(tmp, self.settings_path)
        self._mtimes = self._stamp()

    def set_enabled(self, filename: str, enabled: bool) -> None:
        # A line edit rather than a YAML round trip. These files are meant to
        # be hand-edited and kept in git, and safe_dump would silently drop
        # every comment in one the moment somebody flicked a toggle.
        ruleset = self.rulesets[filename]
        path = os.path.join(self.rulesets_dir, filename)
        text = open(path).read()
        value = "true" if enabled else "false"
        edited, count = re.subn(
            r"^enabled:.*$", f"enabled: {value}", text, count=1, flags=re.MULTILINE
        )
        if count == 0:
            edited = f"enabled: {value}\n{text}"

        if not _write_checked(path, edited, lambda doc: bool(doc.get("enabled", True)) is bool(enabled)):
            doc = yaml.safe_load(text) or {}
            doc["enabled"] = enabled
            _write_yaml(path, doc)
        ruleset.enabled = enabled
        self._mtimes = self._stamp()

    def append_rule(self, filename: str, rule: Rule) -> None:
        path = os.path.join(self.rulesets_dir, filename)
        entry = {"match": rule.match, "action": rule.action}
        if rule.path:
            entry["path"] = rule.path
        if rule.note:
            entry["note"] = rule.note

        text = open(path).read() if os.path.exists(path) else "rules:\n"
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
                if rule.action in effective:
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


def _write_checked(path: str, text: str, ok) -> bool:
    """Write only if the result parses and says what it was meant to say."""
    try:
        doc = yaml.safe_load(text) or {}
    except Exception:
        return False
    if not ok(doc):
        return False
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def _write_yaml(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


def print_stderr(message: str) -> None:
    import sys

    print(f"dev-agent-proxy: {message}", file=sys.stderr, flush=True)
