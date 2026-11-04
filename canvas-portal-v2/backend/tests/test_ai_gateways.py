import asyncio
import base64
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from fastapi.responses import JSONResponse

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
        ("BEARER abc", "<redacted>"),
        ("http://host/?api_key=abc?def&x=1", "http://host/?api_key=<redacted>&x=1"),
        ("http://host/?next=https://y/?token=abc&x=1", "http://host/?next=https://y/?token=<redacted>&x=1"),
        ("//u:p@host/mcp", "//<redacted>@host/mcp"),
        ("user:pw?x@host:8000", "<redacted>@host:8000"),
        ("host.example:8000/v1", "host.example:8000/v1"),
    ],
)
def test_sanitize_masks_secret_values(value, expected):
    assert sanitize(value) == expected


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Basic calculator for sums", "Basic calculator for sums"),
        ("Bearer tokens are accepted here", "Bearer tokens are accepted here"),
        ("Use Bearer abcdefghijklmnop123 to call", "Use <redacted> to call"),
        ("Calls https://u:p@host/x?api_key=abc", "Calls https://<redacted>@host/x?api_key=<redacted>"),
        ("Calls u:p@ss@host/mcp", "Calls <redacted>@host/mcp"),
        ("Ask me@example.com for help", "Ask me@example.com for help"),
        ("Calls me@corp.com:pw@host/mcp", "Calls <redacted>@host/mcp"),
        ("Calls //u:p@host/mcp", "Calls //<redacted>@host/mcp"),
        ("See http://host/x?y=1 for help", "See http://host/x?y=1 for help"),
        ("Uses ?api_key=abc?def here", "Uses ?api_key=<redacted> here"),
        ("Go to /x?next=https://y/?token=abc", "Go to /x?next=https://y/?token=<redacted>"),
    ],
)
def test_mcp_descriptions_stay_readable_but_lose_secrets(description, expected):
    assert ai_gateways._mcp_description(description) == expected


def test_mcp_text_is_limited_in_length():
    assert len(ai_gateways._mcp_description("x" * 100_000)) == ai_gateways.MAX_MCP_TEXT_LENGTH


@pytest.mark.parametrize("hostile", ["?" + "key" * 300_000, "a:" * 500_000, "?a=" * 300_000, "a b " * 250_000, "?key=" * 200_000, "/a:" * 300_000, "@:" * 500_000])
def test_masking_long_hostile_input_is_fast(hostile):
    start = time.monotonic()
    sanitize(hostile)
    ai_gateways._mcp_description(hostile)

    assert time.monotonic() - start < 2


def test_secret_past_the_length_limit_is_masked_before_the_cut():
    text = "x " * (ai_gateways.MAX_MCP_TEXT_LENGTH // 2 - 5) + "Bearer abcdefghijklmnop123"

    assert "abcdef" not in ai_gateways._mcp_description(text)


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
    tool = summary["discovered"]["tools"][0]
    assert (tool["name"], tool["description"]) == ("echo", "Echo")
    assert "secret" not in tool["inputSchema"]
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


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def _sse(*chunks):
    return [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]


def _delta(**delta):
    return {"choices": [{"delta": delta, "finish_reason": None}]}


FINISHED = {"choices": [{"delta": {}, "finish_reason": "stop"}]}


@pytest.fixture
def llm_settings(monkeypatch):
    monkeypatch.setattr(
        ai_gateways,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "AGENTGATEWAY_CONFIG_URL": "http://gateway:8081/config_dump",
                "AGENTGATEWAY_LLM_URL": "http://gateway/v1/chat/completions",
                "AGENTGATEWAY_API_KEY": "portal-key",
            },
        ),
    )


def _run_test(handler, model="m"):
    async def collect():
        stream = ai_gateways.stream_model_test(model, transport=httpx.MockTransport(handler))
        return [json.loads(line) async for line in stream]

    return asyncio.run(collect())


def _streaming(chunks):
    return lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Chunks(chunks)
    )


def test_model_test_sends_only_the_fixed_request(llm_settings):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, stream=_Chunks([b"data: [DONE]\n\n"]))

    _run_test(handler)
    assert seen["url"] == "http://gateway/v1/chat/completions"
    assert seen["body"] == {
        "model": "m",
        "messages": [{"role": "user", "content": "PING"}],
        "stream": True,
        "max_tokens": ai_gateways.LLM_TEST_MAX_TOKENS,
    }


