---
name: egress-policy
description: Network access from this container is filtered by the dev-agent proxy. Read this when a fetch, clone, install or API call fails to reach a host.
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
correct next step, not a workaround.

Anything that is not HTTP or HTTPS has no route out at all, including `git`
over SSH. Use an HTTPS remote.
