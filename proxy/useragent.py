"""User-Agent override: what the proxy puts in the header on the way out.

Three modes. `passthrough` leaves the agent's own header alone. `single` sends
the first string of the list on every request. `random` picks one of the list
per request — or, with `sticky`, once per remote host and then the same one for
that host until the setting changes, so one site sees one consistent browser.

Kept in /state/useragent.json, so it survives a restart. Only intercepted
requests can be rewritten: a `tunnel` rule never shows the proxy a header.
"""

from __future__ import annotations

import json
import os
import random

MODES = ("passthrough", "single", "random")

# The versions the presets claim. One number per browser, so keeping them
# current is a one-line edit. Chrome's and Edge's minor parts are always zero
# since the UA reduction, and so are the OS versions they report.
CHROME = "148"
FIREFOX = "150"
SAFARI = "26.5"

_WEBKIT = "AppleWebKit/537.36 (KHTML, like Gecko)"
_CHROME = f"{_WEBKIT} Chrome/{CHROME}.0.0.0"
_IOS = "CPU iPhone OS 18_6 like Mac OS X"  # frozen by Safari 26; the real one is in Version/

PRESETS = [
    # Desktop
    {"label": "Chrome / Windows", "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_CHROME} Safari/537.36"},
    {"label": "Chrome / macOS", "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) {_CHROME} Safari/537.36"},
    {"label": "Chrome / Linux", "ua": f"Mozilla/5.0 (X11; Linux x86_64) {_CHROME} Safari/537.36"},
    {"label": "Edge / Windows", "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_CHROME} Safari/537.36 Edg/{CHROME}.0.0.0"},
    {"label": "Firefox / Windows", "ua": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{FIREFOX}.0) Gecko/20100101 Firefox/{FIREFOX}.0"},
    {"label": "Firefox / macOS", "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{FIREFOX}.0) Gecko/20100101 Firefox/{FIREFOX}.0"},
    {"label": "Firefox / Linux", "ua": f"Mozilla/5.0 (X11; Linux x86_64; rv:{FIREFOX}.0) Gecko/20100101 Firefox/{FIREFOX}.0"},
    {"label": "Safari / macOS", "ua": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{SAFARI} Safari/605.1.15"},
    # Mobile
    {"label": "Chrome / Android", "ua": f"Mozilla/5.0 (Linux; Android 10; K) {_WEBKIT} Chrome/{CHROME}.0.0.0 Mobile Safari/537.36"},
    {"label": "Firefox / Android", "ua": f"Mozilla/5.0 (Android 15; Mobile; rv:{FIREFOX}.0) Gecko/{FIREFOX}.0 Firefox/{FIREFOX}.0"},
    {"label": "Safari / iPhone", "ua": f"Mozilla/5.0 (iPhone; {_IOS}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{SAFARI} Mobile/15E148 Safari/604.1"},
    {"label": "Chrome / iPhone", "ua": f"Mozilla/5.0 (iPhone; {_IOS}) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/{CHROME}.0.0.0 Mobile/15E148 Safari/604.1"},
]


class UserAgent:
    def __init__(self, state: str):
        self.path = os.path.join(state, "useragent.json")
        self.mode, self.agents, self.sticky = "passthrough", [], False
        self.by_host: dict[str, str] = {}
        try:
            with open(self.path) as fh:
                self.set(json.load(fh), save=False)
        except (OSError, ValueError):
            pass  # none yet, or unreadable: passthrough, and the UI rewrites it

    def as_json(self) -> dict:
        return {"mode": self.mode, "agents": self.agents, "sticky": self.sticky}

    def set(self, cfg: dict, save: bool = True) -> None:
        """Replace the whole setting. Raises ValueError on a bad one, and
        leaves the old one in place when it does."""
        mode = cfg.get("mode", self.mode)
        agents = cfg.get("agents", self.agents)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
            raise ValueError("agents must be a list of strings")
        # A header value cannot carry a line break, and an empty one is not a
        # user agent; both are dropped rather than refused, so a textarea's
        # trailing newline is not an error.
        agents = [a.strip() for a in agents if a.strip() and "\n" not in a and "\r" not in a]
        if mode != "passthrough" and not agents:
            raise ValueError(f"{mode} needs at least one user agent")
        self.mode, self.agents = mode, agents
        self.sticky = bool(cfg.get("sticky", self.sticky))
        self.by_host = {}  # a new list means new picks
        if save:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.as_json(), fh, indent=2)
            os.replace(tmp, self.path)

    def pick(self, host: str) -> str | None:
        """The header to send to `host`, or None to leave it alone."""
        if self.mode == "passthrough":
            return None
        if self.mode == "single":
            return self.agents[0]
        if not self.sticky:
            return random.choice(self.agents)
        # ponytail: one entry per host ever contacted, until the setting
        # changes or the proxy restarts. Bound it if an agent crawls widely.
        return self.by_host.setdefault(host, random.choice(self.agents))
