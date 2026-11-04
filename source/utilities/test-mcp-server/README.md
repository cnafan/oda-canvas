# Test MCP server

A small MCP server that gives the AI gateway a real target in development clusters. It has no authentication, so don't use it in production.

| Path | Purpose |
|---|---|
| `/mcp` | Streamable HTTP MCP endpoint: tools `echo` and `add`, resource `canvas://about` |
| `/healthz` | Health check |
| `/flaky` | Returns 503 on odd-numbered calls, to exercise retries |
| `/slow` | Responds after 5 seconds, to exercise timeouts |

The image isn't published. Build it and load it into kind:

```bash
docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  -t tmforumodacanvas/test-mcp-server:0.1.0 source/utilities/test-mcp-server
kind load docker-image --name oda-canvas tmforumodacanvas/test-mcp-server:0.1.0
```

The [agentgateway chart](../../../charts/agentgateway/README.md) deploys it when `testMcpServer.enabled` is `true`.
