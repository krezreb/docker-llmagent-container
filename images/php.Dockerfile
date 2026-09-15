FROM dev-agent:ubuntu24

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        php-cli \
        php-xml \
        php-mbstring \
        composer \
    && rm -rf /var/lib/apt/lists/*