def test_model_test_streams_reasoning_then_content(llm_settings):
    chunks = _sse(
        _delta(role="assistant", content=""),
        _delta(reasoning="Think"),
        _delta(reasoning_content="ing"),
        _delta(content="PONG"),
        FINISHED,
    ) + [b"data: [DONE]\n\n"]
    assert _run_test(_streaming(chunks)) == [
        {"type": "reasoning", "text": "Think"},
        {"type": "reasoning", "text": "ing"},
        {"type": "content", "text": "PONG"},
        {"type": "done", "finishReason": "stop"},
    ]


def test_model_test_handles_events_split_across_chunks(llm_settings):
    joined = b"".join(_sse(_delta(content="PONG"), FINISHED)) + b"data: [DONE]\n\n"
    pieces = [joined[i : i + 7] for i in range(0, len(joined), 7)]
    assert _run_test(_streaming(pieces)) == [
        {"type": "content", "text": "PONG"},
        {"type": "done", "finishReason": "stop"},
    ]


def test_model_test_finishes_without_a_done_marker(llm_settings):
    assert _run_test(_streaming(_sse(_delta(content="hi"), FINISHED)))[-1] == {
        "type": "done",
        "finishReason": "stop",
    }


def test_model_test_reports_a_stream_cut_short(llm_settings):
    assert _run_test(_streaming(_sse(_delta(content="hi"))))[-1]["type"] == "error"


@pytest.mark.parametrize("status, text", [(429, "rate limit"), (502, "HTTP 502"), (401, "HTTP 401")])
def test_model_test_upstream_failure_hides_the_body(llm_settings, status, text):
    events = _run_test(lambda request: httpx.Response(status, text="upstream 203.0.113.10 sk-secretsecret"))
    assert len(events) == 1 and events[0]["type"] == "error"
    assert text in events[0]["message"]
    assert "203.0.113.10" not in events[0]["message"] and "secret" not in events[0]["message"]


def test_model_test_error_in_the_stream_hides_its_text(llm_settings):
    events = _run_test(_streaming(_sse(_delta(content="a"), {"error": {"message": "at 203.0.113.10"}})))
    assert events == [
        {"type": "content", "text": "a"},
        {"type": "error", "message": "The model reported an error."},
    ]


def test_model_test_rejects_an_invalid_stream(llm_settings):
    assert _run_test(_streaming([b"data: {not json\n\n"]))[-1]["type"] == "error"


def test_model_test_reports_connection_failures_without_details(llm_settings):
    def handler(request):
        raise httpx.ConnectError("http://203.0.113.10 refused")

    assert _run_test(handler) == [{"type": "error", "message": "Request failed: ConnectError."}]


def test_model_test_stops_at_the_size_limit(llm_settings, monkeypatch):
    monkeypatch.setattr(ai_gateways, "MAX_LLM_TEST_RESPONSE_BYTES", 40)
    events = _run_test(_streaming(_sse(*[_delta(content="x" * 20)] * 5)))
    assert events[-1]["type"] == "error"
    assert sum(event["type"] == "content" for event in events) < 5


def test_model_test_stops_at_the_deadline(llm_settings, monkeypatch):
    monkeypatch.setattr(ai_gateways, "LLM_TEST_DEADLINE_SECONDS", -1)
    assert _run_test(_streaming(_sse(_delta(content="x"))))[-1]["type"] == "error"


def test_closing_the_model_test_closes_the_upstream_stream(llm_settings):
    upstream = _Chunks(_sse(_delta(content="a"), _delta(content="b")))

    async def close_after_first_event():
        stream = ai_gateways.stream_model_test(
            "m", transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=upstream))
        )
        await stream.__anext__()
        await stream.aclose()

    asyncio.run(close_after_first_event())
    assert upstream.closed


def _check_model(model, handler):
    asyncio.run(ai_gateways.ensure_model_is_active(model, transport=httpx.MockTransport(handler)))


def _config_with_models(active=(), rejected=()):
    return {
        "backends": [
            {
                "backend": {
                    "ai": {
                        "name": "llm",
                        "target": {
                            "providers": [
                                {
                                    "active": {name: {"endpoint": {}} for name in active},
                                    "rejected": {name: {"endpoint": {}} for name in rejected},
                                }
                            ]
                        },
                    }
                }
            }
        ]
    }


def test_active_model_is_accepted_using_the_portal_key(llm_settings):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=_config_with_models(active=["good"]))

    _check_model("good", handler)
    assert seen["auth"] == "Bearer portal-key"


