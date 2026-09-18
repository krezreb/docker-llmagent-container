# dev-agent egress proxy — specification

Status: **all four assumptions measured and confirmed**, and the proxy of sections 5
to 12 is built. `VALIDATION.md` has the results and the three corrections they
forced. Assumption 3 — that a flow can be held while a human decides — is confirmed
for `curl`, `node` and `git`, so section 8.3 is a feature rather than a slow way to
fail; whether `claude` and `codex` wait out the full 30 seconds is the measurement
still owed.

## 1. Why

`dev-agent` gives an agent a strong filesystem and privilege sandbox: a read-only
root filesystem, `--cap-drop=ALL`, `no-new-privileges`, an unprivileged user, two
writable mounts and no inherited environment. It gives the agent no network
restriction at all. `README.md` says so plainly:

> Network access is not restricted. The agents need it to reach their APIs, and so
> does anything they run in your project.

That is the last large hole in the sandbox. An agent that can reach any host on the
internet can exfiltrate the project it was given, fetch and run arbitrary code, or
call a paid API in a loop, and nothing in the current design would show you that it
happened.

This specification describes an opt-in `--proxy` mode that closes the hole and, just
as importantly, makes egress *visible*. The proxy is not only a filter; it is the
first place where a human can see what an agent actually reaches for.

## 2. Goals

1. Under `--proxy`, the agent container has **no route to the internet** except
   through the proxy. Defeating the proxy must mean losing network access, not
   gaining unfiltered access.
2. Every HTTP and HTTPS request is logged with its full URL, decision, category,
   size and duration.
3. A human watching a web UI on the host can see requests as they happen, and can
   allow or deny an unrecognised one **while the agent is waiting on it**.
4. Approvals can be saved into named rulesets that are enabled or disabled from the
   UI, with no restart and no container rebuild.
5. Requests are categorised (LLM API, OS packages, language packages, source
   control, cloud, docs, telemetry) so the log can be read at a glance.
6. The agent itself can enumerate the current policy through a read-only endpoint,
   so that "this fetch failed" can be distinguished from "this fetch is not allowed".
7. The log stream is consumable by ordinary log shipping tools, for Grafana/Loki or
   Elastic.

## 3. Non-goals

These are out of scope on purpose. Each one is a place where this design would
otherwise grow without limit.

- **Not a production egress gateway.** One developer, one host, one proxy. The web
  UI binds to `127.0.0.1` and has no authentication, no users and no roles.
- **Not a defence against a determined, sophisticated exfiltration attempt.**
  Section 4 lists the channels that remain open. The honest claim is: it stops
  casual and accidental egress, it makes deliberate egress visible, and it raises
  the cost of hiding.
- **No traffic shaping, quotas, or rate limits.** Attractive, unrelated.
- **No built-in Loki or Elasticsearch client.** The proxy writes JSON lines to
  stdout and stops there; shipping is `vector`, `promtail`, `filebeat` or
  `fluent-bit`'s job, and those already exist.
- **No policy language.** Rules are host and path globs with an action. If a rule
  needs an expression evaluator, the answer is a better category, not a language.
- **No persistence beyond files.** Rulesets and categories are YAML on disk, the
  request log is a bounded in-memory ring plus stdout. No database.

## 4. Threat model and residual channels

The enforcement boundary is the docker network, not the proxy configuration. The
agent container is attached only to a network created with `internal: true`, which
has no gateway to the outside, and which it shares with nothing but the proxy. The `HTTP_PROXY`/`HTTPS_PROXY` environment variables
are a *convenience*, telling well-behaved clients where the proxy is. If the agent
unsets them, it gets no network, not free network. The failure mode is closed.

That leaves the following open, and the documentation must say so rather than imply
a completeness that does not exist.

**DNS.** Containers on an internal network still reach docker's embedded resolver at
`127.0.0.11`, so container names resolve. Whether the daemon *forwards* what it
cannot answer is the question, and it decides that per network: on docker 29.8.0 it
does not forward for an `internal` network, and an external name simply does not
resolve.

```
$ docker run --rm --network internal-net dev-agent:ubuntu26 getent hosts example.com
(nothing, exit 2)
```

This is the opposite of what an earlier draft of this section assumed, and it is
better news: on this engine there is **no DNS tunnelling channel**, because there is
no recursion to tunnel through. It also explains the `EAI_AGAIN` in section 7 — a
client that bypasses the proxy fails at name resolution rather than at routing.

It is not promised, though. It is engine behaviour, not something this design
enforces, and a docker version or a daemon configuration that does forward would
reopen the channel without anything here changing. So the assumption is re-measured
on every run of the spike rather than assumed, the claim in this document is limited
to "measured absent on docker 29.8.0", and the upgrade path stands if it ever comes
back: give the agent container a stub resolver that knows only the proxy's name, or
skip DNS entirely by passing the proxy's address as an IP.

**Non-HTTP protocols.** Anything that is not HTTP or HTTPS has no path out: raw TCP,
UDP, `git+ssh`, `ssh`, database connections to the internet. These fail rather than
being filtered. This is a real behaviour change, not a detail: **`git push` over SSH
stops working under `--proxy`.** Use an HTTPS remote, or run without `--proxy`.

**Certificate-pinned clients.** The proxy intercepts TLS with its own certificate
authority. A client that pins a certificate will refuse the connection. The escape
hatch is a rule with the `tunnel` action, which passes the connection through
untouched — the hostname is still logged and still subject to allow/deny, only the
contents are not inspected.

