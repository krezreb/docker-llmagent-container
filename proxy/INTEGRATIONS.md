# Shipping the log elsewhere

The proxy has no Loki or Elasticsearch client in it, and is not going to grow
one. It writes one JSON object per request to three places, and everything
below is a shipper reading one of them.

| Source | Where | Survives `compose down` | Use it for |
|---|---|---|---|
| stdout | `docker logs dev-agent-proxy` | no | a shipper already collecting docker logs |
| file | `/state/log.jsonl` in the container, the state volume on the host | yes | everything else; this is the default below |
| SSE | `GET :8099/api/events`, `event: request` | n/a | ad-hoc tailing, a dashboard of your own |

The record is section 10 of `SPEC.md`. The schema is a contract — fields are
added, never renamed — so a pipeline written against it stays working:

```json
{"ts":"2026-09-18T10:22:31.412Z","id":"01J8Z3...","client":"dev-agent-claude-4711",
 "method":"POST","scheme":"https","host":"api.anthropic.com","port":443,
 "path":"/v1/messages","status":200,"cat":"llm-api",
 "decision":"allow","rule":"default.yml:Claude API","mode":"default-deny",
 "bytes_up":8213,"bytes_down":91204,"ms":1840,"held_ms":0}
```

The fields worth building on: `decision` (`allow`, `deny`, `tunnel`), `cat`
(the category from section 9), `client` (which agent container), `host` (where
it was going), `held_ms` (how long a human sat on it).

## Before anything else: tail it

No stack required, and it is often all you want:

```sh
docker exec dev-agent-proxy tail -f /state/log.jsonl | jq -c '{ts,client,decision,host}'
curl -sN localhost:8099/api/events | grep --line-buffered '^data:'   # SSE, same records live
```

## Loki and Grafana

Vector reads the file and writes to Loki. One overlay file, composed on top of
the proxy's own, so nothing about the proxy changes:

```yaml
# proxy/compose.loki.yml
services:
  vector:
    image: timberio/vector:0.41.X-alpine
    container_name: dev-agent-vector
    restart: unless-stopped
    depends_on: [loki]
    volumes:
      - ${DEV_AGENT_PROXY_STATE:-~/.local/share/dev-agent/proxy}:/state:ro
      - ./vector.toml:/etc/vector/vector.toml:ro
      - dev-agent-vector-data:/var/lib/vector
    networks: [telemetry]

  loki:
    image: grafana/loki:3.X
    container_name: dev-agent-loki
    restart: unless-stopped
    command: -config.file=/etc/loki/local-config.yaml
    volumes:
      - dev-agent-loki-data:/loki
    networks: [telemetry]

  grafana:
    image: grafana/grafana:11.X
    container_name: dev-agent-grafana
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Admin
    networks: [telemetry]

volumes:
  dev-agent-vector-data:
  dev-agent-loki-data:

networks:
  telemetry: {}
```

```toml
# proxy/vector.toml
[sources.proxy]
type = "file"
include = ["/state/log.jsonl"]
read_from = "beginning"

[transforms.record]
type = "remap"
inputs = ["proxy"]
source = '''
. = parse_json!(.message)
.timestamp = parse_timestamp!(.ts, "%+")
'''

[sinks.loki]
type = "loki"
inputs = ["record"]
endpoint = "http://loki:3100"
encoding.codec = "json"
labels = { job = "dev-agent-proxy", client = "{{ client }}", cat = "{{ cat }}", decision = "{{ decision }}" }
remove_label_fields = false
```

Bring it up alongside the proxy, then open Grafana on `http://localhost:3000`
and add a Loki data source at `http://loki:3100`:

```sh
docker compose -f proxy/compose.yml -f proxy/compose.loki.yml up -d
```

Queries to start from:

```logql
{job="dev-agent-proxy", decision="deny"} | json
{job="dev-agent-proxy"} | json | host = "api.anthropic.com"     # one destination
sum by (host) (count_over_time({job="dev-agent-proxy", decision="deny"} [1h]))
sum by (client) (rate({job="dev-agent-proxy"} | json | unwrap bytes_down [5m]))
quantile_over_time(0.95, {job="dev-agent-proxy"} | json | unwrap ms [5m])
```

A deny rate alert is the one worth having: a rule on the third query, firing
when an agent repeatedly hits something the ruleset does not allow.

### Do not label on `host`

Tempting, and `SPEC.md` shows it, but a busy `npm install` reaches a few
hundred distinct hostnames and each one becomes a Loki stream. Keep `host`,
`path`, `id` and `status` in the JSON body — the `| json` in the queries above
gets at them — and label only on the low-cardinality fields: `client`, `cat`,
`decision`, `mode`. Label `host` only if you already know your traffic is a
handful of destinations.

If you do label on something called `host`, rename it (`dest`) — Grafana's
node-level dashboards use `host` for the machine, and the collision is
confusing later.

### Promtail instead of Vector

If promtail is already running, no new agent is needed:

```yaml
# promtail scrape_configs entry
- job_name: dev-agent-proxy
  static_configs:
    - targets: [localhost]
      labels:
        job: dev-agent-proxy
        __path__: /state/log.jsonl
  pipeline_stages:
    - json:
        expressions: { ts: ts, client: client, cat: cat, decision: decision }
    - timestamp: { source: ts, format: RFC3339 }
    - labels: { client: , cat: , decision: }
```

## Elasticsearch and Kibana

Same Vector agent, different sink. The important part is `id_key`: the
record's `id` becomes the Elasticsearch `_id`, so a re-read of the file
overwrites rather than duplicates — see the purge note at the end.

