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
RUN npm install -g \
      npm@11 \
      @openai/codex \
      @anthropic-ai/claude-code \
    && npm cache clean --force

RUN echo "alias ll='ls -alF'" >> /etc/bash.bashrc

RUN mkdir -p /workspace

ENV HOME=/home/agent
WORKDIR /workspace

CMD ["/bin/bash"]
