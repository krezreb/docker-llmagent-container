# Validation of the assumptions in SPEC.md section 14

Measured, not reasoned about. Where a result contradicted the specification, the
specification was changed and this file says so.

Two harnesses, and which one produced a number matters. Assumptions 1, 2 and 4 come
from `spike/check.sh`, which stands in for the design with `mitmdump` and no addon.
Assumption 3 and the subnet guard could not be measured that way — they *are* the
addon — so they come from the built `dev-agent-proxy` image driven by hand. Those
runs are reproducible from the commands quoted below, but they are **not yet in
`spike/check.sh`**, which is the gap named at the end of this file.

## Environment

| | |
|---|---|
| Date | 2026-09-18 |
| Host | Linux 7.0.0-31-generic |
| docker | 29.8.0 |
| Agent image | `dev-agent:ubuntu26`, Node 24.21.0 |
| Proxy | `mitmproxy/mitmproxy:latest` — mitmproxy 12.2.3, Python 3.14.5 |
| Proxy image | `dev-agent-proxy`, built from `proxy/Dockerfile` on `mitmproxy/mitmproxy:12.2.3` |
| Repository | `6b70b02`, plus the working tree that adds `proxy/` |

Result: **12 passed, 0 failed, 0 skipped** from `spike/check.sh`, and **13 passed**
from `make -C proxy test`, which covers the evaluation order and the pending queue
as units. The measurements of assumption 3 and of the subnet guard below are from
the running proxy and are not counted in either figure.

The spike stands up two networks (one `internal`, one not), a `mitmdump` with
interception on attached to both, and runs throwaway containers on the internal
network with the hardening `dev-agent` applies — `--read-only`, `--cap-drop=ALL`,
`no-new-privileges`, tmpfs `/tmp`, no inherited environment. It is not a
reconstruction of the design; it is the design's network and certificate wiring with
`mitmdump` standing in for the addon.

## 1. Isolation isolates — CONFIRMED

A container on the `internal` network reaches neither a name nor a raw IP:
`https://example.com` and `http://1.1.1.1` both fail. Through the proxy, HTTPS, plain
HTTP and `git clone https://` all succeed. Unsetting `HTTP_PROXY`/`HTTPS_PROXY` inside
the container loses the network rather than freeing it, which is the property section
4 rests on: the enforcement boundary is the docker network, and the environment
variables are only a convenience.

**Correction to section 4: there is no DNS tunnelling channel on this engine.**
The draft assumed the daemon forwards queries it cannot answer, and that a
DNS-tunnelling client could therefore move data out slowly. It does not forward for
an `internal` network:

```
$ docker run --rm --network internal-net dev-agent:ubuntu26 getent hosts example.com
(nothing, exit 2)
```

`/etc/resolv.conf` inside the container still points at `127.0.0.11` and the daemon
still lists an upstream (`ExtServers: [host(127.0.0.53)]`), so this is a decision the
daemon makes per network rather than an absence of configuration. That makes it
engine behaviour and not a guarantee of this design, so the spike re-measures it on
every run and section 4 now claims only what was measured.

## 2. The agents survive interception — CONFIRMED

`claude -p` and `codex exec` each completed a turn through the intercepting proxy, on
a read-only root filesystem, with the CA wired as in section 7 and the persistent
agent home mounted. Alongside them `curl`, `python3`, `node`, `git clone https://`.

Two things had to be fixed before this passed, and only one of them was the spike's
fault.

### Node ignores the proxy environment — SPEC.md amended

`node -e 'fetch("https://example.com")'` failed with:

```
fetch failed |cause: getaddrinfo EAI_AGAIN example.com
```

Not a certificate rejection, which is how it first read. Node reads neither
`HTTP_PROXY` nor `HTTPS_PROXY` on its own, in `fetch`/undici or in the core
`http`/`https` modules, so it connected directly and died at name resolution — see
assumption 1 for why that is the error it gives. Adding
`NODE_OPTIONS=--use-env-proxy` fixed `fetch` and `http`/`https` both, and section 7's
environment block now carries it. It requires Node 24 or newer.

This was worth the detour. Anything in the container that shells out to node is
affected, the failure names DNS and points at nothing that is wrong, and it would
have surfaced as "the proxy is broken" rather than as a missing variable. A check
that the variable is still *necessary* is in the spike so that it cannot quietly fall
out of section 7 again.