@pytest.mark.parametrize("model", ["rejected-model", "unknown", "http://elsewhere/"])
def test_model_that_is_not_active_is_refused(llm_settings, model):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=_config_with_models(active=["good"], rejected=["rejected-model"]))

    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        _check_model(model, handler)
    assert refused.value.status_code == 404
    assert calls == ["http://gateway:8081/config_dump"]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(500), httpx.Response(200, text="not json")],
)
def test_unreadable_config_refuses_the_test(llm_settings, response):
    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        _check_model("good", lambda request: response)
    assert refused.value.status_code == 502


def test_model_test_needs_its_urls_configured(monkeypatch):
    monkeypatch.setattr(
        ai_gateways,
        "get_settings",
        lambda: type("Settings", (), {"AGENTGATEWAY_CONFIG_URL": "http://gateway", "AGENTGATEWAY_LLM_URL": ""}),
    )
    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        _check_model("good", lambda request: httpx.Response(200))
    assert refused.value.status_code == 503


def test_input_schema_keeps_fields_with_secret_like_names():
    schema = {
        "type": "object",
        "required": ["password"],
        "properties": {
            "password": {"type": "string", "default": "hunter2", "description": "Bearer abcdefghijklmnopqrstuvwx"},
            "count": {"type": "integer", "minimum": 1, "default": 2},
            "mode": {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]},
        },
        "x-internal": "drop me",
    }

    filtered = ai_gateways._input_schema(schema)

    assert filtered["required"] == ["password"]
    assert filtered["properties"]["password"]["type"] == "string"
    assert filtered["properties"]["count"] == {"type": "integer", "minimum": 1, "default": 2}
    assert filtered["properties"]["mode"]["anyOf"][0] == {"enum": ["a", "b"]}
    text = json.dumps(filtered)
    assert "hunter2" not in text and "abcdefghijklmnop" not in text and "drop me" not in text


def test_secret_fields_lose_defaults_and_choices_at_every_level():
    schema = {"properties": {
        "api_key": {"anyOf": [{"type": "string", "default": "prod-key-123"}, {"enum": ["prod-key-456"]}]},
        "settings": {"properties": {"token": {"oneOf": [{"type": "string", "default": "prod-key-789"}]}}},
    }}

    filtered = ai_gateways._input_schema(schema)

    assert "prod-key" not in json.dumps(filtered)
    assert filtered["properties"]["api_key"]["anyOf"][0] == {"type": "string"}


@pytest.mark.parametrize("name", ["API_KEY", "PASSWORD", "pwd", "private_key", "ACCESS_KEY", "TOKEN", "accessKey", "key"])
def test_secret_field_names_are_found_in_any_case(name):
    filtered = ai_gateways._input_schema({"properties": {name: {"type": "string", "default": "prod-value", "enum": ["prod-value"]}}})

    assert filtered["properties"][name] == {"type": "string"}


def test_ordinary_field_names_keep_their_defaults():
    filtered = ai_gateways._input_schema({"properties": {"mode": {"type": "string", "default": "fast"}}})

    assert filtered["properties"]["mode"]["default"] == "fast"


def test_results_are_returned_unmasked_to_the_admin():
    secret = "API_KEY=abc Bearer abcdefghijklmnopqrstuvwx"
    blob = base64.b64encode(b"data").decode()
    client, _ = mcp_server({"resources/read": {"result": {"contents": [{"uri": "canvas://about", "text": secret, "blob": blob}]}}})

    outcome = ai_gateways.read_mcp_resource("canvas://about", client)

    assert outcome["exchanges"][-1]["response"]["result"]["contents"] == [{"uri": "canvas://about", "text": secret, "blob": blob}]


def test_input_schema_gives_up_on_oversized_and_invalid_schemas():
    huge = {"properties": {f"field{i}": {"title": "x" * 200} for i in range(1000)}}

    assert ai_gateways._input_schema(huge) is None
    assert ai_gateways._input_schema("not a schema") is None


def test_input_schema_stops_at_the_depth_limit():
    schema = current = {}
    for _ in range(500):
        current["properties"] = {"next": {}}
        current = current["properties"]["next"]

    assert ai_gateways._input_schema(schema) is not None


def test_discovery_survives_a_deeply_nested_response():
    def handler(request):
        if request.url.path != "/mcp":
            return gateway(request)
        return streamed("[" * 200_000 + "]" * 200_000)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert summary["discovered"] is None


