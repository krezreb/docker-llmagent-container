FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        bind9-dnsutils \
        bubblewrap \
        git \
        jq \
        less \
        openssh-client \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
        pkg-config \
        ripgrep \
        procps vim tmux \
        figlet lolcat \
    && rm -rf /var/lib/apt/lists/*

# Under 'dev-agent --workspace' the project is bind-mounted at /workspace while
# the same checkout lives somewhere else entirely on the host, so the absolute
# paths git normally writes into a worktree's link files ("gitdir:
# /workspace/.git/worktrees/x") are dead ends outside the container. Recording
# them relative to the checkout instead keeps a worktree usable from both sides. Needs git 2.48 or newer at both ends: an
# older git still reads and writes such a worktree, but reports it as prunable,
# and `git gc` then drops its registration.
RUN git config --system worktree.useRelativePaths true

# Node.js from NodeSource; Ubuntu 26.04 ships Node 22 with npm 9.
ARG NODE_MAJOR=24
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install the coding agents into the immutable image.
#
# Both move fast, and an unpinned 'npm install' would be cached for as long as
# nothing above it changes: a rebuild months from now would quietly ship the
# pair that was current the day the layer was first built. The Makefile passes
# the versions npm calls 'latest' at build time, which gives this layer a cache
# key that changes exactly when one of them publishes. Left at 'latest' — a
# plain 'docker build .' — it is whatever docker already has.
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
RUN npm install -g \
      npm@11 \
      "@openai/codex@${CODEX_VERSION:-latest}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-latest}" \
    && npm cache clean --force \
    && npm ls -g --depth=0

RUN echo "alias ll='ls -alF'" >> /etc/bash.bashrc

RUN mkdir -p /workspace

ENV HOME=/home/agent
WORKDIR /workspace

# The proxy's UI lists the agents that are up, and it cannot see that from
# traffic alone: an agent that is thinking, or waiting on a human, holds no
# connection open and would drop off the list. So the container says so
# itself, at startup and every five seconds after that.
#
# Five rather than a minute because the loop is also the retry: a first beat
# sent before the network is ready would otherwise leave the UI reading "0
# agents up" for a whole minute with an agent plainly running.
#
# Backgrounded and never fatal: this is a status line, and an agent has to run
# whether the proxy is reachable, unreachable, or not in use at all — without
# DEV_AGENT_HEARTBEAT set (every run but --proxy) nothing is sent.
#
# The banner names the container by the number that ends the name the proxy
# shows: dev-agent-bash-12345 prints as agent-12345, and the same number seeds
# the colours, so each agent keeps its own rainbow. Only on a terminal:
# dev-agent also runs this image to cat the CA bundle out of it, and a banner
# in that output would end up inside the bundle.
COPY --chmod=755 <<'EOF' /usr/local/bin/dev-agent-entrypoint
#!/bin/sh
if [ -n "$DEV_AGENT_NAME" ] && [ -t 1 ]; then
    n="${DEV_AGENT_NAME##*-}"
    figlet -f small -w "${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}" "agent-$n" \
        | /usr/games/lolcat --seed "$n" 2>/dev/null || true
fi
if [ -n "$DEV_AGENT_HEARTBEAT" ]; then
    while :; do
        curl -fsS -m 5 -X POST "$DEV_AGENT_HEARTBEAT" -o /dev/null 2>/dev/null
        sleep 5
    done &
fi
exec "$@"
EOF

ENTRYPOINT ["/usr/local/bin/dev-agent-entrypoint"]
CMD ["/bin/bash"]