**The interception CA is a real CA.** Its private key lives in the proxy's state
directory on the host and is trusted by everything inside the agent container. It
must be generated locally, never committed, and never installed into the host's own
trust store. The container trusts it only through explicit environment variables
(section 7), which is why the blast radius stops at the container. The directory
holding the key is never mounted into an agent container — section 5.2 keeps the key
and the mounted certificate in separate directories for that reason.

**Localhost within the container** is unaffected, and so is container-to-proxy
traffic. An agent can still talk to itself.

## 5. Architecture

```
                    ┌──────────────────────────────────────┐
  host 127.0.0.1    │  dev-agent-proxy                     │
  :8099  ───────────┤    mitmproxy          :3128          ├────── internet
  (web UI + API)    │    web UI + full API  :8099          │      (network: egress)
                    │    read-only API      :8098          │
                    └───────┬──────────────────┬───────┘
                            │                  │
       dev-agent-claude-4711-net    dev-agent-codex-99-net
          (internal: true)             (internal: true)
                            │                  │
        ┌───────────────────┴───┐  ┌────────────┴──────────┐
        │ dev-agent-claude-4711 │  │ dev-agent-codex-99    │
        │ no route out, and no  │  │ no route out, and no  │
        │ route to the other    │  │ route to the other    │
        └───────────────────────┘  └───────────────────────┘
```

One long-lived proxy container serves every agent session on the host, and it is not
a per-session sidecar: a shared, `restart: unless-stopped` proxy is up before the
first session and stays up after the last, so nothing has to start it in the hot
path or stop it afterwards.

**One network per container, not one shared network.** A docker bridge has
container-to-container traffic enabled by default, so two agents attached to one
network can reach each other by IP and by container name, and `internal: true` does
not change that — it removes the route out, not the reachability of the neighbours.
Two agents running at once are two separate sessions, frequently on two different
projects, and one reaching the other's listening ports is a channel this document
would otherwise have to list in section 4.

Turning docker's own switch off is not the fix. A network created with
`com.docker.network.bridge.enable_icc=false` drops agent-to-proxy traffic along with
agent-to-agent — measured on docker 29.8.1, where a container on such a network
could reach neither a second container nor the proxy — and the proxy is the only
thing on the network worth reaching, so that setting buys isolation by removing the
feature.

So `dev-agent` creates an `internal: true` network per container, named after it, and
attaches the proxy to it. The cost is one `docker network create` and one
`docker network connect` per session, and a teardown: the wrapper therefore runs
`docker run` rather than `exec`-ing it, and removes the network from an `EXIT` trap.
A session killed with `SIGKILL` leaves its network behind, which is why the next run
under the same name removes it first.

Nothing else changes. The proxy's name resolves on every network it joins, so
`http://dev-agent-proxy:3128` is the same string it always was; the reverse lookup
of section 10.1 still answers `<container>.<network>` from the proxy's side; and the
subnet guard of section 11.3 derives the trusted subnet from the default route,
which only `egress` carries, so an extra interface per session is untrusted by
construction.

### 5.1 proxy/compose.yml

Lives in `proxy/` with the rest of the proxy, installed to
`~/.config/dev-agent/compose.proxy.yml` by `make -C proxy install` so that
`dev-agent` works without the repository checked out.

It carries `image:` and deliberately **no** `build:`. A `build: .` would resolve
against the installed file's own directory, where `proxy/` does not exist, so the
installed copy would be unusable the moment compose needed to build — which is
exactly the first run. Building is `proxy/Makefile`'s job instead, and its `install`
target depends on `build`, so the image is always there before `up -d` looks for it.

```yaml
name: dev-agent            # pins the network name to dev-agent_agents

services:
  proxy:
    image: dev-agent-proxy  # built by proxy/Makefile; no build: here, see above
    container_name: dev-agent-proxy
    restart: unless-stopped
    ports:
      - "127.0.0.1:8099:8099"
    volumes:
      - ${DEV_AGENT_PROXY_STATE:-~/.local/share/dev-agent/proxy}:/state
    networks: [egress]

networks:
  egress: {}
```

`egress` is the only network here. The internal networks are per container and
outlive neither the session nor compose's knowledge of them, so they are
`dev-agent`'s to create and remove, not compose's. The project name is pinned so
that `docker compose -p dev-agent` and the container name `dev-agent-proxy` are the
same on every run, whatever directory the installed compose file is invoked from.

### 5.2 State directory

`/state` inside the container, `~/.local/share/dev-agent/proxy` on the host:

```
ca/                      mitmproxy's confdir, holding the CA private key.
                         Never mounted into an agent container.
agent-ca/ca.crt          the CA certificate alone, a copy of
                         ca/mitmproxy-ca-cert.pem
agent-ca/bundle.crt      an agent image's trust store with ca.crt appended
rulesets/*.yml           one file per ruleset, hand-editable and git-able
categories.yml           host pattern to category map
settings.json            current mode, pending timeout
```

On first start the proxy copies the default ruleset and category map in if
`rulesets/` is empty. It never overwrites a file the user has edited.

#### Two directories, because mitmproxy owns the first

The CA is not generated by any code of ours. `mitmproxy` is started with
`--set confdir=/state/ca` and generates its own key material there, under its own
names: `mitmproxy-ca.pem` is the certificate **and the private key in one file**,
`mitmproxy-ca-cert.pem` is the certificate alone. Inventing `ca.crt`/`ca.key` for
those would mean either generating the CA ourselves and handing mitmproxy a `ca_file`
to adopt, or copying files both ways on every start. Neither buys anything, so the
confdir keeps mitmproxy's names and stays mitmproxy's business.

