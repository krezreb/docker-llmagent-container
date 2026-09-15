# docker-llmagent-container

Run the [Codex](https://github.com/openai/codex) and [Claude Code](https://github.com/anthropics/claude-code)
CLI agents inside a locked-down Docker container, with only one project directory
exposed to them.

The point is blast radius. An agent running on the host can read your SSH keys,
your browser profile and every repository you own. Here it sees one directory
and nothing else.

## What you get

- **Ubuntu 24.04 image** with both agents preinstalled globally via npm, plus the
  tooling they expect (git, curl, jq, ripgrep, python3, build-essential).
- **`dev-agent` wrapper script** that starts a container with a hardened set of
  Docker flags and bind-mounts exactly one project directory at `/workspace`.
- **Separate persistent home per command** under `~/.local/share/dev-agent/`, so
  login state survives between runs and neither agent can read the other's
  credentials.
- **A config file** for any extra host paths you want the agent to see.

## Install

```sh
make install
```

This builds the image (`dev-agent:ubuntu24`), installs the wrapper to
`~/.local/bin/dev-agent`, and writes a starter `~/.config/dev-agent/config.yml`
if you don't already have one. Make sure `~/.local/bin` is on your `PATH`.

To build the image alone:

```sh
make build-image
```

## Usage

```
dev-agent codex  [directory] [codex arguments...]
dev-agent claude [directory] [claude arguments...]
dev-agent bash   [directory] [bash arguments...]
```

`bash` drops you into a shell in the same sandbox, which is useful for
inspecting the image or running a command in the container by hand. To poke at
an agent's own home from that shell, mount it: `- ~/.local/share/dev-agent/codex:/codex`.

The directory argument is optional and defaults to the current directory. Any
remaining arguments are passed straight through to the agent.

```sh
dev-agent codex .
dev-agent claude .
dev-agent codex ~/src/foo
dev-agent claude ~/src/foo --model opus
dev-agent bash ~/src/foo
```

First run of each agent will prompt you to log in. The credentials are written
to that agent's persistent volume, so you only do this once.

## Sandbox details

Every container is started with:

| Flag | Effect |
| --- | --- |
| `--read-only` | Root filesystem is immutable. |
| `--cap-drop=ALL` | No Linux capabilities. |
| `--security-opt=no-new-privileges:true` | No privilege escalation through setuid binaries. |
| `--pids-limit=2048` | Basic fork-bomb protection. |
| `--user $(id -u):$(id -g)` | Files created in the project keep your ownership, not root's. |
| `--tmpfs /tmp`, `--tmpfs /run` | Writable scratch space (`nosuid,nodev`), discarded on exit. |
| `--rm` | Container is destroyed when the agent exits. |

Only two paths are writable and persistent:

- `/workspace` — the bind-mounted project directory.
- `/home/agent` — `~/.local/share/dev-agent/<command>` on the host, so `codex`,
  `claude` and `bash` each get their own.

The environment is not inherited from your shell. Only `HOME` and `TERM` are
passed in, so API keys and other secrets in your host environment stay on the
host.

The wrapper refuses to mount `/`, `/home`, `/root` or your home directory
outright, since mounting any of those would defeat the purpose.

## Configuration

`~/.config/dev-agent/config.yml` lists extra host paths to expose, one per line:

```yaml
mounts:
  - ~/.gitconfig:/home/agent/.gitconfig:ro
  - ~/.ssh:/home/agent/.ssh:ro
  - ~/src/shared-lib:/shared
```

Each entry is `source:destination[:ro]`. `~` is expanded, the source must
already exist, and `:ro` makes the mount read-only. Everything else in the file
is ignored, so only the `mounts:` list matters.

Mounting credentials such as `~/.ssh` gives the agent the ability to use them.
Mount read-only, and only what the work actually needs.

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEV_AGENT_IMAGE` | `dev-agent:ubuntu24` | Image to run. |
| `DEV_AGENT_HOME` | `~/.local/share/dev-agent` | Parent directory of the per-command homes. |
| `DEV_AGENT_CONFIG` | `~/.config/dev-agent/config.yml` | Config file path. |

## Removing persistent state

To force a fresh login, or to wipe an agent's config:

```sh
rm -rf ~/.local/share/dev-agent/codex
rm -rf ~/.local/share/dev-agent/claude
rm -rf ~/.local/share/dev-agent/bash
```

## Caveats

- Network access is not restricted. The agents need it to reach their APIs, and
  so does anything they run in your project.
- Anything you mount at `/workspace` is fully writable by the agent. Use the
  agent's own approval settings if you want a further check on that.
- Docker on Linux is assumed; the `--user` mapping and bind mount semantics
  differ on Docker Desktop for macOS and Windows.
