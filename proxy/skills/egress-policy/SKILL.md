---
name: egress-policy
description: Network access from this container is filtered by the dev-agent proxy. Read this when a fetch, clone, install or API call fails to reach a host, and before driving a headless browser.
---

# Egress policy

Network access from this container goes through the dev-agent proxy, and it is
filtered. A request that fails to reach a host may be policy rather than a bug,
and retrying will not change it.

Check the policy before assuming a fault:

```
curl -s http://dev-agent-proxy:8098/api/policy
```

That returns the current mode and the hosts that are allowed, denied and
tunnelled. A denied request also answers with `403` and a plain-text body
naming the rule that decided.

You cannot change the policy from in here; that endpoint is read-only. A human
can, from the UI at `http://127.0.0.1:8099` on the host. If you need a host that
is not allowed, say which host and why, and ask them to allow it — that is the
correct next step, not a workaround. The UI lists this container as
`$DEV_AGENT_NAME`; give that name too, so they approve the right request.

Anything that is not HTTP or HTTPS has no route out at all, including `git`
over SSH. Use an HTTPS remote.

## Headless browsers

Some images (`dev-agent:full`) ship Playwright with headless Chromium and
Firefox already installed; `command -v playwright` tells you. Use those rather
than downloading a browser, which the proxy will likely block. From node,
`require('playwright')` works from any directory.

Browsers ignore `HTTPS_PROXY` and the CA bundle in `SSL_CERT_FILE`, so pass
both explicitly or every external page fails:

```
playwright screenshot --proxy-server "$HTTPS_PROXY" --proxy-bypass "$NO_PROXY" \
    --ignore-https-errors https://example.com shot.png
```

```js
const { chromium } = require('playwright'); // or firefox
const browser = await chromium.launch({
  proxy: { server: process.env.HTTPS_PROXY, bypass: process.env.NO_PROXY },
});
const page = await browser.newPage({ ignoreHTTPSErrors: true });
```

`localhost` needs neither, since it is covered by the bypass list.