`agent-ca/` exists so that the bind mount of section 7 step 5 can be a whole
directory without also handing the agent `mitmproxy-ca.pem` and with it the CA
private key. Section 4 states that the key never leaves the proxy's state directory;
mounting the confdir would make that statement false. The proxy copies
`ca/mitmproxy-ca-cert.pem` to `agent-ca/ca.crt` whenever the former is newer, and
copies nothing else.

#### Permissions

Agent containers run as the invoking user's uid (`--user $(id -u):$(id -g)`), which
is not the uid the proxy writes `/state` as, and a read-only bind mount does not
relax file permissions. So the modes are part of the design, not an afterthought:

| Path | Mode | Why |
|---|---|---|
| `/state/ca` | `0700` | contains the private key; only the proxy reads it |
| `/state/agent-ca` | `0755` | traversed by an arbitrary host uid |
| `/state/agent-ca/*.crt` | `0644` | read by an arbitrary host uid |

If the state directory does not exist when compose first starts, docker creates it
owned by root. The proxy therefore sets these modes on every start rather than only
at creation, and `dev-agent` fails with a clear message rather than launching an
agent that cannot read the bundle it was told to trust.

#### `bundle.crt` comes from an agent image, not from the proxy image

`bundle.crt` is a system trust store with `ca.crt` appended, and *which* trust store
is not a detail. The proxy image's `/etc/ssl/certs/ca-certificates.crt` is not the
`dev-agent` image's — different distribution, different update cadence, different set
of roots. Since `SSL_CERT_FILE` and its siblings *replace* the trust store rather
than adding to it, a bundle assembled from the proxy's roots silently changes which
certificate authorities the agent trusts for every host the proxy tunnels rather than
intercepts. Nothing would report that, and the agent would be the one to break.

The bundle is assembled from the image the agent will actually run:

```bash
docker run --rm "$IMAGE" cat /etc/ssl/certs/ca-certificates.crt
```

The proxy container has no docker socket and cannot do this, so it belongs to
`dev-agent`, as step 3a of section 7. It runs on every `--proxy` start rather than
being cached against `ca.crt`'s mtime and `$IMAGE`'s id: the command costs a fraction
of a second, `$IMAGE` varies per run through `DEV_AGENT_IMAGE`, and a stale trust
store is a failure that surfaces days later as one host mysteriously failing to
verify. Regenerating unconditionally has no staleness to reason about.

## 6. Implementation

`mitmproxy` with a Python addon, in one container and one process.

Full TLS interception means owning certificate generation, HTTP/2, websockets,
chunked and streaming bodies, and connection reuse. `mitmproxy` has all of that,
plus an async addon API whose `request` hook can hold a flow — which section 8.3
needs. Everything genuinely specific to this project (the policy engine, the
categoriser, the API and the UI) is a few hundred lines on top.

```
proxy/
  Makefile                build, install and uninstall; see section 5.1
  compose.yml             the compose file of section 5.1
  Dockerfile              mitmproxy base, plus the vendored Vue build
  addon.py                mitmproxy hooks; wires everything together
  policy.py               modes, rulesets, match order, the pending queue
  categories.py           host/path to category lookup
  api.py                  the HTTP API of section 11, on :8099 and :8098
  log.py                  the record of section 10: stdout JSONL, ring buffer, SSE fan-out
  ui/index.html           the Vue 3 single page
  ui/vue.global.prod.js   vendored, so the UI works with no internet
  defaults/default.yml    the starter ruleset
  defaults/categories.yml the starter category map
  skills/egress-policy/SKILL.md   the text of section 13
  SPEC.md                 this file
  VALIDATION.md           written by phase 2
```

The API server runs inside mitmproxy's own asyncio loop, started from the addon's
`running` hook. There is therefore no IPC: the pending queue, the ring buffer and the
loaded rulesets are plain objects shared by both halves of the process.

Rulesets and `categories.yml` are re-read when their mtime changes, so editing a file
by hand has the same effect as editing it from the UI.

## 7. CLI surface

One new flag on `dev-agent`:

```
    --proxy     Route all network access through the dev-agent proxy and cut
                off direct internet access. Starts the proxy if it is not
                already running; its web UI is at http://127.0.0.1:8099.
```

Parsed in the `take_flags` case block alongside `--ro`, `--mirror`, `--workspace` and
`--host-network`. Its effects, in the order the script applies them:

1. `--proxy` together with `--host-network` is an error. The host network namespace
   has direct internet access by definition, so the combination is a contradiction
   rather than a layering.
2. Before the `exec`, bring the proxy up:
   `docker compose -p dev-agent -f "$compose_file" up -d`, where `$compose_file` is
   `${DEV_AGENT_COMPOSE:-$HOME/.config/dev-agent/compose.proxy.yml}`. Idempotent and
   fast when it is already running. Failure is fatal — `--proxy` must never silently
   degrade to unfiltered access, and a missing compose file means
   `make -C proxy install` has not been run.
3. Wait for `agent-ca/ca.crt` to exist, briefly, for the very first run where the CA
   is still being generated.
3a. Write `agent-ca/bundle.crt`: the trust store of `$IMAGE` with `agent-ca/ca.crt`
   appended, per section 5.2. Unconditionally, every run.
4. Network: create `internal: true` network `<container name>-net`, attach
   `dev-agent-proxy` to it, and run with `--network <container name>-net` instead of
   the default bridge — one per container, per section 5. Remove it again from an
   `EXIT` trap, which means `docker run` and not `exec docker run`; the exit status
   is docker's either way. `--hostname` is still passed, as it is in the
   non-`--host-network` path today.
