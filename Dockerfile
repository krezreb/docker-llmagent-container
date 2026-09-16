FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        bind9-dnsutils \
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

CMD ["/bin/bash"]