@pytest.mark.parametrize("value", ["NaN", "-Infinity", "1e999", '"\\ud83d"'])
def test_values_the_browser_cant_receive_dont_break_the_page(value):
    body = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"x","inputSchema":{"properties":{"a":{"default":%s}}}}]}}'

    def handler(request):
        return gateway(request) if request.url.path != "/mcp" else streamed(body % value)

    summary = get_ai_gateways_summary(client_for(handler))

    assert summary["state"] == "ok"
    assert summary["discovered"] is None
    JSONResponse(summary)


def test_tool_annotations_are_filtered():
    annotations = {"readOnlyHint": True, "destructiveHint": "yes", "title": "Add", "other": 1}

    assert ai_gateways._tool_annotations(annotations) == {"readOnlyHint": True, "title": "Add"}


def mcp_server(handlers=None, session_id=None, sse=True):
    """A fake MCP server that records each request, with responses per method."""
    seen = []
    responses = {
        "initialize": {"result": {"protocolVersion": "2025-06-18"}},
        "tools/list": {"result": {"tools": [{"name": "add"}]}},
        "resources/list": {"result": {"resources": [{"name": "about", "uri": "canvas://about"}]}},
        "tools/call": {"result": {"content": [{"type": "text", "text": "3"}], "isError": False}},
        "resources/read": {"result": {"contents": [{"uri": "canvas://about", "text": "hello"}]}},
        **(handlers or {}),
    }

    def handler(request):
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202, stream=httpx.ByteStream(b""))
        answer = responses[payload["method"]]
        if isinstance(answer, httpx.Response):
            return answer
        message = json.dumps({"jsonrpc": "2.0", "id": payload["id"], **answer})
        headers = {"mcp-session-id": session_id} if session_id else {}
        if sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", **headers},
                stream=httpx.ByteStream(f"event: message\ndata: {message}\n\n".encode()),
            )
        return httpx.Response(
            200, headers={"content-type": "application/json", **headers}, stream=httpx.ByteStream(message.encode())
        )

    return client_for(handler), seen


def _methods(seen):
    return [json.loads(r.content)["method"] for r in seen if r.method == "POST"]


def test_tool_call_does_the_handshake_and_records_every_exchange():
    client, seen = mcp_server()

    outcome = ai_gateways.call_mcp_tool("add", {"a": 1, "b": 2}, client)

    assert _methods(seen) == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert [e["method"] for e in outcome["exchanges"]] == _methods(seen)
    assert outcome["error"] is None
    final = outcome["exchanges"][-1]
    assert final["request"]["params"] == {"name": "add", "arguments": {"a": 1, "b": 2}}
    assert json.loads(seen[-1].content) == final["request"]
    assert final["response"]["result"]["content"][0]["text"] == "3"
    assert seen[-1].headers["mcp-protocol-version"] == "2025-06-18"
    assert not any(r.method == "DELETE" for r in seen)


def test_stateful_session_is_carried_and_closed():
    client, seen = mcp_server(session_id="session-1")

    ai_gateways.call_mcp_tool("add", {}, client)

    assert "mcp-session-id" not in seen[0].headers
    assert seen[-2].headers["mcp-session-id"] == "session-1"
    assert seen[-1].method == "DELETE"
    assert seen[-1].headers["mcp-session-id"] == "session-1"


def test_tool_that_is_not_listed_is_refused_without_a_call():
    client, seen = mcp_server()

    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        ai_gateways.call_mcp_tool("delete_everything", {}, client)

    assert refused.value.status_code == 404
    assert "tools/call" not in _methods(seen)


def test_resource_that_is_not_listed_is_refused_without_a_read():
    client, seen = mcp_server()

    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        ai_gateways.read_mcp_resource("file:///etc/passwd", client)

    assert refused.value.status_code == 404
    assert "resources/read" not in _methods(seen)


def test_a_masked_name_on_the_page_calls_the_raw_upstream_name():
    raw = "http://user:hunter2@host/resource"
    client, seen = mcp_server({"resources/list": {"result": {"resources": [{"uri": raw}]}}})
    displayed = ai_gateways._mcp_text(raw)
    assert displayed != raw

    ai_gateways.read_mcp_resource(displayed, client)

    assert json.loads(seen[-1].content)["params"]["uri"] == raw


