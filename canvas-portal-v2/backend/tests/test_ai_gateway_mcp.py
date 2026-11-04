import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import yaml

from config import get_settings
from k8s.resources import ai_gateway_config as config_file
from k8s.resources import ai_gateway_mcp as mcp
from k8s.resources import ai_gateways
from k8s.resources.ai_gateways import GatewayRequestRefused

CONFIG = {
    "binds": [{"port": 8081, "policies": {"key": "$PORTAL_KEY"}}],
    "mcp": {
        "gateways": "default",
        "statefulMode": "stateless",
        "targets": [{"name": "first", "mcp": {"host": "http://first.test:8000/mcp"}}],
    },
}


class FakeCore:
    def __init__(self, gateway):
        self.gateway = gateway
        self.text = yaml.safe_dump(CONFIG, sort_keys=False)
        self.version = 1
        self.keys = {}
        self.writes = []

    def read_namespaced_config_map(self, name, namespace):
        return SimpleNamespace(data={"config.yaml": self.text}, metadata=SimpleNamespace(resource_version=str(self.version)))

    def patch_namespaced_config_map(self, name, namespace, body):
        self.text = body.data["config.yaml"]
        self.version += 1
        self.writes.append(self.text)
        self.gateway.config_written(self.text)
        return SimpleNamespace(metadata=SimpleNamespace(resource_version=str(self.version)))

    def patch_namespaced_secret(self, name, namespace, body):
        for key, value in body["data"].items():
            if value is None:
                self.keys.pop(key, None)
            else:
                self.keys[key] = value

    def list_namespaced_pod(self, namespace, label_selector):
        return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="gw-0"))])

    def patch_namespaced_pod(self, name, namespace, body):
        pass


class FakeGateway:
    """Reload always succeeds. Like failClosed, one bad host then fails every MCP request."""

    def __init__(self):
        self.targets = [{"name": "first", "mcp": {"host": "http://first.test:8000/mcp"}}]
        self.last_error = None
        self.generation = 0

    def config_written(self, text):
        self.targets = yaml.safe_load(text)["mcp"]["targets"]
        self.generation += 1

    def handle(self, request):
        if request.url.path == "/api/runtime":
            return httpx.Response(200, json={"configReload": {"synchronized": True, "lastError": self.last_error}})
        if request.url.path == "/config_dump":
            return httpx.Response(200, json=self.dump())
        if "unreachable" in str(self.targets):
            return httpx.Response(500, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32603}})
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [], "resources": []}}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    def dump(self):
        targets = [
            {"name": t["name"], next(k for k in t if k in mcp._TRANSPORTS): {"backend": {"backend": f"/mcp//mcp/{i}"}, "path": "/mcp"}}
            for i, t in enumerate(self.targets)
        ]
        hosts = [{"backend": {"host": {"name": f"mcp//mcp/{i}", "target": "h:1"}}} for i in range(len(targets))]
        return {"generation": self.generation, "binds": [], "backends": [{"backend": {"mcp": {"name": "mcp", "target": {"targets": targets}}}}, *hosts]}


def no_such_host(*args, **kwargs):
    raise socket.gaierror


