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
  --set-string 'agentgateway-standalone.config.llm.models[0].params.baseUrl=http://<llm-host>:<port>/v1'
```

`values-kind.yaml` assumes the release is called `agentgateway` in the `canvas` namespace, and that `sample-agent` runs in `components`.

Set the LLM URL again on every `helm upgrade`, or use `--reuse-values`.

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