def test_tool_error_result_is_returned_for_the_page_to_show():
    client, _ = mcp_server({"tools/call": {"result": {"content": [{"type": "text", "text": "boom"}], "isError": True}}})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] is None
    assert outcome["exchanges"][-1]["response"]["result"]["isError"] is True


def test_json_rpc_error_is_returned_for_the_page_to_show():
    client, _ = mcp_server({"tools/call": {"error": {"code": -32602, "message": "Bad arguments"}}})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["exchanges"][-1]["response"]["error"]["message"] == "Bad arguments"


def test_notification_before_the_response_is_skipped():
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
    answer = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": []}})
    stream = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(f"data: {note}\n\ndata: {answer}\n\n".encode()),
    )
    client, _ = mcp_server({"tools/call": stream})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["exchanges"][-1]["response"]["result"] == {"content": []}


def test_http_failure_hides_the_body_and_keeps_the_exchanges():
    client, _ = mcp_server({"tools/call": httpx.Response(502, stream=httpx.ByteStream(b"upstream 10.0.0.5 failed"))})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint returned HTTP 502."
    assert outcome["exchanges"][-1]["status"] == 502
    assert "10.0.0.5" not in json.dumps(outcome)


def test_refused_initialize_stops_the_call():
    client, seen = mcp_server({"initialize": {"error": {"code": -32600, "message": "no"}}})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint refused to start a session."
    assert _methods(seen) == ["initialize"]


def test_connection_failure_is_reported_without_details():
    def handler(request):
        raise httpx.ConnectError("cannot reach 10.0.0.5")

    outcome = ai_gateways.call_mcp_tool("add", {}, client_for(handler))

    assert outcome["error"] == "Request failed: ConnectError."
    assert "10.0.0.5" not in json.dumps(outcome)


def test_oversized_result_is_refused(monkeypatch):
    monkeypatch.setattr(ai_gateways, "MAX_MCP_RESPONSE_BYTES", 400)
    big = {"result": {"content": [{"type": "text", "text": "x" * 1000}]}}
    client, _ = mcp_server({"tools/call": big})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint sent a response that can't be used."


@pytest.mark.parametrize("value", ["NaN", "1e999", '"\\ud83d"'])
def test_result_the_browser_cant_receive_is_reported(value):
    body = '{"jsonrpc":"2.0","id":3,"result":{"x":%s}}' % value
    client, _ = mcp_server({"tools/call": httpx.Response(
        200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body.encode())
    )})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint sent a response that can't be used."
    assert [e["method"] for e in outcome["exchanges"]][-1] == "tools/call"
    JSONResponse(outcome)


def test_line_separators_inside_a_stream_stay_in_the_text():
    answer = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"text": "a\u2028b\x85c"}}, ensure_ascii=False)
    client, _ = mcp_server({"tools/call": httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(f"data: {answer}\r\n\r\n".encode())
    )})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] is None
    assert outcome["exchanges"][-1]["response"]["result"]["text"] == "a\u2028b\x85c"


def test_closing_a_session_never_reads_its_response():
    def body():
        raise AssertionError("The DELETE body was read")
        yield b""

    class Unread(httpx.SyncByteStream):
        def __iter__(self):
            return body()

    seen = []

    def handler(request):
        seen.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(200, stream=Unread())
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202, stream=httpx.ByteStream(b""))
        result = {"tools": [{"name": "add"}]} if payload["method"] == "tools/list" else {}
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "s"},
            stream=httpx.ByteStream(json.dumps({"id": payload["id"], "result": result}).encode()),
        )

    outcome = ai_gateways.call_mcp_tool("add", {}, client_for(handler))

    assert outcome["error"] is None
    assert seen[-1] == "DELETE"


def test_session_stops_when_its_time_budget_runs_out(monkeypatch):
    monkeypatch.setattr(ai_gateways, "MCP_CALL_DEADLINE_SECONDS", -1)
    client, seen = mcp_server()

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint took too long to respond."
    assert seen == []


def test_each_request_only_waits_for_the_time_left(monkeypatch):
    monkeypatch.setattr(ai_gateways, "MCP_CALL_DEADLINE_SECONDS", 5)
    timeouts = []
    client, _ = mcp_server()
    original = client.stream

    def stream(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "stream", stream)

    ai_gateways.call_mcp_tool("add", {}, client)

    assert all(0 < t.read <= 5 and t.connect <= 5 for t in timeouts)


