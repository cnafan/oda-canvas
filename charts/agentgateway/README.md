# agentgateway

## Overview

This chart deploys [agentgateway](https://github.com/agentgateway/agentgateway) as the Canvas AI gateway. One instance offers LLM consumption, MCP connectivity, agent (A2A) connectivity and traffic management. The Canvas Portal's AI Gateways page shows its active configuration.

It wraps the upstream `agentgateway-standalone` chart, so it runs from one config file and needs no controller or CRDs. It's a separate release, not a dependency of `canvas-oda`, and it isn't installed by default.

## Architecture

| Port | Service port | Purpose | Access |
|---|---|---|---|
| 8080 | 80 | LLM, MCP and A2A traffic | No client authentication: any pod that can reach the Service can use it |
| 8081 | 8081 | Read-only config: `GET /config_dump` and `GET /api/runtime` | API key in the generated `agentgateway-portal-key` secret |
| 15000 | None | Admin API | Loopback only. It has no authentication, so don't expose it |

`values.yaml` holds only what every environment needs, including the read-only config routes. Add `llm`, `mcp` and `routes` in an overlay: `values-kind.yaml` is the example for a kind cluster.

The chart uses `ghcr.io` for the image and dependency because some corporate proxies block `cr.agentgateway.dev`.

### Integration Points

- **Canvas Portal**: reads port 8081 server-side. Set `backend.aiGateways.configUrl`, `backend.aiGateways.mcpUrl` and `backend.secretEnv.AGENTGATEWAY_API_KEY` in the `canvas-portal` chart.
- **Observability**: the upstream chart annotates pods so that Prometheus can scrape metrics on port 15020.

## Install on kind

```bash
docker pull ghcr.io/agentgateway/agentgateway:v1.7.0
docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  -t tmforumodacanvas/test-mcp-server:0.1.0 source/utilities/test-mcp-server
kind load docker-image --name oda-canvas \
  ghcr.io/agentgateway/agentgateway:v1.7.0 tmforumodacanvas/test-mcp-server:0.1.0

helm dependency update charts/agentgateway
helm install agentgateway charts/agentgateway -n canvas \
  -f charts/agentgateway/values-kind.yaml \
  --set-string 'agentgateway-standalone.config.llm.models[0].params.baseUrl=http://<llm-host>:<port>/v1' \
  --set-string 'agentgateway-standalone.config.routes[0].backends[0].host=sample-agent.components.svc.cluster.local:8080'
```

The test MCP server image is never published, so an install that enables `testMcpServer` needs the image loaded into the cluster first. `values-kind.yaml` also assumes the release is called `agentgateway`, because it names that server's Service.

Set both values again on every `helm upgrade`, or use `--reuse-values`.

Install with Helm directly. The chart reads the existing `agentgateway-portal-key` secret to keep the key across upgrades, which needs a live cluster. Applying `helm template` output, such as Argo CD's default rendering, generates a new key on each render, and the portal's AI Gateways page then shows that it's unauthorized.

## Models and MCP servers changed from the portal

The canvas portal's AI Gateways page writes model and MCP server changes directly to the `agentgateway-config` ConfigMap, and API keys and auth tokens to the `agentgateway-llm-keys` Secret, which the gateway reads as files.

**Helm owns the ConfigMap, and doesn't know about these changes.** After the portal changes a model, Helm 4's `helm upgrade` stops with a server-side apply conflict on `.data.config.yaml`. Don't get past this with `--force-conflicts` alone, because that puts back the chart's values and drops what was added from the portal. To keep it, pass the live lists to the upgrade:

```bash
LIVE=$(kubectl -n canvas get configmap agentgateway-config -o jsonpath='{.data.config\.yaml}')
MODELS=$(echo "$LIVE" | python3 -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["llm"]["models"]))')
TARGETS=$(echo "$LIVE" | python3 -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["mcp"]["targets"]))')
helm upgrade agentgateway charts/agentgateway -n canvas --reset-then-reuse-values --force-conflicts \
  --set-json "agentgateway-standalone.config.llm.models=$MODELS" \
  --set-json "agentgateway-standalone.config.mcp.targets=$TARGETS"
```

Older Helm versions don't stop: they put back the chart's values without warning. Use the same command, without `--force-conflicts`.

The keys stay in the Secret. Helm doesn't change it after creating it, and it stays after `helm uninstall`.

## Paths on the gateway (kind overlay)

| Path | Function | Notes |
|---|---|---|
| `/v1/*` | LLM | OpenAI-compatible, for example `/v1/chat/completions` |
| `/mcp` | MCP | Streamable HTTP |
| `/agents/sample-agent/.well-known/agent-card.json` | A2A | The card's URL is rewritten to point at the gateway |
| `/agents/sample-agent/a2a` | A2A | JSON-RPC. The agent requires an `A2A-Version: 1.0` header |
| `/demo/traffic/{flaky,slow,healthz}` | Traffic management | GET only. Shows the retry, the 2s timeout and the rate limit |

Check a config before you deploy it, using the `agentgateway` release binary:

```bash
PORTAL_KEY=dummy agentgateway --validate-only -f config.yaml
```
