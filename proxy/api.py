"""The HTTP API of SPEC section 11, and the web UI of section 12.

Two listeners, because the API has two audiences whose trust levels are
opposite: 8099 for the operator, 8098 for agent containers, read-only. The
servers run inside mitmproxy's own asyncio loop, so the policy, the pending
queue and the ring buffer are shared objects rather than anything marshalled.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import struct
import time

import tornado.iostream
import tornado.web

from log import now, warn

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# GET-only, and all an agent container may ever see. SPEC section 11.2.
READ_ONLY = ("/api/policy", "/api/mode", "/api/rulesets")

# The one write an agent container is allowed, and it writes nothing an
# operator reads as policy: the roster of who is up. Safe to open because the
# name recorded comes from the reverse lookup of the caller's own address, not
# from anything the caller sends — a container can register itself and nothing
# else. SPEC section 11.2.
HEARTBEAT = "/api/heartbeat"

STALE = 15     # the entrypoint beats every 5s: two missed and a margin
SWEEP = 5      # how often the roster is re-checked for staleness


def agents(ctx) -> list[str]:
    """The agent containers the proxy believes are up.

    The heartbeat alone. Live connections are not consulted: they answer a
    different question — who is fetching — and an agent that is thinking makes
    none. A container that routes through the proxy without running this
    image's entrypoint is not listed here; the log is where it shows up.
    """
    fresh = time.monotonic() - STALE
    return sorted(name for name, seen in ctx.agents.items() if seen > fresh)


def publish(ctx) -> None:
    """Push the roster to the UIs, but only when it actually changed — the
    sweep runs every few seconds and most of the time has nothing to say."""
    roster = agents(ctx)
    if roster != ctx.roster:
        ctx.roster = roster
        ctx.log.event("agents", roster)


async def sweep(ctx) -> None:
    """Expire agents that stopped beating. A container is killed rather than
    asked to leave, so nothing but silence ever reports it gone."""
    while True:
        await asyncio.sleep(SWEEP)
        fresh = time.monotonic() - STALE
        for name in [n for n, seen in ctx.agents.items() if seen <= fresh]:
            del ctx.agents[name]  # pruned, not just filtered: this dict lives
        publish(ctx)              # as long as the proxy does


def trusted_subnet() -> ipaddress.IPv4Network | None:
    """The subnet of the interface carrying the default route.

    The operator's traffic arrives through a published port, SNAT'd to the
    egress bridge gateway, so it is in that subnet by construction. An agent
    network is `internal: true` and has no gateway, so it can never carry the
    default route, however many of them are attached. Deriving the trusted subnet this way needs no
    guess about which of eth0/eth1 is which. SPEC section 11.3.

    None means nothing is trusted: the guard fails closed.
    """
    try:
        rows = []
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) > 7:
                    rows.append((f[0], int(f[1], 16), int(f[2], 16), int(f[7], 16)))
    except OSError as exc:
        warn(f"cannot read /proc/net/route ({exc}); no peer is trusted")
        return None

    default_if = next((r[0] for r in rows if r[1] == 0 and r[3] == 0), None)
    if default_if is None:
        warn("no default route; no peer is trusted, the UI will be read-only")
        return None

    for iface, dest, gateway, mask in rows:
        if iface == default_if and gateway == 0 and mask:
            network = socket.inet_ntoa(struct.pack("<I", dest))
            # Bit count is invariant under the byte swap, so the little-endian
            # mask can be counted where it stands.
            return ipaddress.ip_network(f"{network}/{bin(mask).count('1')}", strict=False)

    warn(f"default route on {default_if} has no subnet; no peer is trusted")
    return None


class Base(tornado.web.RequestHandler):
    def initialize(self, ctx, full: bool):
        self.ctx = ctx
        self.full = full

    @property
    def trusted(self) -> bool:
        """Read-write only for the operator. Everything else gets 11.2."""
        if not self.full:
            return False
        subnet = self.ctx.trusted
        if subnet is None:
            return False
        try:
            return ipaddress.ip_address(self.request.remote_ip) in subnet
        except ValueError:
            return False

    def prepare(self):
        if self.trusted:
            return
        if self.request.method == "GET" and self.request.path in READ_ONLY:
            return
        if self.request.method == "POST" and self.request.path == HEARTBEAT:
            return
        self.set_status(403)
        self.finish({"error": "read-only: this API is writable only from the host UI"})

    def write_error(self, status_code, **kwargs):
        self.finish({"error": self._reason})

    def body(self) -> dict:
        try:
            return json.loads(self.request.body or b"{}")
        except ValueError:
            raise tornado.web.HTTPError(400, reason="body is not JSON")


class Policy(Base):
    def get(self):
        self.ctx.policy.reload()
        self.write(self.ctx.policy.snapshot())


class Mode(Base):
    def get(self):
        self.write({"mode": self.ctx.policy.mode})

    def put(self):
        try:
            self.ctx.policy.set_mode(self.body().get("mode"))
        except ValueError as exc:
            raise tornado.web.HTTPError(400, reason=str(exc))
        self.ctx.log.event("state", {"mode": self.ctx.policy.mode})
        self.write({"mode": self.ctx.policy.mode})


class Rulesets(Base):
    def get(self, filename=None):
        self.ctx.policy.reload()
        if filename is None:
            self.write({"rulesets": self.ctx.policy.snapshot()["rulesets"]})
            return
        ruleset = self.ctx.policy.rulesets.get(filename)
        if ruleset is None:
            raise tornado.web.HTTPError(404, reason="no such ruleset")
        self.write(
            {
                "name": ruleset.name,
                "file": ruleset.filename,
                "description": ruleset.description,
                "enabled": ruleset.enabled,
                "rules": [vars(r) for r in ruleset.rules],
            }
        )

    def post(self):
        try:
            filename = self.ctx.policy.create_ruleset(self.body().get("name"))
        except ValueError as exc:
            raise tornado.web.HTTPError(400, reason=str(exc))
        self.ctx.log.event("state", {"ruleset": filename})
        self.write({"file": filename})

    def put(self, filename):
        if filename not in self.ctx.policy.rulesets:
            raise tornado.web.HTTPError(404, reason="no such ruleset")
        body = self.body()
        try:
            if "name" in body:
                self.ctx.policy.set_name(filename, body["name"])
        except ValueError as exc:
            raise tornado.web.HTTPError(400, reason=str(exc))
        if "enabled" in body:
            self.ctx.policy.set_enabled(filename, bool(body["enabled"]))
        if "description" in body:
            self.ctx.policy.set_description(filename, body["description"])
        self.ctx.log.event("state", {"ruleset": filename})
        self.write({"ok": True})


class Rules(Base):
    """Add rules to a ruleset, and enable, disable or retitle the ones there."""

    def post(self, filename):
        if filename not in self.ctx.policy.rulesets:
            raise tornado.web.HTTPError(404, reason="no such ruleset")
        from policy import Rule

        try:
            rules = [
                Rule(match=r.get("match", ""), action=r.get("action", "allow"),
                     path=r.get("path") or None, note=r.get("note", ""))
                for r in self.body().get("rules") or []
            ]
            self.ctx.policy.append_rules(filename, rules)
        except (AttributeError, TypeError, ValueError) as exc:
            raise tornado.web.HTTPError(400, reason=str(exc) or "malformed rule")
        self.ctx.log.event("state", {"ruleset": filename})
        self.write({"ok": True, "rules": len(rules)})

    def put(self, filename):
        if filename not in self.ctx.policy.rulesets:
            raise tornado.web.HTTPError(404, reason="no such ruleset")
        body = self.body()
        try:
            field = next((k for k in ("action", "match", "path", "note") if k in body), None)
            if "to" in body or "to_file" in body:
                target = body.get("to_file") or filename
                if target not in self.ctx.policy.rulesets:
                    raise tornado.web.HTTPError(404, reason="no such ruleset")
                to = body.get("to")
                self.ctx.policy.move_rule(filename, int(body["index"]), target,
                                          None if to is None else int(to))
                touched = 1
            elif field:
                self.ctx.policy.set_rule_field(filename, int(body["index"]), field, body[field])
                touched = 1
            else:
                indexes = [int(i) for i in body.get("indexes") or []]
                if not indexes:
                    raise tornado.web.HTTPError(400, reason="no rules named")
                self.ctx.policy.set_rules_enabled(filename, indexes, bool(body.get("enabled")))
                touched = len(indexes)
        except (TypeError, ValueError, KeyError) as exc:
            # The policy's own message says which rule or action was wrong;
            # only a missing or unparsable index has nothing to say for itself.
            raise tornado.web.HTTPError(
                400, reason=str(exc) if isinstance(exc, ValueError) and str(exc)
                else "index and indexes must be whole numbers"
            )
        except IndexError as exc:
            raise tornado.web.HTTPError(404, reason=str(exc))
        self.ctx.log.event("state", {"ruleset": filename})
        self.write({"ok": True, "rules": touched})

    def delete(self, filename):
        """Drop one rule. The index is a query argument: a DELETE body is not
        carried by every client, and one number does not need one."""
        if filename not in self.ctx.policy.rulesets:
            raise tornado.web.HTTPError(404, reason="no such ruleset")
        try:
            index = int(self.get_argument("index"))
        except ValueError:
            raise tornado.web.HTTPError(400, reason="index must be a whole number")
        try:
            self.ctx.policy.delete_rule(filename, index)
        except IndexError as exc:
            raise tornado.web.HTTPError(404, reason=str(exc))
        self.ctx.log.event("state", {"ruleset": filename})
        self.write({"ok": True, "deleted": index})


class Pending(Base):
    def get(self):
        self.write({"pending": self.ctx.policy.pending.as_json()})

    def post(self, key):
        body = self.body()
        decision = body.get("decision")
        if decision not in ("allow", "deny"):
            raise tornado.web.HTTPError(400, reason="decision must be allow or deny")
        try:
            resolved = self.ctx.policy.pending.resolve(
                key, decision, body.get("save_to"), body.get("scope", "host"),
                enable=bool(body.get("enable")),
            )
        except KeyError as exc:
            raise tornado.web.HTTPError(
                404, reason=f"no such pending entry or ruleset: {exc.args[0]}"
            )
        self.write({"resolved": resolved})


class LogTail(Base):
    def get(self):
        self.write(
            {
                "log": self.ctx.log.tail(
                    since=self.get_argument("since", None),
                    host=self.get_argument("host", None),
                    cat=self.get_argument("cat", None),
                    decision=self.get_argument("decision", None),
                )
            }
        )

    def delete(self):
        """Clear the record: everything, or everything older than `seconds`.
        The age is resolved here rather than sent as a timestamp, so a UI
        whose clock differs from the proxy's still purges what it displayed."""
        try:
            seconds = float(self.get_argument("seconds", 0))
        except ValueError:
            raise tornado.web.HTTPError(400, reason="seconds must be a number")
        if seconds < 0:
            raise tornado.web.HTTPError(400, reason="seconds cannot be negative")
        self.write({"dropped": self.ctx.log.purge(now(seconds) if seconds else None)})


