FROM dev-agent:ubuntu26

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        golang-go \
    && rm -rf /var/lib/apt/lists/*
