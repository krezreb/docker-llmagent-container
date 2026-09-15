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
- **One persistent home** at `~/.local/share/dev-agent/home`, shared by every
  command, so login state and toolchain caches survive between runs and are the
  same whichever command you start.
- **A config file** for any extra host paths you want the agent to see.

## Prerequisites

Docker, `make` and `git`. Docker Engine on Linux (rootless works too), Docker
Desktop or Colima on macOS.

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

### macOS

`make` and `git` come from the Xcode command line tools, which a stock macOS does
not have:

```sh
xcode-select --install
```

Docker Desktop is the usual choice. Install it from Docker's site, or with
Homebrew:

```sh
brew install --cask docker
```

Open it once from Applications so the daemon starts and the `docker` CLI is
linked, then check:

```sh
docker run --rm hello-world
```

[Colima](https://github.com/abiosoft/colima) is a lighter alternative and needs
no changes to `dev-agent`:

```sh
brew install colima docker
colima start --cpu 4 --memory 8
```

Apple silicon and Intel both work; the images build natively on arm64, PHP from
the `ondrej/php` PPA included.

Keep your projects somewhere under your home directory. Docker Desktop shares
`/Users` out of the box and Colima mounts your home directory, so anything under
`~` can be mounted with no extra setup. A path outside it, such as an external
disk under `/Volumes`, has to be added in Docker Desktop's Settings → Resources
→ File sharing first.

`~/.local/bin`, where `make install` puts the wrapper, is not on the `PATH` by
default:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

That line is for zsh, the default shell since Catalina. Under bash, use
`~/.bash_profile` instead — macOS Terminal starts login shells, which read
`~/.bash_profile` and never `~/.bashrc` unless you source it there yourself.

The wrapper needs bash to run, but the system bash 3.2 that ships with macOS is
enough; there is nothing to install. Your own shell can be zsh, bash, fish or
anything else, since `dev-agent` is a script and not a shell function.

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

This builds the image (`dev-agent:ubuntu24`), installs the wrapper to
`~/.local/bin/dev-agent`, and writes a starter `~/.config/dev-agent/config.yml`
if you don't already have one. Make sure `~/.local/bin` is on your `PATH`.

To build the image alone:

```sh
make build-image
```

## Usage

```
dev-agent [codex|claude|bash|tmux] [directory] [agent arguments...]
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
```

First run of each agent will prompt you to log in. The credentials are written
to that agent's persistent home on the host, so you only do this once.

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
| `--hostname` | Taken from the image, so the shell prompt names the variant: `dev-agent:php` gives `agent@dev-agent-php`. Characters a hostname cannot hold, such as the `:` before the tag, become `-`. |

Only two paths are writable and persistent:

- `/workspace` — the bind-mounted project directory.
- `/home/agent` — `~/.local/share/dev-agent/home` on the host, shared by every
  command.

The environment is not inherited from your shell. Only `HOME` and `TERM` are
passed in, so API keys and other secrets in your host environment stay on the
host.

The wrapper refuses to mount `/`, `/home`, `/root` or your home directory
outright, since mounting any of those would defeat the purpose.

## Extending the image

The image is a base to build on. Put a Dockerfile in `images/`, build it, and point
`DEV_AGENT_IMAGE` at the result:

```dockerfile
# images/rust.Dockerfile
FROM dev-agent:ubuntu24

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
variant to your shell's rc file, `~/.zshrc` if your `SHELL` is zsh and `~/.bashrc`
otherwise:

```sh
make aliases
source ~/.zshrc              # or ~/.bashrc
dev-agent-php claude .       # DEV_AGENT_IMAGE=dev-agent:php dev-agent claude .
```

The aliases go in a marked block, so re-running after adding a variant rewrites the
block rather than appending a second copy, and deleting the block removes them all.
`make aliases SHELL_RC=~/.bash_profile` writes somewhere else — worth it for bash on
macOS, where Terminal reads `~/.bash_profile` rather than `~/.bashrc`.

These ship, and are meant to be copied and edited rather than maintained as a
catalogue:

| Variant | Contents |
| --- | --- |
| `go` | `golang-go` |
| `java` | `default-jdk`, `maven` |
| `php` | PHP 8.5 from the `ondrej/php` PPA (`cli`, `xml`, `mbstring`, `curl`, `zip`) plus upstream `composer` |
| `ruby` | `ruby-full` |
| `rust` | `rustc`, `cargo` from apt |
| `rustup` | current Rust via the rustup installer |

Ubuntu pins compilers hard — apt's `rustc` is 1.75, years behind. `rustup.Dockerfile`
is the escape hatch, and the same shape works for nvm, sdkman, rbenv or pyenv: run the
installer at build time into a system path such as `/opt`, then point the tool's cache
variable at `/home/agent`.

The same applies to PHP: Ubuntu 24.04 ships 8.3 only, so `php.Dockerfile` adds the
`ondrej/php` PPA. `PHP_VERSIONS` is a space-separated build argument; the first entry
becomes the default `php`, and any others stay callable under their own names.

```sh
make php                                                    # PHP 8.5
docker build -t dev-agent:php -f images/php.Dockerfile \
    --build-arg PHP_VERSIONS="8.5 8.4 8.3" .                # php, php8.4, php8.3
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
| `DEV_AGENT_IMAGE` | `dev-agent:ubuntu24` | Image to run. |
| `DEV_AGENT_HOME` | `~/.local/share/dev-agent/home` | Host directory mounted at `/home/agent`. |
| `DEV_AGENT_CONFIG` | `~/.config/dev-agent/config.yml` | Config file path. |

## Removing persistent state

To force a fresh login, or to wipe an agent's config:

```sh
rm -rf ~/.local/share/dev-agent/home
```

## Caveats

- Network access is not restricted. The agents need it to reach their APIs, and
  so does anything they run in your project.
- Anything you mount at `/workspace` is fully writable by the agent. Use the
  agent's own approval settings if you want a further check on that.
- On macOS your uid (usually 501) has no entry in the image's `/etc/passwd`, so
  the shell prompt reads `I have no name!` and `whoami` fails. It is cosmetic:
  `HOME` is set explicitly, and the agents, git and the toolchains do not care.
- Bind mounts on macOS go through a VM, so large repositories are slower than on
  Linux. Docker Desktop's VirtioFS setting (Settings → General) is the fastest of
  its options.
