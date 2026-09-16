FROM dev-agent:ubuntu26

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jdk \
        maven \
    && rm -rf /var/lib/apt/lists/*