5. Mount the certificate directory read-only:
   `--mount type=bind,src=<state>/agent-ca,dst=/etc/dev-agent-ca,readonly`.
   `<state>/agent-ca`, never `<state>/ca` — the latter holds the CA private key.
6. Add to the environment block, which today passes only `HOME` and `TERM`:

   | Variable | Value |
   |---|---|
   | `HTTP_PROXY`, `http_proxy` | `http://dev-agent-proxy:3128` |
   | `HTTPS_PROXY`, `https_proxy` | `http://dev-agent-proxy:3128` |
   | `NO_PROXY`, `no_proxy` | `localhost,127.0.0.1,dev-agent-proxy` |
   | `SSL_CERT_FILE` | `/etc/dev-agent-ca/bundle.crt` |
   | `REQUESTS_CA_BUNDLE` | `/etc/dev-agent-ca/bundle.crt` |
   | `CURL_CA_BUNDLE` | `/etc/dev-agent-ca/bundle.crt` |
   | `GIT_SSL_CAINFO` | `/etc/dev-agent-ca/bundle.crt` |
   | `CARGO_HTTP_CAINFO` | `/etc/dev-agent-ca/bundle.crt` |
   | `PIP_CERT` | `/etc/dev-agent-ca/bundle.crt` |
   | `NODE_EXTRA_CA_CERTS` | `/etc/dev-agent-ca/ca.crt` |
   | `NODE_OPTIONS` | `--use-env-proxy` |

   The bundle is a system trust store — the agent image's, per section 5.2 —
   **with** the interception CA appended, not the CA alone: several of these
   variables replace the trust store rather than adding to it, and pointing them at
   a lone certificate would break every host the proxy tunnels rather than
   intercepts. `NODE_EXTRA_CA_CERTS` is the exception —
   Node appends it, so it takes the bare certificate.

   The certificate cannot simply be installed into `/usr/local/share/ca-certificates`
   and `update-ca-certificates` run, because the container's root filesystem is
   read-only. Environment variables are the only mechanism available.

   `NODE_OPTIONS=--use-env-proxy` is not about certificates, and it is not optional.
   **Node reads neither `HTTP_PROXY` nor `HTTPS_PROXY` on its own** — not in
   `fetch`/undici and not in the core `http`/`https` modules. Without the flag a node
   process ignores the proxy, connects directly, and on this network fails at DNS:

   ```
   $ node -e 'fetch("https://example.com")'
   fetch failed |cause: getaddrinfo EAI_AGAIN example.com
   ```

   Which is the worst possible error message for the situation, since it names DNS
   and points at nothing that is actually wrong. `--use-env-proxy` fixes both `fetch`
   and `http`/`https`, and was measured to do so (VALIDATION.md, assumption 2). It
   requires **Node 24 or newer**; an older node fails on the unknown option, so an
   image carrying Node 23 or earlier cannot use this mechanism and needs a proxy
   agent inside the application instead. The base image has Node 24.21.

7. Tell the agent, by whichever mechanism the agent has. `${agent_home}` is
   bind-mounted as `/home/agent` and is the only writable path into an agent's
   configuration, but what to write there differs per agent, and writing Claude's
   layout for `codex` tells `codex` nothing at all:

   | Agent | What `dev-agent` writes |
   |---|---|
   | `claude` | `${agent_home}/.claude/skills/egress-policy/SKILL.md`, overwritten each run so it tracks the installed version |
   | `codex` | a marked block in `${agent_home}/.codex/AGENTS.md` |
   | `bash`, `tmux` | nothing; there is no agent to inform |

   `codex` has no skills directory and reads `AGENTS.md`, which is a file the user
   may well have their own content in. So the block is delimited and replaced in
   place rather than the file being overwritten — the same technique, and the same
   marker convention, as the `aliases` target in the root `Makefile`:

   ```
   # >>> dev-agent egress policy >>>
   ...the content of section 13...
   # <<< dev-agent egress policy <<<
   ```

   Neither mechanism is load-bearing. Section 13 explains why the `403` body is.

The proxy installs separately from the wrapper. The root `make install` continues to
install only `dev-agent` and `config.yml`; `make -C proxy install` builds the
`dev-agent-proxy` image and installs `compose.proxy.yml` and the skill source into
`~/.config/dev-agent/`. Without that second step `--proxy` fails at step 2 with a
missing compose file, which is the correct failure — fatal, not degraded.

Without `--proxy`, nothing changes: same network, same environment, same behaviour.

### 7.1 Logging in happens outside the proxy

Both agents authenticate with a browser flow that redirects to a loopback listener
inside the container. That flow cannot work under `--proxy`: the host's browser
cannot reach a port on a container attached only to an internal network, and
`--host-network`, which is what makes the callback reachable today, is refused
together with `--proxy` for the reason given above.

This is not a problem to solve, because credentials are persistent. `/home/agent` is
bind-mounted from the host and shared by every session, so an agent that has logged
in once stays logged in. The supported sequence is:

```bash
dev-agent --host-network claude    # once: /login, then exit
dev-agent --proxy claude ~/project # from then on
```

The same applies to `codex`. Log in once without `--proxy`, then work behind it
forever. Re-authentication is needed only when a token expires or the agent home is
deleted, at which point the same one-off applies.

Under `--proxy`, an agent that has never logged in will fail at authentication rather
than hang. `dev-agent` should therefore say so plainly when `--proxy` is combined with
`--host-network`:

```
--proxy and --host-network are mutually exclusive: the host network has direct
internet access. Log in once with --host-network, then use --proxy.
```

