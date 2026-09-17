# docker-llmagent-container

Run the [Codex](https://github.com/openai/codex) and [Claude Code](https://github.com/anthropics/claude-code)
CLI agents inside a locked-down Docker container, with only one project directory
exposed to them.

The point is blast radius. An agent running on the host can read your SSH keys,
your browser profile and every repository you own. Here it sees one directory
and nothing else.

![dev-agent installed, Claude Code running in the sandbox, and the read-only mount refusing writes](docs/demo.gif)

## Why not just the agent's own permissions?

Both agents ship a permission system, and a project `.claude/settings.json` can
deny reads of the obvious secrets:

```json
{
  "permissions": {
    "deny": ["Read(~/.aws/**)", "Read(~/.ssh/**)", "Read(~/.gnupg/**)"]
  }
}
```

Worth having, but those patterns restrict a *tool*, not a *file*. The model can simply
bypass by invoking a shell command and running `cat ~/.aws/credentials`

The `sandbox` block is a real kernel boundary, but it is
opt-out per command, `--dangerously-skip-permissions` can bypass it,
and the rules live in a file inside the one directory the agent may write 🧌.

This container removes the ability for the LLM to access files you don't want it to.
Only the files you choose are in the container's filesystem, and nothing inside can change them.

These two security measures are complements. The container decides what exists; the agent's settings
decide what it may do with what exists. Keep both.

## What you get

- **Ubuntu 26.04 LTS image** with both agents preinstalled globally via npm, plus the
  tooling they expect (git, curl, jq, ripgrep, python3, build-essential, bubblewrap).
- **`dev-agent` wrapper script** that starts a container with a hardened set of
  Docker flags and bind-mounts exactly one project directory, at the very path
  it has on the host, so the paths an agent reports mean the same on both sides.
- **One persistent home** at `~/.local/share/dev-agent/home`, shared by every
  command, so login state and toolchain caches survive between runs and are the
  same whichever command you start.
- **A config file** for any extra host paths you want the agent to see.

## Prerequisites

Docker Engine, `make` and `git`. Rootless Docker works too.

Make it git 2.48 or newer if you expect an agent to create git worktrees; see
[Git worktrees](#git-worktrees) for why.

### Linux

Install Docker Engine from Docker's own repository, not the distribution package —
the distro version is usually old:

```sh
curl -fsSL https://get.docker.com | sh
```

Then add yourself to the `docker` group so `dev-agent` can talk to the daemon without
`sudo`, and start a new login session for it to take effect:

```sh
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world
```

Membership of the `docker` group is equivalent to root on the host. If that is not
acceptable, use [rootless mode](https://docs.docker.com/engine/security/rootless/)
instead; `dev-agent` needs no changes for it.

### WSL2

Two options, both fine:

- **Docker Desktop for Windows** with WSL integration enabled for your distribution
  (Settings → Resources → WSL integration). `docker` then works from inside WSL.
- **Docker Engine installed directly in the WSL distribution**, using the Linux steps
  above. With systemd enabled in `/etc/wsl.conf` the daemon starts on its own;
  otherwise start it with `sudo service docker start`.

  ```ini
  # /etc/wsl.conf
  [boot]
  systemd=true
  ```

Keep your projects on the Linux filesystem, under `/home/you/...`, not on `/mnt/c`.
Bind mounts from `/mnt/c` are slow and lose Unix ownership and permissions, which
breaks the `--user` mapping the sandbox relies on.

WSL1 is not supported; check with `wsl -l -v` on the Windows side and convert with
`wsl --set-version <distro> 2`.

## Install

```sh
make install
```

This builds the image (`dev-agent:ubuntu26`), installs the wrapper to
`~/.local/bin/dev-agent`, and writes a starter `~/.config/dev-agent/config.yml`
if you don't already have one. Make sure `~/.local/bin` is on your `PATH`.

To build the image alone:

```sh
make build-image
```

## Keeping the agents current

Both agents are installed from npm, and that layer would otherwise stay cached
until something beneath it changed — a rebuild a year from now could still
hand you the pair that was current on the day you first built. `make
build-image` asks the registry what `latest` means today and passes the two
versions in as build arguments, so the layer is rebuilt when, and only when,
one of them publishes. Everything below it stays cached, which makes the
refresh one `npm install` rather than a whole distribution:

```sh
make build-image      # today's claude-code and codex
make install          # the same, plus the wrapper
```

Name a version to pin one instead:

```sh
make build-image CLAUDE_CODE_VERSION=2.1.267 CODEX_VERSION=0.154.0
```

And to distrust the cache from the base image down — a new apt package, a
rebuilt `ubuntu:26.04` — there is the blunt instrument:

```sh
make build-image DOCKER_BUILD_FLAGS=--no-cache
```

If the registry cannot be reached, both versions fall back to `latest`, which
leaves docker's cache in charge and lets an offline build go through.

## Usage

```
dev-agent [--ro] [--workspace] [--host-network] [codex|claude|bash|tmux] [directory] [agent arguments...]
```

`bash` drops you into a shell in the same sandbox, which is useful for
inspecting the image or running a command in the container by hand. `tmux` does
the same inside a tmux session, so you can run an agent and a shell side by side
in one container. Every command shares the same `/home/agent`, so a login done
under `claude` is already there under `bash` or `tmux`.

Both leading arguments are optional: the command defaults to `bash` and the
directory to the current one, so a bare `dev-agent` is the same as
`dev-agent bash .`. Any remaining arguments are passed straight through to the
agent.

```sh
dev-agent
dev-agent codex .
dev-agent claude .
dev-agent codex ~/src/foo
dev-agent claude ~/src/foo --model opus
dev-agent bash ~/src/foo
dev-agent tmux ~/src/foo
dev-agent --ro claude ~/src/foo
dev-agent --workspace claude ~/src/foo
dev-agent --host-network codex ~/src/foo
```

`--ro` mounts the project read-only, for when you want an agent to read the
code and not touch it — investigating a bug, reviewing a branch, answering
questions about an unfamiliar tree. The agent can still write to `/home/agent`
and `/tmp`, so it keeps its own session state, but every write into the project
fails, including `git` ones: no commits, no branch switches, no stray files.

`--host-network` puts the container in the host's network namespace
(`docker run --network host`), so a service it starts is reachable on your own
`localhost`. Codex's login flow needs this: it sends you to a callback URL on
localhost, which otherwise lands in your browser outside the container and
reaches nothing. It costs the container's network isolation — it shares your
network interfaces and can talk to anything listening on localhost, including
services bound only there — so use it for the login and drop it afterwards.
Docker also refuses `--hostname` in that mode, so the prompt shows the host's
hostname rather than the image's.

Options may be given before the command or before the directory, so both
`dev-agent --ro claude ~/src/foo` and `dev-agent claude --ro ~/src/foo` work.
Everything from the directory onwards still goes to the agent untouched.

First run of each agent will prompt you to log in. The credentials are written
to that agent's persistent home on the host, so you only do this once.

## Where the project lives

The project is mounted at the path it already has on this host, and the agent
starts there. A checkout at `~/src/foo` is `/home/you/src/foo` on both sides,
so every path an agent prints — a file it changed, a frame in a stack trace, a
command it suggests you run — is a path that also exists out here, ready to
paste into your own shell. It is still exactly one mount with the same
guarantees; only its destination follows the host.

Two consequences are worth knowing. The container learns the layout of your
home directory — it still cannot reach anything outside the mount, but the name
of the path crosses over. And both agents key their per-project state — session
history, todos — by working directory, so each project now keeps its own
instead of every project sharing one `/workspace` key.

`--workspace` mounts at `/workspace` instead, the way this wrapper behaved
before, and `DEV_AGENT_MIRROR=0` in your shell profile makes that the default
again; `--mirror` then brings a single run back to the host path.

A handful of paths cannot be mirrored: anything inside the container's own
`/home/agent`, and the image's top-level directories such as `/etc` or `/tmp`,
which the project would hide. Those fall back to `/workspace` with a note on
stderr, unless you asked for `--mirror` by name, which turns the fallback into
an error.

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
| `--hostname` | Omitted under `--host-network`; otherwise taken from the image, so the shell prompt names the variant: `dev-agent:php` gives `agent@dev-agent-php`. Characters a hostname cannot hold, such as the `:` before the tag, become `-`. |

Only two paths are writable and persistent:

- The project directory — bind-mounted at its host path, or at `/workspace`
  under `--workspace`, and mounted `readonly` when `--ro` is given.
- `/home/agent` — `~/.local/share/dev-agent/home` on the host, shared by every
  command.

The environment is not inherited from your shell. Only `HOME` and `TERM` are
passed in, so API keys and other secrets in your host environment stay on the
host.

The wrapper refuses to mount `/`, `/home`, `/root` or your home directory
outright, since mounting any of those would defeat the purpose.

### Codex's own sandbox does not run in here

Codex sandboxes each command it runs with bubblewrap, which needs to create an
unprivileged user namespace. `bubblewrap` is installed in the image, but on
Ubuntu 24.04 and newer the host sets
`kernel.apparmor_restrict_unprivileged_userns=1`, and unless docker's
`docker-default` AppArmor profile carries the matching `userns` rule the call
is denied inside the container:

```
bwrap: No permissions to create new namespace
```

Check your setup with:

```sh
dev-agent bash . -c 'bwrap --unshare-user --unshare-net --ro-bind / / /bin/true && echo userns-ok'
```

Where it fails, run codex with its own sandbox off — `codex --sandbox
danger-full-access` — and let the container be the sandbox. That is the layer
that matters: codex can then write anywhere it can already reach, which is the
project mount, `/home/agent` and `/tmp`, and nothing else. What you give up is
the inner layer: a command codex runs can rewrite the logins in `/home/agent`
and can reach the network even when codex meant to deny it.

The alternatives loosen the outer layer to enable the inner one, which is a bad
trade here: `--security-opt apparmor=unconfined` drops the container's AppArmor
profile, and `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` lifts
the restriction for everything on the host, not just this container.

## Git worktrees

Agents like to work in a `git worktree`. The two files that link one back to
its repository hold absolute paths, which only goes wrong when the two sides
disagree about where the checkout is. Mirrored, they agree, and a worktree
made in the container is an ordinary worktree out here.

Under `--workspace` they do not agree. The links then start with `/workspace`
— a location that exists nowhere on the host, so the worktree is not a git
repository at all once you step outside:

```
$ cd .claude/worktrees/readme-fix && git status
fatal: not a git repository: /workspace/.git/worktrees/readme-fix
```

The image therefore sets `worktree.useRelativePaths` in `/etc/gitconfig`, which
makes git write those links relative to the checkout instead. The same two
files then resolve from either side:

```
.claude/worktrees/x/.git    gitdir: ../../../.git/worktrees/x
.git/worktrees/x/gitdir     ../../../.claude/worktrees/x/.git
```

Relative links need **git 2.48 or newer on the host as well**. An older git
reads and writes such a worktree without complaint, but `git worktree list`
reports it as `prunable`, and `git gc` acts on that and drops the
registration. `dev-agent` warns at startup when the host git is too old and
the run is not mirrored; on Ubuntu, `ppa:git-core/ppa` carries a current
build:

```sh
sudo add-apt-repository ppa:git-core/ppa && sudo apt update && sudo apt install git
```

Worktrees left over from an older `/workspace` run keep their absolute paths.
Repair them from the main checkout *inside* the container, where `/workspace`
still means something:

```sh
dev-agent --workspace bash ~/src/foo -c 'git worktree repair'
```

## Extending the image

The image is a base to build on. Put a Dockerfile in `images/`, build it, and point
`DEV_AGENT_IMAGE` at the result:

```dockerfile
# images/rust.Dockerfile
FROM dev-agent:ubuntu26

RUN apt-get update \
    && apt-get install -y --no-install-recommends rustc cargo \
    && rm -rf /var/lib/apt/lists/*
```

```sh
make rust                                          # builds dev-agent:rust
DEV_AGENT_IMAGE=dev-agent:rust dev-agent claude .
```

`make images` builds every `images/*.Dockerfile`. Each variant depends on the base, so
a base change is picked up automatically. Name a variant anything except an existing
make target (`install`, `build-image`, `images`, `aliases`).

`make aliases` saves typing `DEV_AGENT_IMAGE=` by hand — it writes one alias per
variant to `~/.bashrc`:

```sh
make aliases
source ~/.bashrc
dev-agent-php claude .       # DEV_AGENT_IMAGE=dev-agent:php dev-agent claude .
```

The aliases go in a marked block, so re-running after adding a variant rewrites the
block rather than appending a second copy, and deleting the block removes them all.
`make aliases BASHRC=~/.bash_aliases` writes somewhere else.

These ship, and are meant to be copied and edited rather than maintained as a
catalogue:

| Variant | Contents |
| --- | --- |
| `go` | `golang-go` |
| `java` | `default-jdk`, `maven` |
| `php` | PHP 8.5 (`cli`, `xml`, `mbstring`, `curl`, `zip`) plus upstream `composer` |
| `ruby` | `ruby-full` |
| `rust` | `rustc`, `cargo` from apt |
| `rustup` | current Rust via the rustup installer |

A fresh LTS starts close to upstream — 26.04 carries rustc 1.93, Go 1.26, PHP
8.5 — and then holds still for years while upstream moves. `rustup.Dockerfile` is
the escape hatch for when that gap opens, and the same shape works for nvm,
sdkman, rbenv or pyenv: run the installer at build time into a system path such
as `/opt`, then point the tool's cache variable at `/home/agent`.

`php.Dockerfile` takes 8.5 straight from the archive. `PHP_VERSIONS` is a
space-separated build argument; the first entry becomes the default `php`, and
any others stay callable under their own names — though on 26.04 the archive
only has 8.5, and the `ondrej/php` PPA that carries the older ones has no
26.04 builds yet.

```sh
make php                                                    # PHP 8.5
```

To make a project always use its own image, export `DEV_AGENT_IMAGE` from a shell
alias, a direnv `.envrc`, or a small script in the project.

A derived image has to leave the sandbox intact, since the flags come from the wrapper
and not from the image:

- **Install at build time only.** The container runs `--read-only`, so anything that
  writes outside `/tmp`, `/run` or `/home/agent` at runtime will fail. A tool that
  writes to a system directory needs an `ENV` redirecting it under `/home/agent`:
  `GEM_HOME` for `gem`, which otherwise uses `/var/lib/gems`, and `CARGO_HOME` for
  `cargo`, which needs a writable registry. See `images/ruby.Dockerfile` and
  `images/rustup.Dockerfile`.
- **Don't set `USER` or change `HOME`.** The wrapper passes `--user` and
  `HOME=/home/agent`, and `/home/agent` is a bind mount from your host. It does not
  exist during the build, so an installer that inspects `HOME` may need it overridden
  for that one `RUN`, as rustup does.
- **Don't remove the agents.** `dev-agent` runs `codex`, `claude` or `bash` as the
  container command.

Toolchain caches such as `~/.cargo`, `~/.m2` or `~/go` land in the shared home at
`~/.local/share/dev-agent/home`, so they persist between runs.

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
| `DEV_AGENT_IMAGE` | `dev-agent:ubuntu26` | Image to run. |
| `DEV_AGENT_HOME` | `~/.local/share/dev-agent/home` | Host directory mounted at `/home/agent`. |
| `DEV_AGENT_CONFIG` | `~/.config/dev-agent/config.yml` | Config file path. |
| `DEV_AGENT_MIRROR` | `1` | Set to `0` to mount projects at `/workspace` by default, as `--workspace` does. |
| `DEV_AGENT_SKIP_GIT_CHECK` | unset | Set to any value to silence the warning about a host git older than 2.48. |

## Removing persistent state

To force a fresh login, or to wipe an agent's config:

```sh
rm -rf ~/.local/share/dev-agent/home
```

## Caveats

- Network access is not restricted. The agents need it to reach their APIs, and
  so does anything they run in your project.
- Pasting images into Claude code is broken
- The container is told where the project sits on the host, since that is the
  path it is mounted at. It still cannot read a thing outside the mount, but
  if you would rather not hand over even the path, run with `--workspace`.
- Anything you mount as the project directory is fully writable by the agent.
  Use the agent's own approval settings if you want a further check on that.
- Codex's bubblewrap sandbox usually cannot start inside the container, since
  creating a user namespace is denied there; see "Codex's own sandbox does not
  run in here".
- Docker on Linux is assumed; the `--user` mapping and bind mount semantics
  differ on Docker Desktop for macOS and Windows.
