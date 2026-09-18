# The proxy image: mitmproxy, the addon, and nothing else.
#
# Pinned to the version VALIDATION.md measured. mitmproxy owns TLS
# interception, HTTP/2, websockets and certificate generation; what is added
# here is the policy engine, the categoriser, the API and the UI.
FROM mitmproxy/mitmproxy:12.2.3

# The rulesets and the category map are hand-editable YAML, and the base image
# ships no YAML parser. The only dependency added; tornado, which the API
# server uses, is already here because mitmweb needs it.
RUN pip install --no-cache-dir pyyaml==6.0.3

WORKDIR /app

# Vue, vendored rather than loaded from a CDN: the UI has to work in lockdown,
# when nothing in this container can reach one. Pinned and checksummed because
# this script runs in the operator's browser with the writable API in reach.
ADD --chmod=644 --checksum=sha256:dae9c178e86e09240e58721cdb46a81ac96ae1dc6a870d1b1bb9ffaee3c6d3fe \
    https://cdn.jsdelivr.net/npm/vue@3.5.22/dist/vue.global.prod.js \
    /app/ui/vue.global.prod.js

COPY . /app

# 3128 the proxy, 8099 the UI and full API, 8098 the read-only API agents use.
# Only 3128 and 8098 are reachable from the agents network; 8099 is published
# to the host's loopback by compose.yml, and carries the subnet guard of
# SPEC section 11.3 as well.
EXPOSE 3128 8098 8099

# confdir is mitmproxy's own, and holds the CA private key: it is why /state
# has two directories and only agent-ca/ is ever mounted into a container.
#
# connection_strategy=lazy so that nothing is dialled upstream before the
# policy has decided — an eager connection to a host we are about to deny
# would leak the fact of the attempt.
ENTRYPOINT ["mitmdump"]
CMD ["--listen-host", "0.0.0.0", \
     "--listen-port", "3128", \
     "--set", "confdir=/state/ca", \
     "--set", "connection_strategy=lazy", \
     "--quiet", \
     "-s", "/app/addon.py"]