The README documents this as the first step of using `--proxy`, not as a caveat
buried at the end.

## 8. Policy model

Three concepts. Adding a fourth should require an argument.

### 8.1 Mode

One global value, changed from the UI, effective on the next request. No restart.

| Mode | Behaviour |
|---|---|
| `lockdown` | Deny everything. Nothing reaches the network, no prompting. |
| `default-deny` | Allow what an enabled ruleset allows. Anything else is **held** and raised to the operator (8.3). |
| `default-allow` | Allow everything except what an enabled ruleset denies. |
| `log-only` | Allow and log everything. Observation only. |

The default on a fresh install is `default-deny`, with the starter ruleset enabled,
because a mode that silently allows would make the feature pointless for the person
who asked for it.

### 8.2 Rulesets and rules

A ruleset is one YAML file in `/state/rulesets/`:

```yaml
# rulesets/default.yml
name: default
description: Everything a normal development container needs
enabled: true
rules:
  - match: api.anthropic.com
    action: allow
    note: Claude API
  - match: "*.ubuntu.com"
    action: allow
    note: apt
  - match: registry.npmjs.org
    path: /@internal/*
    action: deny
    note: never fetch the private scope from here
```

A rule is `{ match, path?, action, note? }`:

- `match` — the host. A glob (`*.example.com`), a CIDR (`10.0.0.0/8`, `fd00::/8`),
  or a regular expression when written as `/.../`. Matched case-insensitively against
  the hostname only, never the port. A CIDR matches only a request made to a literal
  IP address in that range; a name is never resolved to compare it.
- `path` — optional glob against the request path. Absent means any path. Under a
  `tunnel` rule, or for a request the proxy is not intercepting, the path is unknown,
  and a rule carrying `path` therefore cannot match.
- `action` — `allow`, `deny` or `tunnel`. `tunnel` allows the connection but skips
  TLS interception, for pinned clients; the request is logged with the host only.
- `note` — free text, shown in the UI and in the log record's `rule` field.

Evaluation order, first match wins:

1. `lockdown` short-circuits to deny.
2. `deny` rules, across all enabled rulesets, in filename order then file order.
3. `tunnel` rules, same order.
4. `allow` rules, same order.
5. The mode default: allow under `default-allow` and `log-only`, hold under
   `default-deny`.

Deny before allow means a deny rule in any enabled ruleset cannot be overridden by an
allow rule somewhere else — the conservative direction.

Disabled rulesets are parsed and shown in the UI but take no part in evaluation.

The starter `default.yml` covers what a development container needs to function:
the Anthropic and OpenAI APIs, Ubuntu and NodeSource apt repositories, the npm,
PyPI, crates.io, RubyGems, Go module and Maven Central registries, and
`github.com`/`codeload.github.com`.

### 8.3 The pending queue

Under `default-deny`, a request matching no rule is **held**: the proxy does not
answer it and does not forward it, and it appears in the UI's pending pane with its
method, full URL, category, client container and how long it has been waiting.

The operator chooses:

- **Allow once** — every request currently attached to this entry proceeds, and the
  entry is forgotten; the next one asks again.
- **Deny once** — every attached request is answered with `403` and an explanatory
  body, and the next one asks again.
- **Always Allow**, **Always Deny** — the same decision, and a rule carrying it is
  appended to a chosen ruleset, either `host` scope (`match: <host>`) or `host+path`
  scope (`match: <host>`, `path: <dir>/*`).

The ruleset and the scope are one pair of controls shared by both saves: where the
rule goes and how wide it is are the same question whichever way the answer falls,
and asking it twice would put two sets of pickers in a row that is already busy.

If nobody answers within the timeout (default **30 seconds**, in `settings.json`),
the request is denied. A headless or unattended run therefore fails rather than hangs
forever. 30 rather than a rounder 60 because 30 is the figure section 14.3 validates;
shipping a default longer than the one that was tested is how a spec grows a bug that
only appears in front of a user.

### One entry per question, not per request

A held request is not its own queue entry. Entries are keyed by
`(method, host, path)` — the **ask key** — and a flow whose key already has an entry
attaches to it as another waiter rather than creating a second one. One decision
resolves every waiter attached at that moment.

Without this the queue is unusable the first time it matters. A single
`npm install` opens dozens of connections to one registry; `apt-get update` fetches
a score of index files from one host. Per-request entries would put twenty rows in
front of the operator asking twenty times about one thing, each with its own 30
second fuse, and the honest description of that feature is that it does not work.

The consequences, all of them deliberate:

- The UI shows one row per ask key with a count of waiters, rising while the operator
  reads it. The count is information — twenty parallel fetches of one host is a
  different thing from one.
- **The timeout is per waiter, not per entry.** Each flow is denied 30 seconds after
  *its own* arrival. A flow that attached 25 seconds into an entry's life gets its
  own 30 seconds, and a long-lived entry does not kill a request that just arrived.
  An entry disappears when its last waiter has been resolved or has timed out.
- Coalescing is by exact path, not by prefix. `path` is in the key because a rule can
  be saved at `host+path` scope, so two paths on one host are genuinely two
  questions. For a tunnelled `CONNECT` the path is unknown and the key is
  `(CONNECT, host, null)`, which coalesces every connection to that host — correct,
  since that is also the only scope a rule could be saved at. An *intercepted* HTTPS
  request is keyed on its inner request, not on its `CONNECT`: nothing is decided at
  `CONNECT`, because a `403` answered there is a body no client shows anyone. See
  VALIDATION.md, the correction under assumption 3.
