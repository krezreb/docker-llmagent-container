FROM dev-agent:ubuntu24

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        golang-go \
    && rm -rf /var/lib/apt/lists/*
