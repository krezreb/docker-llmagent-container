FROM dev-agent:ubuntu24

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        rustc \
        cargo \
    && rm -rf /var/lib/apt/lists/*