- `id` in the log record stays per request. Each waiter is its own request with its
  own `id`, `ms` and `held_ms`; what they share is the decision and the ask key.

Holding a live flow is a hard requirement, not a nicety. It is what makes
"greenlight in real time" mean the request actually completes afterwards, rather than
the agent seeing a failure and the human fixing it for the next attempt. Section 14
validates that real clients tolerate the wait; if they do not, the fallback is to
deny immediately, record the attempt as pending, and rely on the agent's retry — a
strictly worse experience that must be chosen deliberately, not by accident.

## 9. Categorisation

`/state/categories.yml`, an ordered list, first match wins, fallback `unknown`:

```yaml
categories:
  - {pattern: "api.anthropic.com",   category: llm-api}
  - {pattern: "api.openai.com",      category: llm-api}
  - {pattern: "*.ubuntu.com",        category: os-pkg}
  - {pattern: "registry.npmjs.org",  category: lang-pkg}
  - {pattern: "*.pythonhosted.org",  category: lang-pkg}
  - {pattern: "github.com",          category: vcs}
  - {pattern: "*.amazonaws.com",     category: cloud}
  - {pattern: "*.wikipedia.org",     category: docs}
  - {pattern: "*.sentry.io",         category: telemetry}
```

Shipped categories: `llm-api`, `os-pkg`, `lang-pkg`, `vcs`, `cloud`, `docs`,
`telemetry`, `unknown`. Users may add their own simply by using a new name.

This is a lookup table and nothing more. No classifier, no heuristics, no inference
from traffic shape. A category that is wrong is fixed by editing one line.

## 10. Log record

One JSON object per request, on **stdout**, appended to `/state/log.jsonl`, and
pushed to connected UIs over SSE.
This schema is a contract: downstream `vector` and Elasticsearch configurations
depend on it, so fields are added but never renamed or repurposed.

`/state/log.jsonl` is what makes the record survive a restart: stdout goes to
docker's log, which a `compose down` takes with it, and the ring buffer the UI
seeds from is process memory. The file rotates at 64 MB with one older file
kept, and on start the ring is refilled from its tail, so the UI opens on the
requests that came before the restart rather than on nothing. A line left
half-written by a kill is skipped.

```json
{"ts":"2026-09-18T10:22:31.412Z","id":"01J8Z3...","client":"dev-agent-claude-4711",
 "method":"POST","scheme":"https","host":"api.anthropic.com","port":443,
 "path":"/v1/messages","status":200,"cat":"llm-api",
 "decision":"allow","rule":"default.yml:Claude API","mode":"default-deny",
 "bytes_up":8213,"bytes_down":91204,"ms":1840,"held_ms":0}
```

| Field | Meaning |
|---|---|
| `ts` | RFC 3339 UTC, millisecond precision, at request start |
| `id` | Unique per request. Not the pending-queue key — that is the ask key of section 8.3, and several requests can share one |
| `client` | Source container name, resolved once per connection (see 10.1); the source IP if that fails |
| `method`, `scheme`, `host`, `port`, `path` | The request. `method` and `path` are `null` for a tunnelled connection |
| `status` | Upstream response status; `null` if denied or failed |
| `cat` | Category from section 9 |
| `decision` | `allow`, `deny`, `tunnel` |
| `rule` | `<ruleset file>:<note>`, or `mode:<mode>` when the default decided, or `pending:<decision>` when a human did |
| `mode` | Mode in force at decision time |
| `bytes_up`, `bytes_down` | Body bytes, excluding headers |
| `ms` | Total request duration |
| `held_ms` | Time spent waiting for a human; `0` when not held |

### 10.1 Resolving `client`

The name comes from a reverse lookup of the peer address against docker's embedded
resolver, and it is done **once per connection**, in the addon's `client_connected`
hook, then carried on the connection for every request on it.

Docker answers with the network appended — `dev-agent-claude-4711.dev-agent-claude-4711-net`,
not `dev-agent-claude-4711`. `client` is the first label only. A container name
cannot contain a dot, so taking everything before the first one is exact rather than
a guess, and the network is a constant that would be noise in every record and in
every Loki label built from one. Not once per request:
`socket.gethostbyaddr` is a blocking call, the addon runs inside mitmproxy's asyncio
loop, and a busy `npm install` would put a synchronous DNS round trip in front of
every one of a few hundred requests on a handful of connections.

Per connection rather than a cache keyed on the IP, because a cache would need
invalidating and this does not. Docker reuses container addresses, so a cached
`172.19.0.4 -> dev-agent-claude-4711` outlives the container and then labels a
different session's traffic with a dead container's name — silently, and in the one
field an operator would use to tell two agents apart. A connection cannot outlive
the container that opened it, so resolving at connection time has nothing to go
stale.

The lookup still runs in an executor rather than inline, so a slow or unreachable
resolver delays one connection's first record instead of stalling the loop, and a
failure falls back to the IP.

Shipping it onward is configuration, not code. Documented in the README:

```toml
# vector.toml
[sources.dev_agent_proxy]
type = "docker_logs"
include_containers = ["dev-agent-proxy"]

[transforms.parsed]
type = "remap"
inputs = ["dev_agent_proxy"]
source = '. = parse_json!(.message)'

[sinks.loki]
type = "loki"
inputs = ["parsed"]
endpoint = "http://localhost:3100"
labels = {job = "dev-agent-proxy", host = "{{ host }}", cat = "{{ cat }}"}
encoding.codec = "json"
```

## 11. HTTP API

Two ports, because the API has two audiences whose trust levels are opposite. The
operator must be able to change policy. The agent must never be able to, or the whole
mechanism is theatre: an agent that can `PUT /api/mode` with `{"mode": "log-only"}` has
switched off its own sandbox with one HTTP call.

