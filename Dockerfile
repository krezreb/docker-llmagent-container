FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
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

# Ubuntu 24.04 ships git 2.43, which predates worktree.useRelativePaths; the
# git-core PPA carries the current release.
RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common \
    && add-apt-repository -y ppa:git-core/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get purge -y --auto-remove software-properties-common \
    && rm -rf /var/lib/apt/lists/* \
    && git --version

# The project is bind-mounted at /workspace, but on the host the same checkout
# lives somewhere else entirely, so the absolute paths git normally writes into
# a worktree's link files ("gitdir: /workspace/.git/worktrees/x") are dead ends
# outside the container. Recording them relative to the checkout instead keeps
# a worktree usable from both sides. Needs git 2.48 or newer at both ends: an
# older git still reads and writes such a worktree, but reports it as prunable,
# and `git gc` then drops its registration.
RUN git config --system worktree.useRelativePaths true

# Node.js from NodeSource; Ubuntu 24.04 only ships Node 18 / npm 9.
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
