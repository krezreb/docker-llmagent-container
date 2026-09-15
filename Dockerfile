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
