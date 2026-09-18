# dev-agent egress proxy — specification

Status: **draft, not yet validated**. Section 14 lists the assumptions that
`VALIDATION.md` must confirm before any of this is built.

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
has no gateway to the outside. The `HTTP_PROXY`/`HTTPS_PROXY` environment variables
are a *convenience*, telling well-behaved clients where the proxy is. If the agent
unsets them, it gets no network, not free network. The failure mode is closed.

That leaves the following open, and the documentation must say so rather than imply
a completeness that does not exist.

**DNS.** Containers on an internal network still reach docker's embedded resolver at
`127.0.0.11`, and the daemon forwards queries it cannot answer to the host's
resolvers. A DNS-tunnelling client can therefore still move data out, slowly. This
is accepted for v1. The upgrade path is to give the agent container a stub resolver
that only knows the proxy's name, or to skip DNS entirely by passing the proxy's
address as an IP; both are cheap to add later and neither is needed to make the
common case work.

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
(section 7), which is why the blast radius stops at the container.

**Localhost within the container** is unaffected, and so is container-to-proxy
traffic. An agent can still talk to itself.

## 5. Architecture

```
                    ┌──────────────────────────────────────┐
  host 127.0.0.1    │  dev-agent-proxy                     │
  :8099  ───────────┤    mitmproxy          :3128          ├────── internet
  (web UI + API)    │    web UI + API       :8099          │      (network: egress)
                    └──────────────┬───────────────────────┘
                                   │
                        network: agents (internal: true)
                                   │
                    ┌──────────────┴───────────────────────┐
                    │  dev-agent-claude-4711               │
                    │  no route to the internet            │
                    └──────────────────────────────────────┘
```

One long-lived proxy container serves every agent session on the host. It is not a
per-session sidecar, and that is a constraint rather than a preference: `dev-agent`
ends with `exec docker run ...`, so the wrapper process does not survive the
container and has nowhere to hang teardown logic. A shared, `restart: unless-stopped`
proxy needs no teardown.

### 5.1 compose.proxy.yml

Lives at the repository root, installed to `~/.config/dev-agent/compose.proxy.yml` so
that `dev-agent` works without the repository checked out.

```yaml
name: dev-agent            # pins the network name to dev-agent_agents

services:
  proxy:
    build: ./proxy
    image: dev-agent-proxy
    container_name: dev-agent-proxy
    restart: unless-stopped
    ports:
      - "127.0.0.1:8099:8099"
    volumes:
      - ${DEV_AGENT_PROXY_STATE:-~/.local/share/dev-agent/proxy}:/state
    networks: [egress, agents]

networks:
  egress: {}
  agents:
    internal: true
```

The project name is pinned so that `dev-agent` can name the network
`dev-agent_agents` without asking compose for it.

### 5.2 State directory

`/state` inside the container, `~/.local/share/dev-agent/proxy` on the host:

```
ca/ca.crt          the interception CA certificate, mounted into agent containers
ca/ca.key          its private key, never leaves this directory
ca/bundle.crt      the system CA bundle with ca.crt appended
rulesets/*.yml     one file per ruleset, hand-editable and git-able
categories.yml     host pattern to category map
settings.json      current mode, pending timeout
```

On first start the proxy generates the CA if `ca/ca.key` is absent and copies the
default ruleset and category map in if `rulesets/` is empty. It never overwrites a
file the user has edited.

## 6. Implementation

`mitmproxy` with a Python addon, in one container and one process.

Full TLS interception means owning certificate generation, HTTP/2, websockets,
chunked and streaming bodies, and connection reuse. `mitmproxy` has all of that,
plus an async addon API whose `request` hook can hold a flow — which section 8.3
needs. Everything genuinely specific to this project (the policy engine, the
categoriser, the API and the UI) is a few hundred lines on top.

