FROM dev-agent:ubuntu26

ARG DEBIAN_FRONTEND=noninteractive

# Every runtime from the other images/ variants in one image. Rust comes from
# the archive (as in rust.Dockerfile); swap in the rustup block from
# rustup.Dockerfile if you need a newer toolchain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        golang-go \
        default-jdk \
        maven \
        ruby-full \
        rustc \
        cargo \
        php8.5-cli \
        php8.5-xml \
        php8.5-mbstring \
        php8.5-curl \
        php8.5-zip \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu's composer package trails upstream; take it from upstream instead.
RUN curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php \
    && php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer \
    && rm /tmp/composer-setup.php \
    && composer --version

# gem installs into /var/lib/gems by default, which is read-only at runtime.
ENV GEM_HOME=/home/agent/.gem \
    PATH=/home/agent/.gem/bin:$PATH

# Headless Chromium and Firefox for agents to drive through Playwright
# ('playwright screenshot', or require('playwright') from node). Baked in
# because the proxy would block the download at runtime, and kept under /opt so
# they stay readable whichever uid the container runs as.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    NODE_PATH=/usr/lib/node_modules
RUN npm install -g playwright \
    && playwright install --with-deps chromium firefox \
    && chmod -R a+rX /opt/playwright \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*