### `codex` refuses to run outside a git repository — spike fixed

`Not inside a trusted directory and --skip-git-repo-check was not specified.` An
artefact of the throwaway container having no repository in its working directory; a
real `--proxy` session is always in a project. The spike passes the flag.

## 3. A flow can be held — CONFIRMED

A mitmproxy addon can await a decision in an `async def request` hook, the flow stays
open while it does, and the request completes afterwards. Section 8.3's fallback —
deny immediately and rely on the agent's retry — is not needed.

Three parallel `curl https://example.com/` from one container, under `default-deny`
with no rule matching, coalesced into one queue entry rather than three:

```
$ curl -s http://127.0.0.1:8099/api/pending
{"pending": [{"key": "6658cbc6fad0fb8e", "method": "GET", "host": "example.com",
  "path": "/", "url": "https://example.com/", "cat": "unknown",
  "clients": ["pendtest"], "waiters": 3, "waiting_ms": 2995}]}

$ curl -s -X POST -d '{"decision":"allow","save_to":"default.yml","scope":"host"}' \
       http://127.0.0.1:8099/api/pending/6658cbc6fad0fb8e
{"resolved": 3}

example 200 in 6.531509s
example 200 in 6.631232s
example 200 in 6.840611s
```

One decision released all three, each with `"decision":"allow","rule":"pending:allow"`
and `held_ms` of 3026, 3028 and 3025 — the wait is charged to the record, and the
`ms` of 6508 is the wait plus the real request. This is the property section 8.3
rests on: greenlighting in real time means the request the agent made completes, not
that the human fixes it for the next attempt.

The per-waiter timeout is exact. One request to a host nobody answered for:

```
timeout case 403

real	0m30.027s
```

and the record `"status":403,"decision":"deny","rule":"pending:timeout","held_ms":30001`.
An unattended run fails after 30 seconds rather than hanging.

**What was measured, and what was not.** The clients here were `curl`, `node`'s
`fetch` and `git`, all of which tolerated the full 30 seconds. Section 14.3 asks for
`claude` and `codex` as well, on the grounds that an agent giving up at 10 seconds
turns the queue into a way of failing slowly. **That has not been measured**, and it
stays on the list at the end of this file. What is now settled is the mechanism; what
is not is each agent's own patience.

### Correction to sections 7, 8 and 13.1: nothing is decided at CONNECT

The draft had the proxy answer a denied request with `403` wherever it was denied.
For HTTPS that body is invisible. Answering `403` to a `CONNECT` gets:

```
curl: (56) Received HTTP code 403 from proxy after CONNECT
```

with the body discarded, and the client printed nothing but `[000]`. Section 13.1
calls that body "the mechanism that always works", and for every HTTPS request —
which is nearly all of them — it did not work at all.

So the addon decides nothing at `CONNECT`. The tunnel is always established for a
host the proxy intercepts, and the decision, deny included, is made on the inner
request, where the path is known and the body reaches the client:

```
403 Forbidden

dev-agent proxy: egress to example.org is not allowed.

  mode     default-deny
  decision zz-test.yml:blocked on purpose
  policy   curl -s http://dev-agent-proxy:8098/api/policy
...
[403]
```

Nothing is dialled upstream in the meantime: `connection_strategy` is `lazy` and the
interception certificate is generated from the SNI, so a denied host is never
connected to. The cost is one TLS handshake with our own certificate for a host that
is about to be refused, which is cheaper than an unreadable refusal. The ask key of a
held HTTPS request is therefore `(GET, host, path)` and not `(CONNECT, host, null)`;
the `CONNECT` form remains what a `tunnel` rule coalesces on, since a tunnelled
connection is all the proxy ever sees of it.

## 4. Reverse DNS yields a usable `client` — CONFIRMED, with an amendment

The proxy resolved the client container's address to its name.

**Amendment to section 10:** the answer carries the network — `spike-client.spike-agents`,
not `spike-client`. `client` is therefore the first label only. A container name
cannot contain a dot, so that split is exact.

## The subnet guard of section 11.3 — CONFIRMED

Derived from the default route rather than from an interface name, and the derivation
lands on the right network:

```
dev-agent-proxy: mode default-deny; UI on :8099 (trusted peers: 172.23.0.0/16),
                 read-only API on :8098

$ docker network inspect dev-agent_agents dev-agent_egress
dev-agent_agents internal=true  172.22.0.0/16
dev-agent_egress internal=false 172.23.0.0/16
```

The trusted subnet is `egress`, which is where the operator's traffic arrives from
after docker SNATs the published port, and the `agents` subnet is not trusted. From a
container on `agents`:

```
mode PUT on 8098 -> 403
mode PUT on 8099 -> 403
```

while `GET /api/policy` on `8098` returned the policy. The agent can read the policy
and cannot change it, on either port, which is the whole point of two of them.

## One network per agent (SPEC section 5) — CONFIRMED

A single shared `agents` network let any two sessions reach each other. Two throwaway
containers on `dev-agent_agents`, docker 29.8.1:

```
$ docker exec t-b nc -w2 t-a 9000
HI
```

By container name, not merely by IP: docker's embedded resolver answers for every
member of the network, and `internal: true` changes none of it.

`com.docker.network.bridge.enable_icc=false` is not the fix. On a network created
with it, a container reached neither a second container nor the proxy:

```
--- b -> a:  BLOCKED
--- b -> p:  BLOCKED
```

One internal network per container, with the proxy attached to each, gives the
intended shape. Measured through `dev-agent --proxy` itself, two concurrent
sessions, agent A listening on 9000:

```
control, A to its own listener: 200
B-to-A-by-name: BLOCKED        (curl: Could not resolve host)
B-to-A-by-ip:   BLOCKED        (172.24.0.3, no route)
B-to-internet-via-proxy: 405   (api.anthropic.com, allowed by default.yml)
```

The control matters: without something listening, the two `BLOCKED` lines would be
the same output for the wrong reason.

Nothing downstream broke. The `client` field still resolved:

```json
{"client":"dev-agent-bash-112170","host":"api.anthropic.com","status":405,
 "decision":"allow","rule":"default.yml:Claude API"}
```

and the subnet guard still trusted `egress` and not the session's network
(`trusted peers: 172.23.0.0/16`, with the agent on `172.24.0.0/16`).

The network is removed on the way out, and the exit status is docker's:

```
$ dev-agent --proxy bash -c 'echo inside; exit 7'
inside
dev-agent exit status: 7
after: 0 agent networks
proxy nets now: dev-agent_egress
```

A session killed with `SIGKILL` leaves its network behind — reproduced by killing the
wrapper — which is what the `docker network rm --force` at the start of the next run
under the same name is for.

This is `assumption 5` in `spike/check.sh`, so it is checked rather than remembered.

**Two pre-existing faults in `spike/check.sh` surfaced while adding it**, and are
fixed in the same change: `mktemp -d` leaves the CA directory at `0700`, so every
certificate check failed with `EACCES` on a file that was plainly there, and
`-f "{{.NetworkSettings.Networks.spike-agents.IPAddress}}"` dies with
`bad character U+002D` because a dash is not legal in a Go template field name — which
aborted the run under `set -e` before the DNS check could report. The harness also
lacked the `NODE_OPTIONS=--use-env-proxy` that `dev-agent` passes, so the `node` check
was failing for that and not for the trust store. With those three fixed the spike is
13 passed, 0 failed.

## What is still unmeasured

- **Whether the agents tolerate a held flow.** Assumption 3 is confirmed for `curl`,
  `node` and `git`; section 14.3 asks for `claude` and `codex` too, and an agent that
  gives up after 10 seconds makes the 30 second timeout meaningless. Neither agent's
  HTTP timeout is documented, so this has to be measured rather than read.
- **Interception of the agents' streaming responses under load.** One turn each is
  evidence that interception works at all, not that a long tool-using session holds
  up.
- **Everything above, in `spike/check.sh`.** The addon measurements were driven by
  hand against the running proxy. Until they are in the spike they are a record of
  one afternoon rather than something a change can be checked against, and the two
  that would hurt most to lose are the hold and the subnet guard — the first because
  section 8.3's fallback turns on it, the second because its failure is silent and
  serves the agent a writable API.
- **The proxy under a real session's load.** Every measurement here is a handful of
  requests. An `npm install` behind `default-deny` is the first honest test of the
  queue's coalescing, and it has not been run.