class Events(Base):
    async def get(self):
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        # Tornado holds the headers back until something is written, and the
        # first event can be 20s away — the tab would sit on 'connecting' that
        # whole time. A comment line opens the stream now.
        self.write(": open\n\n")
        # Seeded here rather than fetched: connect and disconnect only ever
        # arrive as events, so a tab that opened between two of them would
        # otherwise show nothing until the next one.
        self.write(f"event: agents\ndata: {json.dumps(agents(self.ctx))}\n\n")
        await self.flush()
        queue = self.ctx.log.subscribe()
        try:
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), 20)
                    self.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n")
                except (asyncio.TimeoutError, TimeoutError):
                    self.write(": keepalive\n\n")  # through idle proxies and tabs
                await self.flush()
        except (tornado.iostream.StreamClosedError, asyncio.CancelledError):
            pass
        finally:
            self.ctx.log.unsubscribe(queue)


class Heartbeat(Base):
    """An agent container saying it is still here.

    The body is ignored: the name is the reverse lookup of the peer, the same
    one the log labels its records with, so a container can only ever register
    itself. Resolved off the loop, because gethostbyaddr blocks — one lookup a
    minute per agent, on the listener agents can reach.
    """

    async def post(self):
        ip = self.request.remote_ip
        try:
            name = (await asyncio.get_running_loop().run_in_executor(
                None, socket.gethostbyaddr, ip
            ))[0].split(".")[0]
        except (OSError, socket.herror):
            name = ip
        self.ctx.agents[name] = time.monotonic()
        publish(self.ctx)
        self.write({"agent": name})


