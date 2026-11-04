import json
from pathlib import Path

import httpx
import pytest

from config import get_settings
from k8s.resources import ai_gateways
from k8s.resources.ai_gateways import get_ai_gateways_summary, parse_config_dump, sanitize

CONFIG_URL = "http://gw.test:8081/config_dump"
MCP_URL = "http://gw.test/mcp"
DUMP = json.loads((Path(__file__).parent / "fixtures" / "config_dump.json").read_text())


@pytest.fixture(autouse=True)
def gateway_settings(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_URL", CONFIG_URL)
    monkeypatch.setenv("AGENTGATEWAY_MCP_URL", MCP_URL)
    monkeypatch.setenv("AGENTGATEWAY_API_KEY", "test-key")
    get_settings.cache_clear()
    ai_gateways._discovery_cache.clear()
    yield
    get_settings.cache_clear()


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def streamed(body, content_type="application/json"):
    """A response that hasn't been read yet, as it would be from the network."""
    return httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body.encode()))


def mcp_body(request, content_type="application/json"):
    method = json.loads(request.content)["method"]
    result = (
        {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"secret": "x"}}]}
        if method == "tools/list"
        else {"resources": [{"name": "about", "uri": "canvas://about"}]}
    )
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
    if content_type == "text/event-stream":
        body = f"event: message\ndata: {body}\n\n"
    return body


def gateway(request, mcp_content_type="application/json"):
    if request.url.path == "/config_dump":
        return httpx.Response(200, json=DUMP)
    if request.url.path == "/api/runtime":
        return httpx.Response(200, json={"configReload": {"synchronized": False, "lastError": "bad value: hunter2"}})
    return streamed(mcp_body(request, mcp_content_type), mcp_content_type)


def test_fixture_fills_all_four_sections():
    parsed = parse_config_dump(DUMP)

    assert parsed["version"] == "1.7.0"
    assert parsed["warnings"] == []
    assert parsed["models"] == [{
        "name": "Qwen/Qwen3.8-27B-FP8",
        "provider": "openAI",
        "upstream": "203.0.113.10:50343/v1",
        "status": "active",
        "matches": ["/*"],
    }]
    assert parsed["agents"] == [{
        "name": "sample-agent",
        "matches": ["/agents/sample-agent/*"],
        "backend": "sample-agent.components.svc.cluster.local:8080",
    }]


def test_mcp_target_reference_is_resolved_to_its_host():
    tool = parse_config_dump(DUMP)["tools"][0]

    assert tool["name"] == "test-mcp"
    assert tool["transport"] == "mcp"
    assert tool["upstream"] == "agentgateway-test-mcp-server.canvas.svc.cluster.local:8000/mcp"
    assert tool["matches"] == ["/mcp/*", "/sse/*", "/.well-known/*"]


def test_traffic_keeps_rate_limit_values_and_drops_non_traffic_policies():
    rows = parse_config_dump(DUMP)["traffic"]
    demo = {row["policy"]: row["settings"] for row in rows if row["route"] == "traffic-demo"}

    assert demo["urlRewrite"] == {"prefix": "/"}
    assert demo["timeout"] == {"requestTimeout": "2s"}
    assert demo["retry"] == {"attempts": 2, "backoff": "200ms", "codes": ["503 Service Unavailable"]}
    assert demo["localRateLimit"] == {
        "type": "requests", "maxTokens": 20, "tokensPerFill": 20, "fillInterval": "1m0s",
    }
    assert not {"a2a", "budget"} & {row["policy"] for row in rows}


def test_read_path_never_appears():
    text = json.dumps(parse_config_dump(DUMP))

    assert "config-dump" not in text
    assert "/config_dump" not in text
    assert "aPIKey" not in text


def test_unknown_policy_is_listed_without_its_values():
    dump = {
        "binds": [{"listeners": {"l": {"routes": {"r": {
            "name": "r",
            "backends": [{"backend": "/b"}],
            "inlinePolicies": [{"requestHeaderModifier": {"add": {"x-leak": "value"}}}],
        }}}}}],
        "backends": [{"backend": {"host": {"name": "b", "target": "h:1"}}}],
    }

    rows = parse_config_dump(dump)["traffic"]

    assert [(row["policy"], row["settings"]) for row in rows] == [("requestHeaderModifier", {})]


@pytest.mark.parametrize(
    "dump",
    [
        None,
        [],
        "text",
        {},
        {"binds": "x", "backends": 3},
        {"binds": [None, {"listeners": []}, {"listeners": {"l": {"routes": {"r": None}}}}]},
        {"backends": [None, {"backend": {"ai": None}}, {"backend": {"mcp": {"name": "m", "target": []}}}]},
    ],
)
def test_malformed_dump_does_not_raise(dump):
    parsed = parse_config_dump(dump)

    assert parsed["models"] == parsed["tools"] == parsed["agents"] == parsed["traffic"] == []


def test_unresolvable_mcp_target_gives_a_warning():
    dump = {"backends": [{"backend": {"mcp": {"name": "m", "target": {
        "targets": [{"name": "t", "mcp": {"backend": {"backend": "/missing"}, "path": "/mcp"}}],
    }}}}]}

    assert parse_config_dump(dump)["warnings"] == ["MCP target t has no resolvable host"]


