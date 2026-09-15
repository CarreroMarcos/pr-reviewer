"""SSM-backed runtime config (HLD §2.3 item 1, §2.6; Constitution III).

One batched `GetParameters` call at cold start (cold start always fetches);
cached in warm-container state behind this injectable provider — never
re-fetched mid-warm except on 401 (`invalidate()`/`bust()` → next `get()`
re-fetches immediately) or the 30-minute TTL. The SSM accessor and the clock
are constructor-injected so tests stay parallel-safe and free of
module-global state (HLD §4.4 item 3); this module never constructs clients
itself. Production passes `boto3.client("ssm").get_parameters` in.

Endpoint hydration validates HTTPS scheme + host allow-list (HLD §2.3
item 5); the allow-list is injected (secure default: empty — every host is
rejected until explicitly allowed). Missing/invalid parameters raise typed
`ConfigError` (a `ValueError`) with machine-readable `field`/`reason`.

Secrets never appear in `repr()` or error text (Constitution III).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

TTL_SECONDS = 30 * 60

GITHUB_TOKEN_NAME = "/pr-reviewer/github-token"  # noqa: S105 (SSM path, not a credential)
WEBHOOK_SECRET_NAME = "/pr-reviewer/webhook-secret"  # noqa: S105 (SSM path)
GLM_API_KEY_NAME = "/pr-reviewer/glm-api-key"
GLM_MODEL_NAME = "/pr-reviewer/glm-model"
GLM_ENDPOINT_NAME = "/pr-reviewer/glm-endpoint"

# Worker fetch surface (HLD §2.6) — one batched GetParameters call. The
# webhook secret is NOT in this set: §2.6 gates it "Ingress-only IAM" (the
# ingress fetches it via its singular GetParameter path), and iam.tf grants
# the worker exactly these four ARNs — one unauthorized name in a batched
# GetParameters denies the whole call.
PARAMETER_NAMES = (
    GITHUB_TOKEN_NAME,
    GLM_API_KEY_NAME,
    GLM_MODEL_NAME,
    GLM_ENDPOINT_NAME,
)

_FIELD_BY_NAME = {
    GITHUB_TOKEN_NAME: "github_token",
    GLM_API_KEY_NAME: "glm_api_key",
    GLM_MODEL_NAME: "glm_model",
    GLM_ENDPOINT_NAME: "glm_endpoint",
}


class ConfigError(ValueError):
    """Typed config rejection: `field` names the logical parameter
    (`github_token`, `glm_api_key`, `glm_model`, `glm_endpoint`),
    `reason` is a machine-readable code (`missing`,
    `empty`, `bad_scheme`, `bad_host`). Never carries secret values."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid config: {field}: {reason}")


@dataclass(frozen=True, repr=False)
class AppConfig:
    """Hydrated runtime config — all values decrypted via `WithDecryption`.
    Worker surface per HLD §2.6: the webhook secret is ingress-only and is
    fetched by the ingress helper, never through this provider."""

    github_token: str
    glm_api_key: str
    glm_model: str
    glm_endpoint: str

    def __repr__(self) -> str:
        return (
            f"AppConfig(glm_model={self.glm_model!r}, "
            f"glm_endpoint={self.glm_endpoint!r}, secrets=<redacted>)"
        )


# Injected SSM accessor: mirrors `SSM.Client.get_parameters` so production
# can pass the bound method directly. `Names`/`WithDecryption` keep boto3
# casing; response shape is the real GetParameters shape
# (`{"Parameters": [{"Name", "Value"}], "InvalidParameters": [...]}`).
SsmAccessor = Callable[..., dict[str, Any]]

Clock = Callable[[], float]


class ConfigProvider:
    """Cached SSM config with TTL + 401 cache-bust (HLD §2.3 item 1)."""

    def __init__(
        self,
        ssm_accessor: SsmAccessor,
        clock: Clock | None = None,
        parameter_names: tuple[str, ...] | None = None,
        allowed_endpoint_hosts: tuple[str, ...] = (),
    ) -> None:
        self._ssm = ssm_accessor
        self._clock = clock if clock is not None else time.time
        self._names = tuple(parameter_names) if parameter_names is not None else PARAMETER_NAMES
        self._allowed_hosts = frozenset(h.lower() for h in allowed_endpoint_hosts)
        self._cached: AppConfig | None = None
        self._fetched_at = 0.0

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Env-configured endpoint host set, lowercased at ingest."""
        return self._allowed_hosts

    def get(self) -> AppConfig:
        """Return the cached config, re-fetching on cold start, TTL expiry,
        or after `invalidate()`/`bust()`. A backwards clock (negative
        elapsed) simply yields no refetch — accepted; the HLD is silent
        on clock behavior, so this documents rather than fixes it."""
        now = self._clock()
        if self._cached is None or (now - self._fetched_at) >= TTL_SECONDS:
            self._cached = self._fetch()
            self._fetched_at = now
        return self._cached

    def invalidate(self) -> None:
        """Drop the cache (401 path, HLD §2.3 item 8): the next `get()`
        re-fetches immediately regardless of TTL."""
        self._cached = None

    def bust(self) -> None:
        """Alias of `invalidate()` for the 401 cache-bust entrypoint."""
        self.invalidate()

    def _fetch(self) -> AppConfig:
        response = self._ssm(Names=list(self._names), WithDecryption=True)
        invalid = set(response.get("InvalidParameters", []))
        by_name = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
        values: dict[str, str] = {}
        for name in self._names:
            field = _FIELD_BY_NAME.get(name, name)
            if name in invalid or name not in by_name:
                raise ConfigError(field, "missing")
            value = by_name[name]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(field, "empty")
            values[field] = value
        for field in ("github_token", "glm_api_key", "glm_model", "glm_endpoint"):
            if field not in values:
                raise ConfigError(field, "missing")
        self._check_endpoint(values["glm_endpoint"])
        return AppConfig(
            github_token=values["github_token"],
            glm_api_key=values["glm_api_key"],
            glm_model=values["glm_model"],
            glm_endpoint=values["glm_endpoint"],
        )

    def _check_endpoint(self, endpoint: str) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme.lower() != "https":
            raise ConfigError("glm_endpoint", "bad_scheme")
        host = (parts.hostname or "").lower()
        if not host or host not in self._allowed_hosts:
            raise ConfigError("glm_endpoint", "bad_host")
