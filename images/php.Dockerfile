FROM dev-agent:ubuntu26

ARG DEBIAN_FRONTEND=noninteractive

# Ubuntu 26.04 ships PHP 8.5, the current release, so nothing outside the
# archive is needed. Space-separated list — the first entry becomes the default
# `php`, any others stay callable as php8.4, php8.3 and so on, but only 8.5 is
# in the archive: anything older needs ppa:ondrej/php, which has no 26.04
# builds yet.
ARG PHP_VERSIONS="8.5"

RUN apt-get update \
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
    && rm -rf /var/lib/apt/lists/* \
    && php --version

# Ubuntu's composer package trails upstream and drags its own PHP dependency
# along; take it from upstream instead.
RUN curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php \
    && php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer \
    && rm /tmp/composer-setup.php \
    && composer --version

# composer writes its cache and auth config to HOME; /home/agent is the only
# writable spot at runtime, and it is already the default target, so nothing to
# redirect here.