| Port | Published | Serves | Audience |
|---|---|---|---|
| `8099` | `127.0.0.1:8099` | the web UI and the full API below | the operator, in a browser on the host |
| `8098` | not published | the read-only subset of 11.2 | agent containers, via `http://dev-agent-proxy:8098` |

### 11.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/policy` | Mode, enabled rulesets, effective allow and deny lists. Also served read-only on `8098`; the endpoint the agent's skill uses. |
| `GET` | `/api/events` | SSE. Events: `request` (a log record), `pending` (a new held request), `resolved` (a held request decided), `state` (mode or rulesets changed), `purge` (records cleared, with the cutoff so every open UI trims the same rows). |
| `GET` | `/api/log?since=&host=&cat=&decision=` | Recent records from the ring buffer (5000 entries). |
| `DELETE` | `/api/log?seconds=` | Purge records older than `seconds`, or all of them when it is absent or `0`. Clears the ring and rewrites `/state/log.jsonl`, so a restart does not bring back what was cleared. The age is resolved against the proxy's clock, not the UI's. Operator only, like every other write. |
| `GET` | `/api/mode` | Current mode. |
| `PUT` | `/api/mode` | `{"mode": "..."}`. |
| `GET` | `/api/rulesets` | Every ruleset with its enabled state, rule count and description. |
| `GET` | `/api/rulesets/<name>` | One ruleset, with its rules. |
| `PUT` | `/api/rulesets/<name>` | `{"enabled": true \| false}`. |
| `DELETE` | `/api/rulesets/<name>/rules?index=` | Remove one rule by its position in the file. The other rules and the file's comments are left as they were; the comments written under the deleted rule go with it. Not undoable, so the UI asks first. |
| `GET` | `/api/pending` | Currently held asks, each with its key, method, URL, category, clients and waiter count. |
| `POST` | `/api/pending/<key>` | `{"decision":"allow" \| "deny", "save_to":"<ruleset>", "scope":"host" \| "host+path", "enable":true}`. `<key>` is the ask key of section 8.3, so one call resolves every request waiting on it. `enable` switches the disabled allow rule the entry reports in `disabled_allow` (and its ruleset, if that is what is off) back on instead of saving a second rule for the same host. |

### 11.2 The read-only subset

Three endpoints, and nothing else:

- `GET /api/policy`
- `GET /api/mode`
- `GET /api/rulesets`

This is what port `8098` serves, and it is what the agent's skill (section 13) is
pointed at. Everything else on `8098` is `404`. The agent may see the policy; it may
never change it.

### 11.3 Why a subnet check, and not a loopback check

Both ports are also reachable from the agent's own network, because the proxy is
attached to it — it has to be, or it could not proxy anything. Port `8098`
existing does not by itself stop an agent from opening `http://dev-agent-proxy:8099`
and calling `PUT /api/mode`. So `8099` carries a source-address guard as well.

The obvious guard is wrong. Traffic arriving through a published port is SNAT'd by
docker, so its peer address is a bridge gateway address, **not** `127.0.0.1`. A check
of the form `if peer == "127.0.0.1"` locks the operator out of their own UI while
letting nothing else through.

The guard is therefore inverted — deny by subnet rather than allow by loopback:

1. At startup the proxy finds the interface carrying the default route, and derives
   that interface's subnet — for example `172.22.0.0/16`. One read of
   `/proc/net/route` and one of the interface's address and netmask; no docker socket
   and no name lookup.
2. A peer inside that subnet is **trusted**. Every other peer is **untrusted**: it is
   served the 11.2 subset and `403` for everything else, exactly as if it had arrived
   on `8098`.
3. That is the right way round because of how the two kinds of network differ. An
   agent network is `internal: true` and so has **no gateway**, which means it
   cannot carry the default route; `egress` does. The published port SNATs the operator's traffic to
   the `egress` bridge gateway, which is in the `egress` subnet by construction. So
   "the default route's subnet" is exactly "where the operator arrives from", and it
   is derived rather than named:

   ```
   default via 172.22.0.1 dev eth0            <- egress, operator arrives from .0.1
   172.22.0.0/16 dev eth0 scope link  src 172.22.0.2
   172.23.0.0/16 dev eth1 scope link  src 172.23.0.2   <- an agent, no gateway
   ```

   Identifying the agent interface directly is the thing to avoid. Docker assigns
   `eth0` and `eth1` in an order the container cannot rely on and neither name says
   which network it belongs to, so a proxy that hardcodes one, or guesses, is one
   restart away from trusting the agent. Deriving the trusted subnet from the default
   route needs no such guess, and it has to stay correct as interfaces come and go,
   because one network per agent session means the proxy gains and loses an
   interface at every one: a new interface is untrusted until something gives it the
   default route.

**Fail closed.** If there is no default route at startup, or its interface has no
address, no subnet is trusted: every peer is untrusted and the proxy logs the
condition prominently. The operator then finds a read-only UI, which is visibly
broken and fixable; the alternative failure is invisible. A proxy
that serves the operator read-only is an inconvenience; one that serves the agent
read-write is the hole this whole document exists to close.

The agent cannot forge its way past this. `dev-agent` runs the container with
`--cap-drop=ALL`, so there is no `CAP_NET_RAW` to build a spoofed packet with, and a
spoofed source address would send the handshake's replies to the address that was
forged rather than back to the agent.

The two mechanisms are deliberately redundant. Port `8098` means the agent's
documented path never touches code that can mutate anything; the subnet guard on
`8099` is the backstop for the undocumented path.