```
proxy/
  Dockerfile              mitmproxy base, plus the vendored Vue build
  addon.py                mitmproxy hooks; wires everything together
  policy.py               modes, rulesets, match order, the pending queue
  categories.py           host/path to category lookup
  api.py                  the HTTP API of section 9, on :8099
  log.py                  the record of section 10: stdout JSONL, ring buffer, SSE fan-out
  ui/index.html           the Vue 3 single page
  ui/vue.global.prod.js   vendored, so the UI works with no internet
  defaults/default.yml    the starter ruleset
  defaults/categories.yml the starter category map
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
   `docker compose -p dev-agent -f "$compose_file" up -d`. Idempotent and fast when
   it is already running. Failure is fatal — `--proxy` must never silently degrade to
   unfiltered access.
3. Wait for `ca/bundle.crt` to exist, briefly, for the very first run where the CA is
   still being generated.
4. Network: `--network dev-agent_agents` instead of the default bridge. `--hostname`
   is still passed, as it is in the non-`--host-network` path today.
5. Mount the CA read-only: `--mount type=bind,src=<state>/ca,dst=/etc/dev-agent-ca,readonly`.
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

   The bundle is the system trust store **with** the interception CA appended, not
   the CA alone: several of these variables replace the trust store rather than
   adding to it, and pointing them at a lone certificate would break every host the
   proxy tunnels rather than intercepts. `NODE_EXTRA_CA_CERTS` is the exception —
   Node appends it, so it takes the bare certificate.

   The certificate cannot simply be installed into `/usr/local/share/ca-certificates`
   and `update-ca-certificates` run, because the container's root filesystem is
   read-only. Environment variables are the only mechanism available.

7. Install the agent-facing skill (section 12) by copying it into
   `${agent_home}/.claude/skills/egress-policy/`, overwriting each run so it tracks
   the installed version. That directory is already bind-mounted into the container
   as `/home/agent`, and is the only writable path into the agent's configuration.

`make install` gains the job of installing `compose.proxy.yml` and the skill source
next to the existing `config.yml`.

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
| `bypass` | Allow and log everything. Observation only. |

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

- `match` — the host. A glob (`*.example.com`), or a regular expression when written
  as `/.../`. Matched case-insensitively against the hostname only, never the port.
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
5. The mode default: allow under `default-allow` and `bypass`, hold under
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

- **Allow once** — this request proceeds; the next identical one asks again.
- **Allow and save** — this request proceeds and a rule is appended to a chosen
  ruleset, either `host` scope (`match: <host>`) or `host+path` scope
  (`match: <host>`, `path: <dir>/*`).
- **Deny** — the request is answered with `403` and an explanatory body.

If nobody answers within the timeout (default 60 seconds, in `settings.json`), the
request is denied. A headless or unattended run therefore fails rather than hangs
forever.

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

One JSON object per request, on **stdout**, and pushed to connected UIs over SSE.
This schema is a contract: downstream `vector` and Elasticsearch configurations
depend on it, so fields are added but never renamed or repurposed.

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
| `id` | Unique per request, also the pending-queue key |
| `client` | Source container name, by reverse DNS on the docker network; the source IP if that fails |
| `method`, `scheme`, `host`, `port`, `path` | The request. `method` and `path` are `null` for a tunnelled connection |
| `status` | Upstream response status; `null` if denied or failed |
| `cat` | Category from section 9 |
| `decision` | `allow`, `deny`, `tunnel` |
| `rule` | `<ruleset file>:<note>`, or `mode:<mode>` when the default decided, or `pending:<decision>` when a human did |
| `mode` | Mode in force at decision time |
| `bytes_up`, `bytes_down` | Body bytes, excluding headers |
| `ms` | Total request duration |
| `held_ms` | Time spent waiting for a human; `0` when not held |

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

Served on `:8099`. Published to the host as `127.0.0.1:8099` and reachable from the
`agents` network as `http://dev-agent-proxy:8099`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/policy` | Mode, enabled rulesets, effective allow and deny lists. The endpoint the agent's skill uses. |
| `GET` | `/api/events` | SSE. Events: `request` (a log record), `pending` (a new held request), `resolved` (a held request decided), `state` (mode or rulesets changed). |
| `GET` | `/api/log?since=&host=&cat=&decision=` | Recent records from the ring buffer (5000 entries). |
| `GET` | `/api/mode` | Current mode. |
| `PUT` | `/api/mode` | `{"mode": "..."}`. |
| `GET` | `/api/rulesets` | Every ruleset with its enabled state, rule count and description. |
| `GET` | `/api/rulesets/<name>` | One ruleset, with its rules. |
| `PUT` | `/api/rulesets/<name>` | `{"enabled": true \| false}`. |
| `GET` | `/api/pending` | Currently held requests. |
| `POST` | `/api/pending/<id>` | `{"decision":"allow" \| "deny", "save_to":"<ruleset>", "scope":"host" \| "host+path"}`. |

**Requests arriving from the `agents` network get only the read-only subset**:
`/api/policy`, `/api/mode` (GET), `/api/rulesets` (GET). Everything else is `403`.
The agent may see the policy; it may never change it. This is enforced by source
address — the mutating endpoints are served only to the host-published listener.

## 12. Web UI

Vue 3, one page, three panes.

- **Live log.** Newest first, streaming from `/api/events`, seeded from `/api/log`.
  Filter by host, category and decision; click a row for the full record.
- **Pending.** Held requests with method, URL, category, client and elapsed time, and
  the three buttons of section 8.3 with a ruleset picker for "allow and save".
  This pane takes the top of the page when non-empty — it is the one part of the UI
  someone is actually waiting on.
- **Rulesets and mode.** The mode as four radio options, each ruleset as a toggle
  with its description and rule count, expandable to show the rules.

No build step. The Vue 3 global build is vendored into the image as
`ui/vue.global.prod.js` and loaded by one `index.html`; components are plain
JavaScript objects. The proxy image has no Node toolchain, the UI is three panes, and
a vendored copy keeps the UI usable even in `lockdown`. If it outgrows that, add
Vite and single-file components as a build stage in `proxy/Dockerfile`; nothing else
in this design changes.

## 13. Agent-facing skill

`skills/egress-policy/SKILL.md`, copied into the agent's home under `--proxy`. It
tells the agent three things:

1. Network access is filtered, and a failed fetch may be policy rather than a bug.
2. `curl -s http://dev-agent-proxy:8099/api/policy` returns the current mode and the
   allowed and denied hosts.
3. The agent cannot change the policy. A human can, from the UI at
   `http://127.0.0.1:8099`, and asking is the correct response to a blocked host.

The skill is deliberately small. It exists so the agent stops guessing, not so it
gains a new capability.

## 14. Assumptions to validate before building

Recorded in `VALIDATION.md`. In descending order of how much damage a wrong answer
does.

1. **Isolation isolates.** A container on the `internal` network cannot reach the
   internet directly, and can reach it through the proxy. Includes measuring what DNS
   still resolves, to confirm or correct section 4.
2. **The agents survive interception.** `claude` and `codex` both authenticate and
   complete a turn through an intercepting proxy, with the CA wired as in section 7,
   on a read-only root filesystem. Likewise `apt-get update`, `npm install`,
   `pip install` and `git clone https://`. Both agents are logged in beforehand per
   section 7.1, so the OAuth loopback flow is out of scope here; what is being
   validated is that an *already authenticated* agent works through interception.
3. **A flow can be held.** A mitmproxy addon can await a decision in its `request`
   hook, and real clients tolerate a 30-second wait rather than timing out. If not,
   section 8.3's fallback applies.
4. **Reverse DNS yields a usable `client`.** If the docker resolver does not map the
   container IP back to its name, the `client` field is the IP and section 10 is
   amended.
