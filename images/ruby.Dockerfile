FROM dev-agent:ubuntu24

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ruby-full \
    && rm -rf /var/lib/apt/lists/*

# gem installs into /var/lib/gems by default, which is read-only at runtime.
ENV GEM_HOME=/home/agent/.gem \
    PATH=/home/agent/.gem/bin:$PATH
