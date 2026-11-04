import base64
from types import SimpleNamespace

import httpx
import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from config import get_settings
from k8s.resources import ai_gateway_models as models
from k8s.resources.ai_gateways import GatewayRequestRefused

CONFIG = {
    "binds": [{"port": 8081, "listeners": [{"routes": [{"policies": {"apiKey": {"keys": [{"key": "$PORTAL_KEY"}]}}}]}]}],
    "llm": {
        "gateways": "default",
        "models": [
            {
                "name": "org/first",
                "provider": "openAI",
                "params": {"baseUrl": "http://llm.test/v1", "pathPrefix": "/kept"},
                "guardrails": {"kept": True},
            },
        ],
    },
}


class FakeCore:
    """Just enough of the Kubernetes API, with the gateway reacting to each config write."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.text = yaml.safe_dump(CONFIG, sort_keys=False)
        self.version = 1
        self.keys = {}
        self.nudges = 0
        self.writes = []
        self.conflict = False

    def read_namespaced_config_map(self, name, namespace):
        return SimpleNamespace(data={"config.yaml": self.text}, metadata=SimpleNamespace(resource_version=str(self.version)))

    def patch_namespaced_config_map(self, name, namespace, body):
        if self.conflict or body.metadata.resource_version != str(self.version):
            raise ApiException(status=409)
        self.text = body.data["config.yaml"]
        self.version += 1
        self.writes.append(self.text)
        self.gateway.config_written(self.text, self.keys)
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
        self.nudges += 1


class FakeGateway:
    def __init__(self):
        self.recovery_polls = 0
        self.recovery_delay = 0
        self.last_error = None
        self.names = ["org/first"]
        self.generation = 0
        self.reject_with = None
        self.key_arrives_late = 0

    def config_written(self, text, keys):
        document = yaml.safe_load(text)
        if self.reject_with and "guardrails-bad" in text:
            self.last_error = self.reject_with
            return
        if self.key_arrives_late:
            self.key_arrives_late -= 1
            self.last_error = "llm.models[1].params: read resource file /llm-keys/abc at line 8"
            return
        if self.last_error:
            self.recovery_polls = self.recovery_delay
        self.last_error = None
        self.names = [m["name"] for m in document["llm"]["models"]]
        self.generation += 1

    def handle(self, request):
        if request.url.path == "/api/runtime":
            if self.recovery_polls and not self.last_error:
                self.recovery_polls -= 1
                return httpx.Response(200, json={"configReload": {"synchronized": False, "lastError": "old error"}})
            return httpx.Response(200, json={"configReload": {"synchronized": not self.last_error, "lastError": self.last_error}})
        return httpx.Response(200, json=self.dump())

    def dump(self):
        return {
            "generation": self.generation,
            "binds": [],
            "backends": [
                {
                    "backend": {
                        "ai": {
                            "name": "llm",
                            "target": {"providers": [{"active": {name: {"endpoint": {"provider": {"openAI": {}}}} for name in self.names}}]},
                        }
                    }
                }
            ],
        }


@pytest.fixture(autouse=True)
def gateway_settings(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_URL", "http://gw.test:8081/config_dump")
    monkeypatch.setenv("AGENTGATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_NAMESPACE", "canvas")
    monkeypatch.setattr(models, "POLL_SECONDS", 0)
    monkeypatch.setattr(models, "NUDGED_DEADLINE_SECONDS", 0.05)
    get_settings.cache_clear()
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
        "provider": "anthropic",
        "visibility": None,
        "passthrough": None,
        "params": {},
        "apiKey": None,
        "removeApiKey": False,
        "advanced": {},
        **fields,
    }


def saved_models(core):
    return yaml.safe_load(core.text)["llm"]["models"]


def first_view(core):
    return models.list_model_configs(core)["models"][0]


def test_adding_a_model_keeps_the_rest_of_the_config(core, http):
    result = models.save_model(request(params={"baseUrl": "https://llm.test/v1", "model": None}), core=core, http=http)

    assert result["applied"] is True
    document = yaml.safe_load(core.text)
    assert document["binds"][0]["listeners"][0]["routes"][0]["policies"]["apiKey"]["keys"][0]["key"] == "$PORTAL_KEY"
    assert document["llm"]["models"][1] == {"name": "second", "provider": "anthropic", "params": {"baseUrl": "https://llm.test/v1"}}
    assert core.nudges >= 1


def test_duplicate_name_is_refused(core, http):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(name="org/first"), core=core, http=http)
    assert refused.value.status_code == 409
    assert core.writes == []


def test_editing_keeps_fields_the_form_does_not_show(core, http):
    view = first_view(core)
    assert view["advanced"] == {"guardrails": {"kept": True}}
    assert "pathPrefix" not in view["params"]

    models.save_model(
        request(name="org/first", provider="openAI", params={"model": "other"}, advanced=view["advanced"]),
        "org/first",
        view["revision"],
        core=core,
        http=http,
    )

    entry = saved_models(core)[0]
    assert entry["params"] == {"pathPrefix": "/kept", "model": "other"}
    assert entry["guardrails"] == {"kept": True}


def test_clearing_an_advanced_field_removes_it(core, http):
    view = first_view(core)
    models.save_model(
        request(name="org/first", provider="openAI", params=view["params"], advanced={}),
        "org/first",
        view["revision"],
        core=core,
        http=http,
    )
    assert "guardrails" not in saved_models(core)[0]


def test_an_edit_from_a_stale_form_is_refused(core, http):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(name="org/first"), "org/first", "old-revision", core=core, http=http)
    assert refused.value.status_code == 409
    assert core.writes == []


def test_an_unchanged_edit_writes_nothing(core, http):
    view = first_view(core)
    # pathPrefix stays because it isn't a form field, so this is the same entry.
    result = models.save_model(
        request(name="org/first", provider="openAI", params=view["params"], advanced=view["advanced"]),
        "org/first",
        view["revision"],
        core=core,
        http=http,
    )
    assert result["message"] == "Nothing changed."
    assert core.writes == []


def test_renaming_is_seen_through_the_new_name(core, http):
    view = first_view(core)
    result = models.save_model(
        request(name="renamed", provider="openAI", params=view["params"], advanced=view["advanced"]),
        "org/first",
        view["revision"],
        core=core,
        http=http,
    )
    assert result["applied"] is True
    assert [m["name"] for m in saved_models(core)] == ["renamed"]


def test_api_key_goes_in_the_secret_and_never_in_the_config(core, http):
    models.save_model(request(apiKey="sk-very-secret"), core=core, http=http)

    assert "sk-very-secret" not in core.text
    file_path = saved_models(core)[1]["params"]["apiKey"]["file"]
    assert file_path.startswith("/llm-keys/")
    stored = core.keys[file_path.rsplit("/", 1)[1]]
    assert base64.b64decode(stored).decode() == "sk-very-secret"

    views = models.list_model_configs(core)["models"]
    assert views[1]["apiKeySet"] is True
    assert "sk-very-secret" not in str(views)
    assert "apiKey" not in views[1]["params"]


def test_blank_key_on_edit_keeps_the_key(core, http):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    models.save_model(
        request(params={"model": "changed"}), "second", view["revision"], core=core, http=http
    )
    assert saved_models(core)[1]["params"]["apiKey"]["file"].startswith("/llm-keys/")
    assert len(core.keys) == 1


def test_replacing_a_key_deletes_the_old_one(core, http):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    old_keys = set(core.keys)
    view = models.list_model_configs(core)["models"][1]
    models.save_model(request(apiKey="sk-two"), "second", view["revision"], core=core, http=http)

    assert len(core.keys) == 1
    assert not old_keys & set(core.keys)


def test_removing_a_key(core, http):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    models.save_model(request(removeApiKey=True), "second", view["revision"], core=core, http=http)

    assert "params" not in saved_models(core)[1]
    assert core.keys == {}


def test_removing_a_model_deletes_its_key(core, http, gateway):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    result = models.remove_model("second", core=core, http=http)

    assert result["applied"] is True
    assert [m["name"] for m in saved_models(core)] == ["org/first"]
    assert core.keys == {}
    assert gateway.names == ["org/first"]


def test_removing_an_unknown_model_is_refused(core, http):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.remove_model("nope", core=core, http=http)
    assert refused.value.status_code == 404


def test_a_rejected_change_is_undone(core, http, gateway):
    gateway.reject_with = "llm.models[1]: unknown field `guardrails-bad`"
    original = core.text

    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(apiKey="sk-one", advanced={"guardrails-bad": {}}), core=core, http=http)

    assert refused.value.status_code == 422
    assert "unknown field" in refused.value.message
    assert core.text == original
    assert core.keys == {}
    assert gateway.last_error is None


def test_a_key_that_arrives_after_the_config_recovers(core, http, gateway):
    gateway.key_arrives_late = 1
    result = models.save_model(request(apiKey="sk-one"), core=core, http=http)

    assert result["applied"] is True
    assert len(core.writes) == 2
    assert core.keys
    assert "second" in gateway.names


@pytest.mark.parametrize(
    "fields",
    [
        {"params": {"baseUrl": "http://evil.test/$PORTAL_KEY"}},
        {"advanced": {"requestHeaders": {"add": {"x": "$PORTAL_KEY"}}}},
        {"advanced": {"$PORTAL_KEY": 1}},
        {"name": "$HOSTNAME"},
        {"provider": {"custom": {"model": "$HOSTNAME"}}},
    ],
)
def test_environment_variables_are_refused(core, http, fields):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(**fields), core=core, http=http)
    assert refused.value.status_code == 422
    assert core.writes == []


def test_a_dollar_in_the_api_key_is_fine(core, http):
    models.save_model(request(apiKey="sk-$abc"), core=core, http=http)
    assert core.keys


def test_an_existing_variable_can_stay(core, http):
    core.text = yaml.safe_dump(
        {"llm": {"models": [{"name": "m", "provider": "openAI", "params": {"apiKey": "$OPENAI_KEY", "model": "$M"}}]}}
    )
    view = first_view(core)
    assert view["apiKeySet"] is True
    models.save_model(
        request(name="m", provider="openAI", params={"model": "$M"}, passthrough="detect"),
        "m",
        view["revision"],
        core=core,
        http=http,
    )
    assert saved_models(core)[0]["params"]["apiKey"] == "$OPENAI_KEY"


@pytest.mark.parametrize(
    "fields",
    [
        {"advanced": {"name": "other"}},
        {"advanced": {"params": {}}},
        {"provider": {"custom": {}, "reference": "x"}},
        {"provider": {"unknown": {}}},
        {"provider": ""},
        {"params": {"baseUrl": "file:///etc/passwd"}},
    ],
)
def test_bad_requests_are_refused(core, http, fields):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(**fields), core=core, http=http)
    assert refused.value.status_code == 422
    assert core.writes == []


def test_refuses_when_the_gateway_is_already_failing(core, http, gateway):
    gateway.last_error = "bad"
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(), core=core, http=http)
    assert refused.value.status_code == 409
    assert core.writes == []


def test_conflicting_write_cleans_up_the_new_key(core, http):
    core.conflict = True
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(apiKey="sk-one"), core=core, http=http)
    assert refused.value.status_code == 409
    assert core.keys == {}


def test_only_one_change_at_a_time(core, http):
    assert models._change_lock.acquire(blocking=False)
    try:
        with pytest.raises(GatewayRequestRefused) as refused:
            models.save_model(request(), core=core, http=http)
        assert refused.value.status_code == 409
    finally:
        models._change_lock.release()


def test_unconfirmed_change_says_so(core, http):
    # The dump doesn't change when the gateway ignores this edit.
    core.gateway.config_written = lambda text, keys: None
    view = first_view(core)
    result = models.save_model(
        request(name="org/first", provider="openAI", advanced=view["advanced"]),
        "org/first",
        view["revision"],
        core=core,
        http=http,
    )
    assert result["applied"] is False


def test_not_configured(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_CONFIG_NAMESPACE", "")
    get_settings.cache_clear()
    assert models.list_model_configs() == {"configurable": False, "models": []}
    with pytest.raises(GatewayRequestRefused) as refused:
        models.remove_model("x")
    assert refused.value.status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"params": {"baseUrl": "http://evil.test"}},
        {"params": {"azureResourceName": "evil"}},
        {"provider": "openAI"},
        {"advanced": {"backendTunnel": {"proxy": {"host": "evil.test:1"}}}},
        {"advanced": {"requestHeaders": {"add": {"x": "y"}}}},
        {"advanced": {"somethingNew": 1}},
        {"advanced": {"guardrails": {"request": []}}},
    ],
)
def test_a_kept_key_is_not_sent_to_a_new_upstream(core, http, change):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    writes = len(core.writes)

    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(**change), "second", view["revision"], core=core, http=http)
    assert refused.value.status_code == 422
    assert len(core.writes) == writes

    models.save_model(request(apiKey="sk-two", **change), "second", view["revision"], core=core, http=http)
    assert len(core.writes) == writes + 1


def test_a_stale_missing_key_error_doesnt_use_up_the_deadline(core, http, gateway, monkeypatch):
    monkeypatch.setattr(models, "MISSING_KEY_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(models, "POLL_SECONDS", 0.005)
    monkeypatch.setattr(models, "NUDGED_DEADLINE_SECONDS", 2.0)
    gateway.key_arrives_late = 8
    assert models.save_model(request(apiKey="sk-one"), core=core, http=http)["applied"] is True
    assert core.keys


def test_a_key_file_that_never_arrives_is_undone(core, http, gateway, monkeypatch):
    monkeypatch.setattr(models, "MISSING_KEY_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(models, "POLL_SECONDS", 0.005)
    gateway.key_arrives_late = 10_000
    original = core.text
    with pytest.raises(GatewayRequestRefused):
        models.save_model(request(apiKey="sk-one"), core=core, http=http)
    assert core.text == original
    assert core.keys == {}


def test_a_failed_retry_write_is_undone_or_reported(core, http, gateway, monkeypatch):
    gateway.key_arrives_late = 10_000
    real_patch = core.patch_namespaced_config_map
    calls = []

    def fail_second_write(name, namespace, body):
        calls.append(1)
        if len(calls) == 2:
            raise ApiException(status=500)
        return real_patch(name, namespace, body)

    monkeypatch.setattr(core, "patch_namespaced_config_map", fail_second_write)
    original = core.text
    with pytest.raises(GatewayRequestRefused):
        models.save_model(request(apiKey="sk-one"), core=core, http=http)
    assert core.text == original
    assert core.keys == {}


@pytest.mark.parametrize("path", ["/llm-keys/openai", "/other/0123456789abcdef"])
def test_keys_the_portal_did_not_create_are_left_alone(core, http, path):
    core.keys = {"openai": "x"}
    core.text = yaml.safe_dump(
        {"llm": {"models": [{"name": "m", "provider": "openAI", "params": {"apiKey": {"file": path}}}]}}
    )
    models.remove_model("m", core=core, http=http)
    assert core.keys == {"openai": "x"}


def test_a_shared_key_is_kept_while_another_model_uses_it(core, http):
    path = "/llm-keys/0123456789abcdef"
    shared = {"provider": "openAI", "params": {"apiKey": {"file": path}}}
    core.keys = {"0123456789abcdef": "x"}
    core.text = yaml.safe_dump({"llm": {"models": [{"name": "a", **shared}, {"name": "b", **shared}]}})
    core.gateway.names = ["a", "b"]

    models.remove_model("a", core=core, http=http)
    assert core.keys
    models.remove_model("b", core=core, http=http)
    assert core.keys == {}


def test_an_unconfirmed_change_keeps_the_replaced_key(core, http, gateway):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    old = set(core.keys)
    gateway.config_written = lambda text, keys: None

    result = models.save_model(request(apiKey="sk-two"), "second", view["revision"], core=core, http=http)
    assert result["applied"] is False
    assert old <= set(core.keys)


def test_a_change_right_after_a_rejection_is_not_blocked(core, http, gateway):
    gateway.reject_with = "unknown field"
    gateway.recovery_delay = 3  # The gateway takes a moment to load the restored file.
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(advanced={"guardrails-bad": {}}), core=core, http=http)
    assert refused.value.status_code == 422
    assert gateway.recovery_polls == 0
    assert models.save_model(request(), core=core, http=http)["applied"] is True


def test_a_kept_key_allows_changes_that_keep_the_upstream(core, http):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    models.save_model(
        request(name="renamed", params={"model": "other"}, advanced={"defaults": {"temperature": 1}}),
        "second",
        view["revision"],
        core=core,
        http=http,
    )
    assert saved_models(core)[1]["params"]["model"] == "other"


def test_an_existing_variable_can_not_be_moved(core, http):
    core.text = yaml.safe_dump(
        {"llm": {"models": [{"name": "m", "provider": "openAI", "params": {"apiKey": "$OPENAI_KEY"}}]}}
    )
    view = first_view(core)
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(
            request(
                name="m",
                provider="openAI",
                removeApiKey=True,
                params={"baseUrl": "http://evil.test"},
                advanced={"requestHeaders": {"add": {"x": "$OPENAI_KEY"}}},
            ),
            "m",
            view["revision"],
            core=core,
            http=http,
        )
    assert refused.value.status_code == 422
    assert core.writes == []


@pytest.mark.parametrize(
    "secret",
    [
        {"requestHeaders": {"add": {"x": "$TOKEN"}}},
        {"auth": {"key": {"file": "/somewhere/else"}}},
    ],
)
def test_other_secrets_are_not_sent_to_a_new_upstream(core, http, secret):
    core.text = yaml.safe_dump({"llm": {"models": [{"name": "m", "provider": "openAI", **secret}]}})
    view = first_view(core)
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(
            request(name="m", provider="openAI", params={"baseUrl": "http://evil.test"}, advanced=view["advanced"]),
            "m",
            view["revision"],
            core=core,
            http=http,
        )
    assert refused.value.status_code == 422
    assert core.writes == []


@pytest.mark.parametrize(
    "fields",
    [
        {"advanced": {"auth": {"key": {"file": "/var/run/secrets/kubernetes.io/serviceaccount/token"}}}},
        {"advanced": {"auth": {"key": {"file": "/llm-keys/0123456789abcdef"}}}},
        {"advanced": {"tls": [{"file": "/etc/passwd"}]}},
        {"advanced": {"tls": {"cert": "/var/run/secrets/x", "key": "/var/run/secrets/y"}}},
        {"advanced": {"backendTLS": {"root": "/etc/passwd"}}},
        # The gateway reads this list form as a file too. A plain [path] is an inline value.
        {"advanced": {"auth": {"key": [{"file": "/var/run/secrets/x"}]}}},
        {"advanced": {"guardrails": {"request": [{"webhook": {"target": {"policies": {"backendTLS": {"key": "/k"}}}}}]}}},
        {"provider": {"custom": {"auth": {"file": "/etc/passwd"}}}},
    ],
)
def test_new_file_references_are_refused(core, http, fields):
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(request(**fields), core=core, http=http)
    assert refused.value.status_code == 422
    assert core.writes == []


def test_a_kept_key_file_can_not_be_moved_to_another_field(core, http):
    models.save_model(request(apiKey="sk-one"), core=core, http=http)
    view = models.list_model_configs(core)["models"][1]
    path = saved_models(core)[1]["params"]["apiKey"]["file"]
    writes = len(core.writes)
    with pytest.raises(GatewayRequestRefused) as refused:
        models.save_model(
            request(removeApiKey=True, advanced={"auth": {"key": {"file": path}}}),
            "second",
            view["revision"],
            core=core,
            http=http,
        )
    assert refused.value.status_code == 422
    assert len(core.writes) == writes


def test_an_existing_file_reference_stays_on_edit(core, http):
    auth = {"key": {"file": "/somewhere/else"}}
    core.text = yaml.safe_dump({"llm": {"models": [{"name": "m", "provider": "openAI", "auth": auth}]}})
    view = first_view(core)
    models.save_model(
        request(name="m", provider="openAI", params={"model": "other"}, advanced=view["advanced"]),
        "m",
        view["revision"],
        core=core,
        http=http,
    )
    assert saved_models(core)[0]["auth"] == auth