```toml
# add to proxy/vector.toml, or replace the loki sink
[sinks.elasticsearch]
type = "elasticsearch"
inputs = ["record"]
endpoints = ["http://elasticsearch:9200"]
mode = "bulk"
bulk.index = "dev-agent-proxy-%Y.%m.%d"
id_key = "id"
# auth.strategy = "basic"
# auth.user = "elastic"
# auth.password = "${ELASTIC_PASSWORD}"
```

```yaml
# proxy/compose.elastic.yml
services:
  vector:
    image: timberio/vector:0.41.X-alpine
    container_name: dev-agent-vector
    restart: unless-stopped
    depends_on: [elasticsearch]
    volumes:
      - ${DEV_AGENT_PROXY_STATE:-~/.local/share/dev-agent/proxy}:/state:ro
      - ./vector.toml:/etc/vector/vector.toml:ro
      - dev-agent-vector-data:/var/lib/vector
    networks: [telemetry]

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.X.X
    container_name: dev-agent-elasticsearch
    restart: unless-stopped
    environment:
      discovery.type: single-node
      # single node on a dev box; turn this on and set auth above for anything else
      xpack.security.enabled: "false"
      ES_JAVA_OPTS: -Xms1g -Xmx1g
    volumes:
      - dev-agent-es-data:/usr/share/elasticsearch/data
    networks: [telemetry]

  kibana:
    image: docker.elastic.co/kibana/kibana:8.X.X
    container_name: dev-agent-kibana
    restart: unless-stopped
    depends_on: [elasticsearch]
    ports:
      - "127.0.0.1:5601:5601"
    environment:
      ELASTICSEARCH_HOSTS: http://elasticsearch:9200
    networks: [telemetry]

volumes:
  dev-agent-vector-data:
  dev-agent-es-data:

networks:
  telemetry: {}
```

```sh
docker compose -f proxy/compose.yml -f proxy/compose.elastic.yml up -d
```

An index template is worth the thirty seconds — dynamic mapping makes `path` a
full-text field and indexes every URL as if it were prose:

```sh
curl -X PUT localhost:9200/_index_template/dev-agent-proxy -H 'Content-Type: application/json' -d '{
  "index_patterns": ["dev-agent-proxy-*"],
  "template": { "mappings": { "properties": {
    "ts":         { "type": "date" },
    "id":         { "type": "keyword" },
    "client":     { "type": "keyword" },
    "host":       { "type": "keyword" },
    "path":       { "type": "keyword", "ignore_above": 1024 },
    "method":     { "type": "keyword" },
    "scheme":     { "type": "keyword" },
    "cat":        { "type": "keyword" },
    "decision":   { "type": "keyword" },
    "rule":       { "type": "keyword" },
    "mode":       { "type": "keyword" },
    "status":     { "type": "short" },
    "port":       { "type": "integer" },
    "bytes_up":   { "type": "long" },
    "bytes_down": { "type": "long" },
    "ms":         { "type": "integer" },
    "held_ms":    { "type": "integer" }
  } } }
}'
```

Then a data view on `dev-agent-proxy-*` with `ts` as the time field.

### Filebeat instead of Vector

```yaml
# filebeat.yml
filebeat.inputs:
  - type: filestream
    id: dev-agent-proxy
    paths: ["/state/log.jsonl"]
    parsers:
      - ndjson: { target: "", overwrite_keys: true, add_error_key: true }

processors:
  - timestamp: { field: ts, layouts: ["2006-01-02T15:04:05.999Z"], test: ["2026-09-18T10:22:31.412Z"] }
  - fingerprint: { fields: [id], target_field: "@metadata._id" }   # same dedup as id_key

output.elasticsearch:
  hosts: ["http://elasticsearch:9200"]
  index: "dev-agent-proxy-%{+yyyy.MM.dd}"

setup.template.name: dev-agent-proxy
setup.template.pattern: dev-agent-proxy-*
setup.ilm.enabled: false
```

Mount it into the stock `docker.elastic.co/beats/filebeat:8.X.X` image at
`/usr/share/filebeat/filebeat.yml:ro`, alongside the same read-only `/state`.

## Reading docker's stdout instead of the file

If a collector is already tailing docker, point it at the container and skip
the state volume:

```toml
[sources.proxy]
type = "docker_logs"
include_containers = ["dev-agent-proxy"]
```

The transform and the sinks above are unchanged — `.message` is the JSON line
either way. The trade: stdout is lost on `compose down`, and it carries the
proxy's stderr warnings too, which are not JSON. `parse_json!` aborts the event
on those, which is the behaviour you want; use `parse_json` without the `!` and
an explicit `abort` if you would rather route them somewhere.

## Two things that will bite

**Purge re-reads the file.** `DELETE /api/log?seconds=` rewrites `log.jsonl` in
place with the surviving records, which changes the head of the file, which
changes the fingerprint a file-tailing shipper identifies it by. The shipper
sees a new file and re-reads it from the start. For Elasticsearch that is
harmless — the `id_key`/`fingerprint` above makes the write idempotent. Loki
has no dedup, so duplicate lines appear for whatever survived the purge. Ship
from `docker_logs` if that matters more to you than surviving a `compose down`.

**Rotation.** The file rotates at 64 MB with one older file kept
(`log.jsonl.1`). Vector, promtail and filebeat all follow a rename. Do not add
`log.jsonl.1` to the include patterns — it is the same lines a second time.
