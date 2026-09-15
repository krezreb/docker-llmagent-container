FROM dev-agent:ubuntu24

ARG DEBIAN_FRONTEND=noninteractive

# Ubuntu 24.04 ships PHP 8.3 only; ondrej/php carries the current releases.
# Space-separated list — the first entry becomes the default `php`, any others
# stay callable as php8.4, php8.3 and so on:
#   docker build --build-arg PHP_VERSIONS="8.5 8.4 8.3" ...
ARG PHP_VERSIONS="8.5"

RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common \
    && add-apt-repository -y ppa:ondrej/php \
    && apt-get update \
    && for v in ${PHP_VERSIONS}; do \
         apt-get install -y --no-install-recommends \
           php$v-cli \
           php$v-xml \
           php$v-mbstring \
           php$v-curl \
           php$v-zip \
         || exit 1; \
       done \
    && update-alternatives --set php "/usr/bin/php${PHP_VERSIONS%% *}" \
    && apt-get purge -y --auto-remove software-properties-common \
    && rm -rf /var/lib/apt/lists/* \
    && php --version

# Ubuntu's composer package depends on the distribution PHP, which would pull
# 8.3 back in; take it from upstream instead.
RUN curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php \
    && php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer \
    && rm /tmp/composer-setup.php \
    && composer --version

# composer writes its cache and auth config to HOME; /home/agent is the only
# writable spot at runtime, and it is already the default target, so nothing to
# redirect here.