@pytest.fixture(autouse=True)
def gateway_settings(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_URL", "http://gw.test:8081/config_dump")
    monkeypatch.setenv("AGENTGATEWAY_MCP_URL", "http://gw.test/mcp")
    monkeypatch.setenv("AGENTGATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_NAMESPACE", "canvas")
    monkeypatch.setattr(config_file, "POLL_SECONDS", 0)
    monkeypatch.setattr(config_file, "LISTING_RETRY_SECONDS", 0)
    monkeypatch.setattr(config_file.socket, "getaddrinfo", no_such_host)
    monkeypatch.setattr(config_file, "NUDGED_DEADLINE_SECONDS", 0.05)
    get_settings.cache_clear()
    ai_gateways._discovery_cache.clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def core(gateway):
    return FakeCore(gateway)


@pytest.fixture
def http(gateway):
    return httpx.Client(transport=httpx.MockTransport(gateway.handle))


def request(**fields):
    return {
        "name": "second",
        "transport": "mcp",
        "host": "http://second.test:8000/mcp",
        "port": None,
        "path": None,
        "schema": None,
        "condition": None,
        "authToken": None,
        "removeAuthToken": False,
        "policies": {},
        **fields,
    }


def targets(core):
    return yaml.safe_load(core.text)["mcp"]["targets"]


def view(core, name):
    return next(t for t in mcp.list_mcp_targets(core)["targets"] if t["name"] == name)


def test_adding_a_server_keeps_the_rest_of_the_config(core, http):
    result = mcp.save_mcp_target(request(condition="true", policies={"http": {"version": "HTTP2"}}), core=core, http=http)

    assert result["applied"] is True
    document = yaml.safe_load(core.text)
    assert document["binds"][0]["policies"]["key"] == "$PORTAL_KEY"
    assert document["mcp"]["statefulMode"] == "stateless"
    assert targets(core)[1] == {
        "name": "second",
        "condition": "true",
        "policies": {"http": {"version": "HTTP2"}},
        "mcp": {"host": "http://second.test:8000/mcp"},
    }


def test_a_server_that_breaks_listing_is_undone(core, http, gateway):
    original = core.text
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.save_mcp_target(request(host="http://unreachable.test/mcp", authToken="t"), core=core, http=http)

    assert refused.value.status_code == 422
    assert "list MCP tools" in refused.value.message
    assert core.text == original
    assert core.keys == {}
    assert gateway.targets[0]["name"] == "first"
    assert ai_gateways._discovery_cache.get("result") is None


def test_an_edit_that_breaks_listing_is_undone(core, http):
    original = core.text
    with pytest.raises(GatewayRequestRefused):
        mcp.save_mcp_target(
            request(name="first", host="http://unreachable.test/mcp"), "first", view(core, "first")["revision"], core=core, http=http
        )
    assert core.text == original


def test_changes_are_allowed_when_listing_was_already_broken(core, http, gateway):
    gateway.targets = [{"name": "first", "mcp": {"host": "http://unreachable.test"}}]
    result = mcp.save_mcp_target(request(host="http://unreachable.test/2"), core=core, http=http)
    assert "already unavailable" in result["warning"]
    assert len(targets(core)) == 2


def test_removing_a_broken_server_is_allowed(core, http, gateway):
    core.text = yaml.safe_dump({"mcp": {"targets": [{"name": "bad", "mcp": {"host": "http://unreachable.test"}}]}})
    gateway.targets = yaml.safe_load(core.text)["mcp"]["targets"]
    result = mcp.remove_mcp_target("bad", core=core, http=http)
    assert result["applied"] is True
    assert targets(core) == []


def test_auth_token_goes_in_the_secret(core, http):
    mcp.save_mcp_target(request(authToken="tok-very-secret"), core=core, http=http)

    assert "tok-very-secret" not in core.text
    path = targets(core)[1]["policies"]["backendAuth"]["key"]["file"]
    assert path.startswith("/llm-keys/")
    assert path.rsplit("/", 1)[1] in core.keys
    shown = view(core, "second")
    assert shown["tokenSet"] is True
    assert shown["policies"] == {}
    assert "tok-very-secret" not in str(shown)


def test_blank_token_keeps_it_and_removing_deletes_it(core, http):
    mcp.save_mcp_target(request(authToken="t"), core=core, http=http)
    mcp.save_mcp_target(request(condition="true"), "second", view(core, "second")["revision"], core=core, http=http)
    assert "backendAuth" in targets(core)[1]["policies"]
    assert len(core.keys) == 1

    mcp.save_mcp_target(request(condition="true", removeAuthToken=True), "second", view(core, "second")["revision"], core=core, http=http)
    assert "policies" not in targets(core)[1]
    assert core.keys == {}


def test_replacing_a_token_deletes_the_old_one(core, http):
    mcp.save_mcp_target(request(authToken="one"), core=core, http=http)
    old = set(core.keys)
    mcp.save_mcp_target(request(authToken="two"), "second", view(core, "second")["revision"], core=core, http=http)
    assert len(core.keys) == 1
    assert not old & set(core.keys)


def test_removing_a_server_deletes_its_token(core, http):
    mcp.save_mcp_target(request(authToken="one"), core=core, http=http)
    mcp.remove_mcp_target("second", core=core, http=http)
    assert core.keys == {}
    assert [t["name"] for t in targets(core)] == ["first"]


@pytest.mark.parametrize(
    "change",
    [
        {"host": "http://evil.test/mcp"},
        {"transport": "sse"},
        {"policies": {"backendTunnel": {"proxy": {"host": "evil.test:1"}}}},
        {"port": 8443, "path": "/x"},
    ],
)
def test_a_kept_token_is_not_sent_to_a_new_upstream(core, http, change):
    mcp.save_mcp_target(request(authToken="one"), core=core, http=http)
    revision = view(core, "second")["revision"]
    writes = len(core.writes)
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.save_mcp_target(request(**change), "second", revision, core=core, http=http)
    assert refused.value.status_code == 422
    assert len(core.writes) == writes

    mcp.save_mcp_target(request(authToken="two", **change), "second", revision, core=core, http=http)
    assert len(core.writes) == writes + 1


def test_renaming_keeps_a_token(core, http):
    mcp.save_mcp_target(request(authToken="one"), core=core, http=http)
    mcp.save_mcp_target(request(name="renamed"), "second", view(core, "second")["revision"], core=core, http=http)
    assert targets(core)[1]["name"] == "renamed"
    assert "backendAuth" in targets(core)[1]["policies"]


def test_both_kinds_of_auth_are_refused(core, http):
    with pytest.raises(GatewayRequestRefused):
        mcp.save_mcp_target(request(authToken="t", policies={"backendAuth": {"key": "x"}}), core=core, http=http)
    assert core.writes == []


@pytest.mark.parametrize(
    "fields",
    [
        {"host": "http://127.0.0.1:15000/quitquitquit"},
        {"host": "127.0.0.1:15000"},
        {"host": "localhost:15000"},
        {"host": "http://LOCALHOST:15000"},
        {"host": "http://foo.localhost/"},
        {"host": "http://[::1]:15000/"},
        {"host": "http://[::ffff:127.0.0.1]:15000/"},
        {"host": "http://0.0.0.0:15000/"},
        {"host": "http://2130706433:15000/"},
        {"host": "http://0x7f.1:15000/"},
        {"host": "http://169.254.169.254/latest/meta-data"},
        {"host": "ftp://x.test"},
        {"host": ""},
        {"host": "http://x.test/$PORTAL_KEY"},
        {"path": "/$PORTAL_KEY"},
        {"condition": "$PORTAL_KEY"},
        {"policies": {"requestHeaderModifier": {"add": {"x": "$PORTAL_KEY"}}}},
        {"policies": {"backendAuth": {"key": {"file": "/var/run/secrets/kubernetes.io/serviceaccount/token"}}}},
        {"policies": {"backendAuth": {"key": [{"file": "/etc/passwd"}]}}},
        {"policies": {"backendTLS": {"root": "/etc/passwd"}}},
        {"transport": "stdio", "host": None},
        {"policies": {"backendTunnel": {"proxy": {"host": "127.0.0.1:15000"}}}},
        {"policies": {"backendTunnel": {"proxy": "127.0.0.1:15000"}}},
        {"policies": {"backendAuth": {"crossAppAccess": {"identityProvider": {"host": "http://[::1]:15000"}}}}},
        {"policies": {"backendAuth": {"gcp": {}}}},
        {"policies": {"backendAuth": {"aws": {}}}},
        {"policies": {"backendAuth": {"azure": {}}}},
        {"policies": {"backendAuth": {"passthrough": {}}}},
        {"policies": {"backendAuth": "passthrough"}},
        {"policies": {"backendTunnel": {"policies": {"backendAuth": {"gcp": {}}}}}},
        {"transport": "openapi"},
    ],
)
def test_bad_requests_are_refused(core, http, fields):
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.save_mcp_target(request(**fields), core=core, http=http)
    assert refused.value.status_code == 422
    assert core.writes == []


def test_a_name_that_resolves_to_loopback_is_refused(core, http, monkeypatch):
    monkeypatch.setattr(config_file.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.2", 0))])
    with pytest.raises(GatewayRequestRefused):
        mcp.save_mcp_target(request(host="http://sneaky.test"), core=core, http=http)
    assert core.writes == []


def test_existing_stdio_keeps_its_command_and_only_other_fields_change(core, http):
    stdio = {"cmd": "npx", "args": ["server"], "env": {"TOKEN": "x"}}
    core.text = yaml.safe_dump({"mcp": {"targets": [{"name": "s", "stdio": stdio}]}})
    shown = view(core, "s")
    assert shown["command"] == "npx"
    mcp.save_mcp_target(
        request(name="s", transport="stdio", host=None, condition="true"), "s", shown["revision"], core=core, http=http
    )
    assert targets(core)[0]["stdio"] == stdio
    assert targets(core)[0]["condition"] == "true"


def test_changing_stdio_to_a_host_and_back_is_refused_back(core, http):
    core.text = yaml.safe_dump({"mcp": {"targets": [{"name": "h", "mcp": {"host": "http://h.test"}}]}})
    with pytest.raises(GatewayRequestRefused):
        mcp.save_mcp_target(request(name="h", transport="stdio", host=None), "h", view(core, "h")["revision"], core=core, http=http)
    assert core.writes == []


def test_a_backend_reference_stays_on_edit(core, http):
    core.text = yaml.safe_dump({"mcp": {"targets": [{"name": "b", "mcp": {"backend": "shared", "path": "/mcp"}}]}})
    shown = view(core, "b")
    assert shown["backend"] == "shared"
    mcp.save_mcp_target(
        request(name="b", host=None, path="/mcp", condition="true"), "b", shown["revision"], core=core, http=http
    )
    assert targets(core)[0]["mcp"] == {"backend": "shared", "path": "/mcp"}


def test_inline_openapi_schema_escapes_dollar_and_round_trips(core, http):
    schema = '{"openapi":"3.0.0","x":{"$ref":"#/a"}}'
    mcp.save_mcp_target(request(transport="openapi", schema=schema), core=core, http=http)
    assert targets(core)[1]["openapi"]["schema"] == schema.replace("$", "$$")
    shown = view(core, "second")
    assert shown["schema"] == schema

    assert mcp.save_mcp_target(
        request(transport="openapi", schema=schema), "second", shown["revision"], core=core, http=http
    )["message"] == "Nothing changed."


def test_an_existing_file_schema_stays_on_edit(core, http):
    core.text = yaml.safe_dump({"mcp": {"targets": [{"name": "a", "openapi": {"host": "http://a.test", "schema": {"file": "/s.json"}}}]}})
    shown = view(core, "a")
    assert shown["schemaKept"] is True and shown["schema"] is None
    mcp.save_mcp_target(
        request(name="a", transport="openapi", host="http://a.test", condition="true"), "a", shown["revision"], core=core, http=http
    )
    assert targets(core)[0]["openapi"]["schema"] == {"file": "/s.json"}


def test_duplicate_and_unknown_names_are_refused(core, http):
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.save_mcp_target(request(name="first"), core=core, http=http)
    assert refused.value.status_code == 409
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.remove_mcp_target("nope", core=core, http=http)
    assert refused.value.status_code == 404


def test_stale_edit_is_refused(core, http):
    with pytest.raises(GatewayRequestRefused) as refused:
        mcp.save_mcp_target(request(name="first"), "first", "old", core=core, http=http)
    assert refused.value.status_code == 409


def test_shares_a_lock_with_model_changes(core, http):
    assert config_file._change_lock.acquire(blocking=False)
    try:
        with pytest.raises(GatewayRequestRefused) as refused:
            mcp.save_mcp_target(request(), core=core, http=http)
        assert refused.value.status_code == 409
    finally:
        config_file._change_lock.release()


def test_a_token_shared_with_a_model_is_kept(core, http):
    path = "/llm-keys/0123456789abcdef"
    core.keys = {"0123456789abcdef": "x"}
    core.text = yaml.safe_dump(
        {
            "llm": {"models": [{"name": "m", "provider": "openAI", "params": {"apiKey": {"file": path}}}]},
            "mcp": {"targets": [{"name": "t", "policies": {"backendAuth": {"key": {"file": path}}}, "mcp": {"host": "http://t.test"}}]},
        }
    )
    mcp.remove_mcp_target("t", core=core, http=http)
    assert core.keys


def test_not_configured(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_NAMESPACE", "")
    get_settings.cache_clear()
    assert mcp.list_mcp_targets() == {"configurable": False, "targets": []}


def _dump_ignores_policies(gateway):
    """The real dump omits inline target policies, so these edits can't be seen in it."""
    gateway.config_written = lambda text: setattr(gateway, "targets", yaml.safe_load(text)["mcp"]["targets"])


def test_a_token_only_edit_still_deletes_the_old_token(core, http, gateway):
    mcp.save_mcp_target(request(authToken="one"), core=core, http=http)
    old = set(core.keys)
    _dump_ignores_policies(gateway)

    result = mcp.save_mcp_target(request(authToken="two"), "second", view(core, "second")["revision"], core=core, http=http)
    assert result["applied"] is False
    assert len(core.keys) == 1
    assert not old & set(core.keys)

    mcp.save_mcp_target(request(removeAuthToken=True), "second", view(core, "second")["revision"], core=core, http=http)
    assert core.keys == {}


def test_a_slow_first_answer_does_not_undo_the_change(core, http, gateway):
    real = gateway.handle
    failures = []

    def fail_first_listing_after_write(request_):
        if request_.url.path == "/mcp" and gateway.generation and not failures:
            failures.append(1)
            raise httpx.ReadTimeout("slow")
        return real(request_)

    client = httpx.Client(transport=httpx.MockTransport(fail_first_listing_after_write))
    assert mcp.save_mcp_target(request(), core=core, http=client)["applied"] is True
    assert failures
    assert len(targets(core)) == 2


def test_existing_special_auth_and_tunnel_stay_on_edit(core, http):
    entry = {
        "name": "x",
        "policies": {"backendAuth": {"gcp": {}}, "backendTunnel": {"proxy": {"host": "127.0.0.1:3128"}}},
        "mcp": {"host": "http://x.test"},
    }
    core.text = yaml.safe_dump({"mcp": {"targets": [entry]}})
    shown = view(core, "x")
    mcp.save_mcp_target(
        request(name="x", host="http://x.test", condition="true", policies=shown["policies"]),
        "x",
        shown["revision"],
        core=core,
        http=http,
    )
    assert targets(core)[0]["policies"] == entry["policies"]