def test_too_many_concurrent_calls_are_refused(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(ai_gateways, "_mcp_call_slots", slots)
    client, seen = mcp_server()

    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        ai_gateways.call_mcp_tool("add", {}, client)

    assert refused.value.status_code == 429
    assert seen == []


@pytest.mark.parametrize("failure", ["refused", "error", "ok"])
def test_call_slot_is_always_released(monkeypatch, failure):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ai_gateways, "_mcp_call_slots", slots)
    client, _ = mcp_server({"tools/call": httpx.Response(500, stream=httpx.ByteStream(b""))} if failure == "error" else None)
    name = "not-listed" if failure == "refused" else "add"

    try:
        ai_gateways.call_mcp_tool(name, {}, client)
    except ai_gateways.GatewayRequestRefused:
        pass

    assert slots.acquire(blocking=False)


def test_deeply_nested_result_is_refused():
    client, _ = mcp_server({"tools/call": httpx.Response(
        200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(b"[" * 200_000 + b"]" * 200_000)
    )})

    outcome = ai_gateways.call_mcp_tool("add", {}, client)

    assert outcome["error"] == "The MCP endpoint sent a response that can't be used."


def test_mcp_calls_need_their_url_configured(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_MCP_URL", "")
    get_settings.cache_clear()

    with pytest.raises(ai_gateways.GatewayRequestRefused) as refused:
        ai_gateways.read_mcp_resource("canvas://about")

    assert refused.value.status_code == 503


@pytest.fixture
def api(monkeypatch):
    """The router with a real admin check, for a user whose groups the test sets."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from auth.middleware import get_current_user
    from auth.models import AuthUser
    from routers import ai_gateways as router_module

    monkeypatch.setenv("AUTHZ_ADMIN_GROUPS", '["canvas-admin"]')
    monkeypatch.setenv("AUTHZ_ALLOW_ALL_AUTHENTICATED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(router_module, "call_mcp_tool", lambda name, arguments: {"error": None, "exchanges": [name]})
    monkeypatch.setattr(router_module, "read_mcp_resource", lambda uri: {"error": None, "exchanges": [uri]})

    app = FastAPI()
    app.include_router(router_module.router)
    groups = []
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="u", email="", groups=groups, sub="s"
    )
    return TestClient(app), groups


def test_only_admins_can_call_tools(api):
    client, groups = api

    assert client.post("/api/ai-gateways/mcp/tools/call", json={"name": "add"}).status_code == 403
    groups.append("/canvas-admin")
    assert client.post("/api/ai-gateways/mcp/tools/call", json={"name": "add"}).json()["exchanges"] == ["add"]


def test_only_admins_can_read_resources(api):
    client, groups = api
    body = {"uri": "canvas://about"}

    assert client.post("/api/ai-gateways/mcp/resources/read", json=body).status_code == 403
    groups.append("/canvas-admin")
    assert client.post("/api/ai-gateways/mcp/resources/read", json=body).json()["exchanges"] == ["canvas://about"]


@pytest.mark.parametrize("body", [{"name": ""}, {}, {"name": "add", "arguments": []}])
def test_invalid_tool_calls_are_rejected(api, body):
    client, groups = api
    groups.append("canvas-admin")

    assert client.post("/api/ai-gateways/mcp/tools/call", json=body).status_code == 422


def test_oversized_arguments_are_rejected(api):
    client, groups = api
    groups.append("canvas-admin")
    body = {"name": "add", "arguments": {"a": "x" * (ai_gateways.MAX_MCP_ARGUMENTS_BYTES + 1)}}

    assert client.post("/api/ai-gateways/mcp/tools/call", json=body).status_code == 422


def test_is_admin_matches_the_admin_requirement(monkeypatch):
    from auth.middleware import is_admin
    from auth.models import AuthUser

    user = AuthUser(username="u", email="", groups=["/canvas-admin"], sub="s")
    other = AuthUser(username="o", email="", groups=["viewers"], sub="s")

    monkeypatch.setenv("AUTHZ_ADMIN_GROUPS", '["canvas-admin"]')
    get_settings.cache_clear()
    assert is_admin(user) and not is_admin(other)

    monkeypatch.setenv("AUTHZ_ADMIN_GROUPS", "[]")
    monkeypatch.setenv("AUTHZ_ALLOW_ALL_AUTHENTICATED", "false")
    get_settings.cache_clear()
    assert not is_admin(user)

    monkeypatch.setenv("AUTHZ_ALLOW_ALL_AUTHENTICATED", "true")
    get_settings.cache_clear()
    assert is_admin(other)