## 12. Web UI

Vue 3, one page, three panes.

- **Live log.** Newest first, streaming from `/api/events`, seeded from `/api/log`.
  Purge buttons — older than 5 minutes, an hour, a day, or everything — call
  `DELETE /api/log`. Only *everything* asks for confirmation: an age cutoff is
  the ordinary way to keep the panel readable, while clearing the lot is the one
  that can throw away the record of something an operator has not looked at yet.
  Filter by host, category and decision; click a row for the full record.
- **Pending.** One row per ask key, not per request: method, URL, category, client,
  the elapsed time of the longest waiter and the waiter count when it is above one.
  Then the three buttons of section 8.3 with a ruleset picker for "allow and save",
  each acting on the whole entry. This pane takes the top of the page when non-empty
  — it is the one part of the UI someone is actually waiting on.
- **Rulesets and mode.** The mode as four radio options, each ruleset as a toggle
  with its description and rule count, expandable to show the rules.

No build step. The Vue 3 global build is vendored into the image as
`ui/vue.global.prod.js` and loaded by one `index.html`; components are plain
JavaScript objects. The proxy image has no Node toolchain, the UI is three panes, and
a vendored copy keeps the UI usable even in `lockdown`. If it outgrows that, add
Vite and single-file components as a build stage in `proxy/Dockerfile`; nothing else
in this design changes.

## 13. Telling the agent

Three things need to reach whatever is inside the container:

1. Network access is filtered, and a failed fetch may be policy rather than a bug.
2. `curl -s http://dev-agent-proxy:8098/api/policy` returns the current mode and the
   allowed and denied hosts.
3. The agent cannot change the policy. A human can, from the UI at
   `http://127.0.0.1:8099`, and asking is the correct response to a blocked host.

### 13.1 The `403` body is the mechanism that always works

Configuration files are per-agent and best-effort: `claude` has skills, `codex` has
`AGENTS.md`, `curl` and `pip` and `npm` have nothing, and a future agent has whatever
it has. The one channel every client shares is the response to the request that was
refused, so that is where the explanation goes and the rest is an optimisation.

Every `deny`, whether by rule or by a pending request timing out, answers with `403`
and a plain-text body carrying all three points:

```
403 Forbidden

dev-agent proxy: egress to gitlab.example.com is not allowed.

  mode     default-deny
  decision rule default.yml:corporate hosts
  policy   curl -s http://dev-agent-proxy:8098/api/policy

This is policy, not a network fault, and retrying will not change it. You cannot
change the policy from in here. A human can, at http://127.0.0.1:8099 — asking
them is the right next step.
```

Plain text, not JSON: its only reader is something that will show it to a human or
to a model, and both read prose. The `Content-Type` is `text/plain; charset=utf-8`.

The body names the rule that decided, because "denied" and "denied *by this line of
this ruleset*" are different amounts of help, and the second one costs nothing to
include.

### 13.2 The skill, and the `AGENTS.md` block

`skills/egress-policy/SKILL.md` for `claude`, and the same text as a marked block in
`.codex/AGENTS.md` for `codex`, installed per section 7 step 7. Both say the three
things above. Their only advantage over 13.1 is timing: the agent knows before it
wastes a turn on a fetch that was never going to work.

Deliberately small, and deliberately not required. They exist so the agent stops
guessing, not so it gains a capability, and an agent that never reads either still
learns everything it needs from the first `403`.

## 14. Assumptions to validate before building

Recorded in `VALIDATION.md`, measured by `spike/check.sh`. In descending order of how
much damage a wrong answer does. Current state: all four confirmed on docker 29.8.0
and mitmproxy 12.2.3, each with an amendment to this document. 1, 2 and 4 by
`spike/check.sh`; 3 against the built proxy, and not yet in the spike.

1. **Isolation isolates.** A container on the `internal` network cannot reach the
   internet directly, and can reach it through the proxy. Includes measuring what DNS
   still resolves, to confirm or correct section 4.
2. **The agents survive interception.** `claude` and `codex` both complete a turn
   through an intercepting proxy, with the CA wired as in section 7, on a read-only
   root filesystem. Likewise `apt-get update`, `npm install`, `pip install` and
   `git clone https://`. Both agents are logged in beforehand per section 7.1, so the
   OAuth loopback flow is out of scope here; what is being validated is that an
   *already authenticated* agent works through interception.

   The agents are the point of this assumption and the tooling is the easy half of
   it. A spike that checks `curl`, `python3`, `node` and `git` and stops there has
   validated the half that was never in doubt: those clients read
   `SSL_CERT_FILE` and behave. What is genuinely unknown is whether an agent's own
   HTTP stack — bundled, possibly pinned, streaming a long response — tolerates
   interception, and it is also the assumption whose failure kills the feature. It is
   not validated until `claude -p` and `codex exec` have each completed a turn
   through the proxy.
3. **A flow can be held.** A mitmproxy addon can await a decision in its `request`
   hook, and real clients tolerate a wait of the full section 8.3 timeout — 30
   seconds — rather than timing out. Measure the agents here too, not only `curl`:
   an agent that gives up at 10 seconds turns the pending queue into a way of
   failing slowly. If they do not tolerate it, section 8.3's fallback applies.
4. **Reverse DNS yields a usable `client`.** If the docker resolver does not map the
   container IP back to its name, the `client` field is the IP and section 10 is
   amended. Also measure how long the lookup takes when it fails, since section 10.1
   spends that once per connection and a multi-second failure is a multi-second delay
   on a first request.
