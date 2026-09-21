"""dev-agent egress proxy — the mitmproxy addon that wires everything together.

Policy lives in policy.py, the record in log.py, the API and UI in api.py.
This file is the part that touches mitmproxy: it decides, it holds a flow
while a human answers, and it writes one record per request.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import time
import uuid

import api
import policy as policy_mod
from log import Log, now, warn

STATE = os.environ.get("DEV_AGENT_PROXY_STATE", "/state")
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults")

CA_DIR = os.path.join(STATE, "ca")           # mitmproxy's confdir: holds the key
AGENT_CA_DIR = os.path.join(STATE, "agent-ca")  # the certificate alone, mounted
                                                # into agent containers
LOG_FILE = os.path.join(STATE, "log.jsonl")  # the record of section 10, kept
                                             # across restarts

FORBIDDEN = """403 Forbidden

dev-agent proxy: egress to {host} is not allowed.

  mode     {mode}
  decision {rule}
  policy   curl -s http://dev-agent-proxy:8098/api/policy

This is policy, not a network fault, and retrying will not change it. You cannot
change the policy from in here. A human can, at http://127.0.0.1:8099 — asking
them is the right next step.
"""


def setup_state() -> None:
    """Lay out /state, and set the modes an agent container depends on.

    Agent containers run as the invoking user's uid, which is not the uid the
    proxy writes as, and a read-only bind mount does not relax permissions.
    Set on every start, because docker creates the directory root-owned when
    compose first mounts it. SPEC section 5.2.
    """
    os.makedirs(CA_DIR, exist_ok=True)
    os.makedirs(AGENT_CA_DIR, exist_ok=True)
    os.makedirs(os.path.join(STATE, "rulesets"), exist_ok=True)
    os.chmod(CA_DIR, 0o700)
    os.chmod(AGENT_CA_DIR, 0o755)
    os.chmod(STATE, 0o755)

    # Starters, copied in only where there is nothing. A file the user has
    # edited is never overwritten.
    if not os.listdir(os.path.join(STATE, "rulesets")):
        shutil.copy(os.path.join(DEFAULTS, "default.yml"),
                    os.path.join(STATE, "rulesets", "default.yml"))
    if not os.path.exists(os.path.join(STATE, "categories.yml")):
        shutil.copy(os.path.join(DEFAULTS, "categories.yml"),
                    os.path.join(STATE, "categories.yml"))


async def publish_ca() -> None:
    """Copy the CA certificate — and only the certificate — where agents can
    read it. mitmproxy generates it into its confdir as it starts, so this
    polls rather than assuming it is already there."""
    source = os.path.join(CA_DIR, "mitmproxy-ca-cert.pem")
    target = os.path.join(AGENT_CA_DIR, "ca.crt")
    announced = False
    while True:
        try:
            if os.path.exists(source) and (
                not os.path.exists(target)
                or os.path.getmtime(source) > os.path.getmtime(target)
            ):
                shutil.copy(source, target)
                os.chmod(target, 0o644)
                warn(f"published the interception CA to {target}")
            elif not announced and not os.path.exists(source):
                warn("waiting for mitmproxy to generate its CA")
                announced = True
        except OSError as exc:
            warn(f"cannot publish the CA: {exc}")
        await asyncio.sleep(2)


class Ctx:
    """What api.py is handed: the shared objects, not a copy of them."""

    def __init__(self):
        self.log = Log(path=LOG_FILE)
        self.policy = policy_mod.Policy(STATE, emit=self.log.event)
        self.trusted = None  # set in running(), once there is a route table
        self.clients: dict[str, str] = {}  # live connections, by mitmproxy id
        self.agents: dict[str, float] = {}  # last heartbeat, by container name
        self.roster: list[str] = []         # what the UIs were last told


class EgressProxy:
    def __init__(self):
        setup_state()
        self.ctx = Ctx()
        self.log = self.ctx.log
        self.policy = self.ctx.policy
        self.clients = self.ctx.clients

    # -- lifecycle -------------------------------------------------------

    def running(self):
        self.ctx.trusted = api.trusted_subnet()
        api.serve(self.ctx)
        asyncio.get_running_loop().create_task(publish_ca())
        asyncio.get_running_loop().create_task(api.sweep(self.ctx))
        warn(
            f"mode {self.policy.mode}; UI on :8099 (trusted peers: "
            f"{self.ctx.trusted or 'none, read-only'}), read-only API on :8098"
        )

    # -- client identity — SPEC section 10.1 -----------------------------

    async def client_connected(self, client):
        """Resolve the container name once per connection, off the loop.

        Not per request: gethostbyaddr blocks, and a busy npm install would
        put a DNS round trip in front of every one of a few hundred requests.
        Not cached by IP either: docker reuses addresses, and a stale entry
        would label a live session with a dead container's name.
        """
        ip = client.peername[0]
        self.clients[client.id] = ip
        try:
            name = await asyncio.get_running_loop().run_in_executor(
                None, socket.gethostbyaddr, ip
            )
            # docker answers with the network appended; a container name
            # cannot contain a dot, so the first label is exact.
            self.clients[client.id] = name[0].split(".")[0]
        except (OSError, socket.herror):
            pass
        api.publish(self.ctx)

    def client_disconnected(self, client):
        self.clients.pop(client.id, None)
        api.publish(self.ctx)

    def _client(self, conn) -> str:
        return self.clients.get(conn.id, conn.peername[0])

    # -- decisions -------------------------------------------------------

    def tls_clienthello(self, data):
        """A `tunnel` rule skips interception, for pinned clients. Only the
        host is known here, so a rule carrying a path cannot match."""
        host = data.client_hello.sni or data.context.server.address[0]
        action, rule = self.policy.decide(host, None)
        if action != "tunnel":
            return
        data.ignore_connection = True
        self.log.record(
            self.record(
                client=self._client(data.context.client),
                method=None, scheme="https", host=host,
                port=data.context.server.address[1], path=None,
                decision="tunnel", rule=rule,
            )
            # ponytail: a tunnelled connection is recorded at the decision,
            # so bytes and duration stay 0 — counting them means tracking the
            # raw connection to its close. Add that if the byte counts on
            # tunnelled hosts ever need to be real.
        )

    # Nothing is decided at CONNECT, deliberately. A 403 answered to a CONNECT
    # is a body no client shows anyone: curl reports "Received HTTP code 403
    # from proxy after CONNECT" and discards it, and SPEC section 13.1 rests on
    # that body being read. So the tunnel is always established for a host we
    # intercept, and the decision — deny included — is made on the inner
    # request, where the path is known and the body reaches the client. No
    # upstream connection is opened in the meantime: connection_strategy is
    # lazy, and the certificate is generated from the SNI.

    async def request(self, flow):
        if flow.request.method == "CONNECT":
            return  # handled in http_connect

        host = flow.request.pretty_host
        path = flow.request.path
        cat = self.policy.category(host)
        started = time.time()

        action, rule = self.policy.decide(host, path)
        held_ms = 0

        if action == "pending":
            waited = time.time()
            action, rule = await self.policy.pending.ask(
                flow.request.method, host, path, flow.request.pretty_url, cat,
                self._client(flow.client_conn),
            )
            held_ms = int((time.time() - waited) * 1000)

        flow.metadata.update(
            id=str(uuid.uuid4()), started=started, cat=cat,
            decision=action, rule=rule, held_ms=held_ms,
            client=self._client(flow.client_conn),
        )

        if action == "deny":
            self.deny(flow, host, rule, cat=cat, held_ms=held_ms,
                      client=flow.metadata["client"], rec_id=flow.metadata["id"])

    # -- records ---------------------------------------------------------

    def record(self, **kw) -> dict:
        rec = {
            "ts": now(), "id": str(uuid.uuid4()), "client": None,
            "method": None, "scheme": None, "host": None, "port": None,
            "path": None, "status": None, "cat": None, "decision": None,
            "rule": None, "mode": self.policy.mode,
            "bytes_up": 0, "bytes_down": 0, "ms": 0, "held_ms": 0,
        }
        rec.update(kw)
        if rec["cat"] is None and rec["host"]:
            rec["cat"] = self.policy.category(rec["host"])
        return rec

    def deny(self, flow, host, rule, cat=None, held_ms=0, client=None,
             rec_id=None):
        from mitmproxy import http

        body = FORBIDDEN.format(host=host, mode=self.policy.mode, rule=rule)
        flow.response = http.Response.make(
            403, body, {"Content-Type": "text/plain; charset=utf-8"}
        )
        # Recorded here rather than in the response hook: the response is ours,
        # and the flow never goes upstream.
        flow.metadata["logged"] = True
        self.log.record(
            self.record(
                id=rec_id or str(uuid.uuid4()),
                client=client or self._client(flow.client_conn),
                method=flow.request.method, scheme=flow.request.scheme,
                host=host, port=flow.request.port, path=flow.request.path,
                status=403, cat=cat, decision="deny", rule=rule,
                bytes_down=len(body), held_ms=held_ms,
            )
        )

    def response(self, flow):
        if flow.metadata.get("logged") or flow.request.method == "CONNECT":
            return
        flow.metadata["logged"] = True
        started = flow.metadata.get("started", flow.timestamp_start)
        self.log.record(
            self.record(
                id=flow.metadata.get("id", str(uuid.uuid4())),
                client=flow.metadata.get("client") or self._client(flow.client_conn),
                method=flow.request.method, scheme=flow.request.scheme,
                host=flow.request.pretty_host, port=flow.request.port,
                path=flow.request.path, status=flow.response.status_code,
                cat=flow.metadata.get("cat"),
                decision=flow.metadata.get("decision", "allow"),
                rule=flow.metadata.get("rule"),
                bytes_up=len(flow.request.raw_content or b""),
                bytes_down=len(flow.response.raw_content or b""),
                ms=int((time.time() - started) * 1000),
                held_ms=flow.metadata.get("held_ms", 0),
            )
        )

    def error(self, flow):
        if flow.metadata.get("logged") or not getattr(flow, "request", None):
            return
        flow.metadata["logged"] = True
        started = flow.metadata.get("started", flow.timestamp_start)
        self.log.record(
            self.record(
                id=flow.metadata.get("id", str(uuid.uuid4())),
                client=flow.metadata.get("client") or self._client(flow.client_conn),
                method=flow.request.method, scheme=flow.request.scheme,
                host=flow.request.pretty_host, port=flow.request.port,
                path=flow.request.path, status=None,
                cat=flow.metadata.get("cat"),
                decision=flow.metadata.get("decision", "allow"),
                rule=f"error:{flow.error.msg}" if flow.error else flow.metadata.get("rule"),
                bytes_up=len(flow.request.raw_content or b""),
                ms=int((time.time() - started) * 1000),
                held_ms=flow.metadata.get("held_ms", 0),
            )
        )


addons = [EgressProxy()]
