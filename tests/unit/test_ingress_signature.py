"""Raw-bytes HMAC contract (HLD §2.1 step 2): ingress verifies the signature
over the exact raw request bytes — never over re-serialized JSON."""

import hashlib
import hmac
import json

from ingress_handler import verify_signature

FIXED_SECRET = "unit-test-secret-not-a-credential"  # noqa: S105 (dummy fixture)


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(FIXED_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_signature_over_exact_raw_bytes_passes() -> None:
    payload = {"b": 2, "a": 1}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    assert verify_signature(FIXED_SECRET, raw, _sign(raw)) is True


def test_reserialized_same_json_with_different_bytes_fails() -> None:
    payload = {"b": 2, "a": 1}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    reserialized = json.dumps(payload, separators=(", ", ": "), sort_keys=False).encode()
    assert reserialized != raw
    assert verify_signature(FIXED_SECRET, reserialized, _sign(raw)) is False
