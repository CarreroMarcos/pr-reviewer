"""T013: config cache + hydration cases, injected clock/ssm (HLD §2.3 item 1, §2.6).

Cold-start fetch, 30-minute TTL refresh, 401 cache-bust via injected clock;
endpoint hydration rejects non-HTTPS / non-allow-list hosts. Every rejection
is typed: tests assert `ConfigError` plus its machine-readable
`field`/`reason`, never a bare `Exception`. The SSM accessor and clock are
injected — the config module never constructs clients itself.
"""

import pytest
from common.config import TTL_SECONDS, ConfigError, ConfigProvider

GITHUB_TOKEN = "/pr-reviewer/github-token"  # noqa: S105 (SSM path, not a credential)
WEBHOOK_SECRET = "/pr-reviewer/webhook-secret"  # noqa: S105 (SSM path, not a credential)
GLM_API_KEY = "/pr-reviewer/glm-api-key"
GLM_MODEL = "/pr-reviewer/glm-model"
GLM_ENDPOINT = "/pr-reviewer/glm-endpoint"

# HLD §2.6 parameter surface — hard-coded here so the tests verify the
# module fetches exactly these names (no invented parameters).
ALL_NAMES = (GITHUB_TOKEN, WEBHOOK_SECRET, GLM_API_KEY, GLM_MODEL, GLM_ENDPOINT)

ALLOWED_HOSTS = ("llm.example.com",)
ENDPOINT_URL = "https://llm.example.com/v1"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture, not a credential)
WEBHOOK_SECRET_VALUE = "webhook-secret-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"


def _values(endpoint=ENDPOINT_URL):
    return {
        GITHUB_TOKEN: GITHUB_TOKEN_VALUE,
        WEBHOOK_SECRET: WEBHOOK_SECRET_VALUE,
        GLM_API_KEY: GLM_API_KEY_VALUE,
        GLM_MODEL: "glm-5.3-flash",
        GLM_ENDPOINT: endpoint,
    }


class FakeSSM:
    """Injected SSM double mirroring the real GetParameters response shape."""

    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        self.calls.append({"Names": list(Names), "WithDecryption": WithDecryption})
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _provider(ssm, clock, hosts=ALLOWED_HOSTS):
    return ConfigProvider(ssm.get_parameters, clock=clock, allowed_endpoint_hosts=hosts)


def _rejected(provider, field, reason):
    with pytest.raises(ConfigError) as excinfo:
        provider.get()
    assert excinfo.value.field == field
    assert excinfo.value.reason == reason


# --- TTL -----------------------------------------------------------------


def test_ttl_is_thirty_minutes():
    assert TTL_SECONDS == 30 * 60


def test_cold_start_single_batched_fetch():
    ssm = FakeSSM(_values())
    provider = _provider(ssm, FakeClock())

    config = provider.get()

    assert len(ssm.calls) == 1
    call = ssm.calls[0]
    assert call["WithDecryption"] is True
    assert set(call["Names"]) == set(ALL_NAMES)
    assert config.glm_model == "glm-5.3-flash"
    assert config.glm_endpoint == ENDPOINT_URL
    assert config.github_token == GITHUB_TOKEN_VALUE


def test_second_access_within_ttl_does_not_refetch():
    ssm = FakeSSM(_values())
    clock = FakeClock()
    provider = _provider(ssm, clock)

    first = provider.get()
    clock.advance(TTL_SECONDS - 1)
    second = provider.get()

    assert len(ssm.calls) == 1
    assert second is first


def test_access_after_ttl_refetches():
    ssm = FakeSSM(_values())
    clock = FakeClock()
    provider = _provider(ssm, clock)

    provider.get()
    clock.advance(TTL_SECONDS + 1)
    ssm.values[GLM_MODEL] = "glm-5.3-flash-rotated"
    config = provider.get()

    assert len(ssm.calls) == 2
    assert config.glm_model == "glm-5.3-flash-rotated"


# --- 401 cache-bust ------------------------------------------------------


def test_401_cache_bust_refetches_immediately():
    ssm = FakeSSM(_values())
    clock = FakeClock()
    provider = _provider(ssm, clock)

    provider.get()
    assert len(ssm.calls) == 1

    provider.invalidate()  # 401 path: drop cache (HLD §2.3 item 8)
    ssm.values[GITHUB_TOKEN] = "ghp-rotated-token"
    config = provider.get()  # no clock advance — bust ignores TTL

    assert len(ssm.calls) == 2
    assert config.github_token == "ghp-rotated-token"  # noqa: S105 (fake fixture)


def test_bust_alias_invalidates_cache():
    ssm = FakeSSM(_values())
    provider = _provider(ssm, FakeClock())

    provider.get()
    provider.bust()
    provider.get()

    assert len(ssm.calls) == 2


# --- endpoint hydration --------------------------------------------------


def test_endpoint_rejects_non_https():
    ssm = FakeSSM(_values(endpoint="http://llm.example.com/v1"))
    _rejected(_provider(ssm, FakeClock()), "glm_endpoint", "bad_scheme")


def test_endpoint_rejects_non_allow_list_host():
    ssm = FakeSSM(_values(endpoint="https://evil.example.com/v1"))
    _rejected(_provider(ssm, FakeClock()), "glm_endpoint", "bad_host")


def test_endpoint_missing_path_still_validates_host():
    ssm = FakeSSM(_values(endpoint="https://llm.example.com"))
    config = _provider(ssm, FakeClock()).get()
    assert config.glm_endpoint == "https://llm.example.com"


# --- typed rejections ----------------------------------------------------


def test_config_error_is_value_error():
    assert issubclass(ConfigError, ValueError)


def test_missing_parameter_typed():
    values = _values()
    del values[GLM_API_KEY]
    _rejected(_provider(FakeSSM(values), FakeClock()), "glm_api_key", "missing")


def test_empty_parameter_value_typed():
    values = _values()
    values[GITHUB_TOKEN] = ""
    _rejected(_provider(FakeSSM(values), FakeClock()), "github_token", "empty")


def test_no_secrets_in_repr_or_errors():
    ssm = FakeSSM(_values())
    config = _provider(ssm, FakeClock()).get()

    redacted = repr(config)
    assert GITHUB_TOKEN_VALUE not in redacted
    assert WEBHOOK_SECRET_VALUE not in redacted
    assert GLM_API_KEY_VALUE not in redacted

    bad = FakeSSM(_values(endpoint="https://evil.example.com/v1"))
    with pytest.raises(ConfigError) as excinfo:
        _provider(bad, FakeClock()).get()
    assert GITHUB_TOKEN_VALUE not in str(excinfo.value)
    assert WEBHOOK_SECRET_VALUE not in str(excinfo.value)
    assert GLM_API_KEY_VALUE not in str(excinfo.value)