class Power(Base):
    """Stop the proxy, or restart it.

    Nothing here talks to docker: the container's own restart policy is
    on-failure, so the exit status decides which of the two happens. 0 and the
    container stays down until the next 'dev-agent --proxy' brings it back; 1
    and docker starts it again. That keeps the docker socket out of a container
    whose whole job is to be the one thing the agents can reach.

    The reply is flushed before the exit, so the UI hears the ack rather than a
    dropped connection.
    """

    async def post(self, what):
        warn(f"{what} asked for by {self.request.remote_ip}")
        self.write({what: True})
        await self.flush()
        asyncio.get_running_loop().call_later(
            0.1, os._exit, 0 if what == "shutdown" else 1
        )


class Index(tornado.web.RequestHandler):
    """The UI if it is installed in the image, else what to call instead."""

    def get(self):
        index = os.path.join(UI_DIR, "index.html")
        if os.path.exists(index):
            self.set_header("Content-Type", "text/html; charset=utf-8")
            # The UI is baked into the image, so it only ever changes when the
            # image is rebuilt — and a tab held open by the event stream would
            # otherwise keep serving the UI from before that rebuild.
            self.set_header("Cache-Control", "no-store")
            self.write(open(index, "rb").read())
            return
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write(
            "dev-agent proxy — no web UI in this image.\n\n"
            "  GET  /api/policy\n"
            "  GET  /api/mode          PUT {\"mode\": \"default-deny\"}\n"
            "  GET  /api/rulesets      POST {\"name\": \"...\"}\n"
            "  GET  /api/rulesets/<file>\n"
            "                          PUT {\"name\": \"...\", \"enabled\": true,\n"
            "                               \"description\": \"...\"}\n"
            "  POST /api/rulesets/<file>/rules {\"rules\": [{\"match\": \"...\",\n"
            "                               \"action\": \"allow\", \"note\": \"...\"}]}\n"
            "  PUT  /api/rulesets/<file>/rules {\"indexes\": [0, 2], \"enabled\": false}\n"
            "  PUT  /api/rulesets/<file>/rules {\"index\": 0, \"to\": 2,\n"
            "                                   \"to_file\": \"other.yml\"}\n"
            "                          or {\"index\": 0, \"match\"|\"path\"|\n"
            "                              \"action\"|\"note\": \"...\"}\n"
            "  DELETE /api/rulesets/<file>/rules?index=0\n"
            "  GET  /api/pending       POST /api/pending/<key> {\"decision\": \"allow\"}\n"
            "  GET  /api/log           DELETE /api/log?seconds=3600\n"
            "  POST /api/heartbeat     (from an agent container, every 60s)\n"
            "  GET  /api/events (SSE)\n"
            "  POST /api/shutdown      POST /api/restart\n"
        )


def serve(ctx) -> None:
    """Start both listeners on the loop that is already running."""
    for port, full in ((8099, True), (8098, False)):
        args = {"ctx": ctx, "full": full}
        routes = [
            (r"/api/policy", Policy, args),
            (r"/api/mode", Mode, args),
            (r"/api/rulesets", Rulesets, args),
            (r"/api/rulesets/([^/]+)", Rulesets, args),
            (r"/api/rulesets/([^/]+)/rules", Rules, args),
            (r"/api/pending", Pending, args),
            (r"/api/pending/([^/]+)", Pending, args),
            (r"/api/heartbeat", Heartbeat, args),
            (r"/api/log", LogTail, args),
            (r"/api/events", Events, args),
            (r"/api/(shutdown|restart)", Power, args),
        ]
        if full:
            routes += [
                (r"/", Index),
                (r"/(.*)", tornado.web.StaticFileHandler, {"path": UI_DIR}),
            ]
        tornado.web.Application(routes).listen(port, address="0.0.0.0")
