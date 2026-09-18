#!/usr/bin/env bash
#
# Phase 2 spike: validate the assumptions in proxy/SPEC.md section 14 that can
# be checked without writing the proxy first.
#
#   1. a container on an internal network cannot reach the internet
#   2. it can reach the internet through a proxy on that network
#   2b. TLS interception works with the CA wired the way SPEC.md section 7 says,
#       on a read-only root filesystem
#   4. reverse DNS on the docker network yields the client container's name
#
# Assumption 3 (holding a flow) needs the addon and is not covered here.
#
# Run on the host, with docker. Cleans up after itself.
#
set -euo pipefail

IMAGE="${DEV_AGENT_IMAGE:-dev-agent:ubuntu26}"
MITM_IMAGE="${MITM_IMAGE:-mitmproxy/mitmproxy:latest}"
NET_AGENTS=spike-agents
NET_EGRESS=spike-egress
PROXY=spike-proxy
CA_DIR="$(mktemp -d)"

pass=0
fail=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  ....  %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cleanup() {
    docker rm -f "$PROXY" >/dev/null 2>&1 || true
    docker network rm "$NET_AGENTS" "$NET_EGRESS" >/dev/null 2>&1 || true
    rm -rf "$CA_DIR"
}
trap cleanup EXIT

# Runs a command in a throwaway container on the agents network only, with the
# same hardening dev-agent uses. Extra docker args come before '--'.
in_agent() {
    local args=()
    while [[ "$1" != "--" ]]; do args+=("$1"); shift; done
    shift

    docker run --rm \
        --network "$NET_AGENTS" \
        --read-only \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --tmpfs /tmp:rw,nosuid,nodev,size=512m \
        --env HOME=/tmp \
        "${args[@]}" \
        "$IMAGE" bash -c "$*"
}

head_ "setup"

docker image inspect "$IMAGE" >/dev/null 2>&1 ||
    { echo "missing image $IMAGE; run 'make build-image'" >&2; exit 1; }
docker pull -q "$MITM_IMAGE" >/dev/null

docker network create "$NET_EGRESS" >/dev/null
docker network create --internal "$NET_AGENTS" >/dev/null
note "networks created ($NET_AGENTS is internal)"

# mitmdump with interception on, CA written where we can read it out.
docker run -d --name "$PROXY" \
    --network "$NET_EGRESS" \
    -v "$CA_DIR:/home/mitmproxy/.mitmproxy" \
    "$MITM_IMAGE" \
    mitmdump --listen-port 3128 --set confdir=/home/mitmproxy/.mitmproxy >/dev/null
docker network connect "$NET_AGENTS" "$PROXY"

for _ in $(seq 30); do
    [[ -f "$CA_DIR/mitmproxy-ca-cert.pem" ]] && break
    sleep 1
done
[[ -f "$CA_DIR/mitmproxy-ca-cert.pem" ]] ||
    { echo "proxy never produced a CA cert" >&2; docker logs "$PROXY" >&2; exit 1; }
note "proxy up, CA generated"

# The bundle of SPEC.md section 7: system trust store plus the interception CA.
docker run --rm -v "$CA_DIR:/ca" "$IMAGE" \
    bash -c 'cat /etc/ssl/certs/ca-certificates.crt /ca/mitmproxy-ca-cert.pem' \
    > "$CA_DIR/bundle.crt"