def test_sanitize_masks_secrets_but_not_token_counts():
    masked = sanitize({
        "apiKey": "a",
        "accessToken": "b",
        "client_secret": "c",
        "key": "d",
        "note": "Bearer abcdefghijklmnop.qrst",
        "url": "http://user:pw@host/",
        "other": "sk-abcdefghijkl",
        "maxTokens": 20,
        "tokensPerFill": 5,
        "nested": [{"password": "e"}],
    })

    assert masked == {
        "apiKey": "<redacted>",
        "accessToken": "<redacted>",
        "client_secret": "<redacted>",
        "key": "<redacted>",
        "note": "<redacted>",
        "url": "http://<redacted>@host/",
        "other": "<redacted>",
        "maxTokens": 20,
        "tokensPerFill": 5,
        "nested": [{"password": "<redacted>"}],
    }


@pytest.mark.parametrize(
    "value, expected",
    [
        ("basic dXNlcjpwYXNzd29yZA==", "<redacted>"),
        ("BEARER abcdefghijklmnop", "<redacted>"),
        ("user:pw@host:8000/mcp", "<redacted>@host:8000/mcp"),
        ("http://user:p@ss@host/", "http://<redacted>@host/"),
        ("http://host/mcp?api_key=abc&x=1", "http://host/mcp?api_key=<redacted>&x=1"),
        ("http://host/mcp?X-Auth-Sig=abc#frag", "http://host/mcp?X-Auth-Sig=<redacted>#frag"),
        ("host.example:8000/v1", "host.example:8000/v1"),
        ("Basic calculator for sums", "Basic calculator for sums"),
        ("Bearer tokens are accepted here", "Bearer tokens are accepted here"),
    ],
)
def test_sanitize_masks_secret_values(value, expected):
    assert sanitize(value) == expected


def test_summary_ok_sends_the_key_and_includes_runtime_and_discovery():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return gateway(request)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert seen[0] == "Bearer test-key"
    assert summary["runtime"] == {"synchronized": False}
    assert "hunter2" not in json.dumps(summary)
    assert summary["discovered"]["tools"] == [{"name": "echo", "description": "Echo"}]
    assert summary["discovered"]["resources"][0]["uri"] == "canvas://about"
    assert "test-key" not in json.dumps(summary)


def test_discovery_reads_sse_responses():
    summary = get_ai_gateways_summary(client_for(lambda r: gateway(r, "text/event-stream")))

    assert summary["discovered"]["tools"][0]["name"] == "echo"


def test_discovery_skips_empty_sse_events():
    def handler(request):
        if request.url.path != "/mcp":
            return gateway(request)
        return streamed(f"data:\n\n{mcp_body(request, 'text/event-stream')}", "text/event-stream")

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["discovered"]["tools"][0]["name"] == "echo"


def test_discovery_refuses_compressed_responses():
    seen = []

    def handler(request):
        if request.url.path != "/mcp":
            return gateway(request)
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=b"\x1f\x8b", headers={"content-encoding": "gzip"})

    summary = get_ai_gateways_summary(client_for(handler))

    assert seen == ["identity"]
    assert summary["discovered"] is None
    assert summary["state"] == "ok"


def test_discovery_stops_at_the_deadline(monkeypatch):
    monkeypatch.setattr(ai_gateways, "MCP_DISCOVERY_DEADLINE_SECONDS", -1.0)

    summary = get_ai_gateways_summary(client_for(gateway))

    assert summary["discovered"] is None
    assert summary["discoveryError"] == "ValueError"


def test_discovery_is_cached():
    calls = []

    def handler(request):
        if request.url.path == "/mcp":
            calls.append(request)
        return gateway(request)

    client = client_for(handler)
    get_ai_gateways_summary(client)
    get_ai_gateways_summary(client)

    assert len(calls) == 2  # tools/list and resources/list, once each


def test_discovery_rejects_oversized_responses():
    def handler(request):
        if request.url.path == "/mcp":
            return streamed("x" * (ai_gateways.MAX_MCP_RESPONSE_BYTES + 1))
        return gateway(request)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert summary["discovered"] is None
    assert summary["discoveryError"] == "ValueError"


def test_discovery_failure_leaves_config_available():
    def handler(request):
        if request.url.path == "/mcp":
            return httpx.Response(503)
        return gateway(request)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert summary["tools"]
    assert summary["discovered"] is None
    assert summary["discoveryError"] == "HTTPStatusError"


def test_runtime_failure_leaves_config_available():
    def handler(request):
        if request.url.path == "/api/runtime":
            return httpx.Response(404)
        return gateway(request)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert summary["runtime"] is None


def test_not_deployed_when_url_is_unset(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_URL", "")
    get_settings.cache_clear()

    assert get_ai_gateways_summary(client_for(gateway))["state"] == "not_deployed"


def raise_connect_error(request):
    raise httpx.ConnectError("refused", request=request)


def test_not_deployed_when_service_name_does_not_resolve(monkeypatch):
    monkeypatch.setattr(ai_gateways, "_host_resolves", lambda url: False)

    assert get_ai_gateways_summary(client_for(raise_connect_error))["state"] == "not_deployed"


def test_unreachable_when_connection_is_refused(monkeypatch):
    monkeypatch.setattr(ai_gateways, "_host_resolves", lambda url: True)

    assert get_ai_gateways_summary(client_for(raise_connect_error))["state"] == "unreachable"


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized(status):
    summary = get_ai_gateways_summary(client_for(lambda r: httpx.Response(status)))

    assert summary["state"] == "unauthorized"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, text="not json"), httpx.Response(200, json=[1])],
)
def test_unreachable_on_bad_responses(response):
    summary = get_ai_gateways_summary(client_for(lambda r: response))

    assert summary["state"] == "unreachable"