chmod a+r "$CA_DIR"/*.pem "$CA_DIR/bundle.crt"

PROXY_ENV=(
    --env "HTTP_PROXY=http://$PROXY:3128"
    --env "HTTPS_PROXY=http://$PROXY:3128"
    --env "http_proxy=http://$PROXY:3128"
    --env "https_proxy=http://$PROXY:3128"
    --mount "type=bind,src=$CA_DIR,dst=/etc/dev-agent-ca,readonly"
    --env SSL_CERT_FILE=/etc/dev-agent-ca/bundle.crt
    --env CURL_CA_BUNDLE=/etc/dev-agent-ca/bundle.crt
    --env GIT_SSL_CAINFO=/etc/dev-agent-ca/bundle.crt
    --env REQUESTS_CA_BUNDLE=/etc/dev-agent-ca/bundle.crt
    --env NODE_EXTRA_CA_CERTS=/etc/dev-agent-ca/mitmproxy-ca-cert.pem
)

head_ "assumption 1 — isolation isolates"

if in_agent -- 'curl -sS -m 8 https://example.com -o /dev/null' 2>/dev/null; then
    bad "reached the internet directly from the internal network"
else
    ok "no direct internet from the internal network"
fi

if in_agent -- 'curl -sS -m 8 http://1.1.1.1 -o /dev/null' 2>/dev/null; then
    bad "reached a raw IP directly, so the network is not internal"
else
    ok "no direct route to a raw IP either"
fi

head_ "assumption 2 — egress works through the proxy"

if in_agent "${PROXY_ENV[@]}" -- 'curl -sS -m 15 https://example.com -o /dev/null'; then
    ok "HTTPS through the proxy, TLS intercepted, CA bundle accepted"
else
    bad "HTTPS through the proxy failed"
fi

if in_agent "${PROXY_ENV[@]}" -- 'curl -sS -m 15 http://example.com -o /dev/null'; then
    ok "plain HTTP through the proxy"
else
    bad "plain HTTP through the proxy failed"
fi

if in_agent "${PROXY_ENV[@]}" -- \
    'python3 -c "import urllib.request,os;
ctx=__import__(\"ssl\").create_default_context(cafile=os.environ[\"SSL_CERT_FILE\"]);
urllib.request.urlopen(\"https://example.com\",context=ctx,timeout=15)" >/dev/null'; then
    ok "python3 trusts the bundle"
else
    bad "python3 rejected the intercepted certificate"
fi

if in_agent "${PROXY_ENV[@]}" -- \
    'node -e "fetch(\"https://example.com\").then(r=>process.exit(r.ok?0:1)).catch(e=>{console.error(e.message);process.exit(1)})"'; then
    ok "node fetch trusts NODE_EXTRA_CA_CERTS"
else
    bad "node rejected the intercepted certificate"
fi

if in_agent "${PROXY_ENV[@]}" -- \
    'git clone -q --depth 1 https://github.com/git/git /tmp/g 2>&1 | tail -1; test -d /tmp/g/.git'; then
    ok "git clone over https through the proxy"
else
    bad "git clone over https failed"
fi

head_ "assumption 2b — the proxy is the only way out"

if in_agent "${PROXY_ENV[@]}" -- \
    'env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy \
        curl -sS -m 8 https://example.com -o /dev/null' 2>/dev/null; then
    bad "unsetting the proxy variables restored internet access"
else
    ok "unsetting the proxy variables loses the network, does not free it"
fi

head_ "assumption 4 — reverse DNS names the client"

client_ip="$(docker run -d --rm --name spike-client --network "$NET_AGENTS" \
    "$IMAGE" sleep 20 >/dev/null && \
    docker inspect -f "{{.NetworkSettings.Networks.${NET_AGENTS}.IPAddress}}" spike-client)"

resolved="$(docker exec "$PROXY" python3 -c \
    "import socket;print(socket.gethostbyaddr('$client_ip')[0])" 2>/dev/null || true)"
docker rm -f spike-client >/dev/null 2>&1 || true

if [[ "$resolved" == *spike-client* ]]; then
    ok "reverse DNS gives the container name ($resolved)"
else
    bad "reverse DNS gave '${resolved:-nothing}' for $client_ip; log the IP instead"
fi

head_ "informational — residual DNS channel (SPEC.md section 4)"

if in_agent -- 'getent hosts example.com >/dev/null'; then
    note "external names still resolve on the internal network: DNS exfil channel is real"
else
    note "external names do not resolve; the DNS caveat may be overstated"
fi

head_ "result"
printf '  %d passed, %d failed\n\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
