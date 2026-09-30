"""T053/T054: viewer routing integration — moto fake AWS (HLD §7 Routing Spec).

Drives the REAL `viewer_handler.handler` with Function-URL v2 events
against moto's fake AWS — SSM `/pr-reviewer/replay-token` (SecureString),
the DynamoDB state table + `pr-runs-index` GSI (state.tf shape), and the
archives bucket — same discipline as test_fake_aws.py: real handlers,
real boto3, no live network.

Creator half of the P2 pair: fails against the T051b stub, greens when
T054 lands the handler.

Pinned routes (the §7 tree, exactly):

* `/runs/{pr}/{sha}/`          → static shell, unauthenticated;
* `/static/{file}`             → `^static/[A-Za-z0-9._-]+$` AND explicit
  dot-segment rejection BEFORE key construction (bot review #3 — the
  character class alone admits `static/..`);
* `/api/runs/{pr}/latest`      → bearer + GSI query, body is EXACTLY
  `{run_id, sha, status, pipeline, archive_s3_key}`;
* `/runs/{pr}/{sha}/{rid}/{f}` → bearer + regex (rid = 32 hex, no
  dashes; f ∈ {events.jsonl, meta.json}) → S3 archive object.

Plus the Gate-39 F4 coupling pin: the handler's SSM literal must equal
the replay-token ARN path iam.tf grants (drift breaks at runtime as
500s on every authed route, so it is pinned at test time instead).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import viewer_handler  # noqa: E402

REGION = "us-west-2"
TABLE = "pr-reviewer-state"
BUCKET = "pr-reviewer-archives"
TOKEN = "view-token-0123456789abcdef"  # noqa: S105 — moto fixture value, not a credential
SHA = "a" * 40
RUN_ID = "b" * 32
RUN_ID_OLDER = "c" * 32
PR = 123


def _event(raw_path: str, token: str | None = None, method: str = "GET") -> dict:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": raw_path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.7"}},
        "isBase64Encoded": False,
    }


@pytest.fixture(scope="module")
def aws():
    # Lazy client creation inside the handler needs a region at first
    # call; tests run before any AWS env exists (mars-law clean shell).
    # Fixture-scoped, not import-time — no process-env leak (bot R2).
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name="/pr-reviewer/replay-token", Type="SecureString", Value=TOKEN)
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "pr_number", "AttributeType": "N"},
                {"AttributeName": "started_ts", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "pr-runs-index",
                    "KeySchema": [
                        {"AttributeName": "pr_number", "KeyType": "HASH"},
                        {"AttributeName": "started_ts", "KeyType": "RANGE"},
                    ],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": [
                            "run_id",
                            "sha",
                            "status",
                            "pipeline",
                            "archive_s3_key",
                            "archive_written_at",
                            "findings_n",
                        ],
                    },
                    "ProvisionedThroughput": {
                        "ReadCapacityUnits": 5,
                        "WriteCapacityUnits": 5,
                    },
                }
            ],
            ProvisionedThroughput={"ReadCapacityUnits": 20, "WriteCapacityUnits": 20},
        )
        for run_id, ts in (
            (RUN_ID, "2026-09-28T12:00:00Z"),
            (RUN_ID_OLDER, "2026-09-27T12:00:00Z"),
        ):
            ddb.put_item(
                TableName=TABLE,
                Item={  # writer-shaped: build_index_item + the worker
                    # UpdateExpression SET exactly these attrs (Gate-40
                    # F1 — the fixture mirrors production, never invents)
                    "pk": {"S": f"run#{run_id}"},
                    "run_id": {"S": run_id},
                    "pr_number": {"N": str(PR)},
                    "started_ts": {"S": ts},
                    "sha": {"S": SHA},
                    "status": {"S": "SUCCEEDED"},
                    "pipeline": {"S": "multi_agent"},
                    "archive_s3_key": {"S": f"runs/{PR}/{SHA}/{run_id}/events.jsonl"},
                },
            )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_object(Bucket=BUCKET, Key="static/index.html", Body=b"<html>shell</html>")
        s3.put_object(Bucket=BUCKET, Key="static/app.css", Body=b"body{}")
        s3.put_object(
            Bucket=BUCKET,
            Key=f"runs/{PR}/{SHA}/{RUN_ID}/events.jsonl",
            Body=b'{"e":1}\n',
        )
        s3.put_object(Bucket=BUCKET, Key=f"runs/{PR}/{SHA}/{RUN_ID}/meta.json", Body=b'{"m":1}')
        yield


def _call(raw_path: str, token: str | None = None, method: str = "GET") -> dict:
    return viewer_handler.handler(_event(raw_path, token, method), None)


# --- shell + static (unauthenticated) ------------------------------------


def test_static_shell_served_unauthenticated(aws):
    r = _call(f"/runs/{PR}/{SHA}/")
    assert r["statusCode"] == 200
    assert "shell" in r["body"]
    assert r["headers"]["content-type"].startswith("text/html")


def test_static_file_served(aws):
    r = _call("/static/app.css")
    assert r["statusCode"] == 200
    assert r["body"] == "body{}"


@pytest.mark.parametrize(
    "path", ["/static/..", "/static/../x", "/static/.", "/static/%2e%2e/x", "/static/./x"]
)
def test_static_rejects_dot_segments_before_key_build(aws, path):
    # bot review #3: the character class alone admits `static/..` — the
    # viewer must reject dot segments BEFORE constructing the S3 key.
    assert _call(path)["statusCode"] == 404


@pytest.mark.parametrize("path", ["/static/app.css/x", "/static/a b", "/static/app.css/"])
def test_static_regex_rejects_out_of_class_paths(aws, path):
    assert _call(path)["statusCode"] == 404


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "AccessDenied"])
def test_missing_s3_key_maps_to_404_across_error_semantics(aws, monkeypatch, capsys, code):
    # Canonical review #1 (PR #167): live S3 without s3:ListBucket
    # answers GetObject for a MISSING key with AccessDenied
    # (anti-enumeration); moto returns NoSuchKey, so CI never saw the
    # branch. All three "absent" codes must map to 404 — the surface
    # stays {200, 401, 404} (bot R2 discipline).
    s3 = boto3.client("s3", region_name=REGION)

    class _Stub:
        exceptions = s3.exceptions  # mirror the real client's error classes —
        # the handler matches on `s3.exceptions.ClientError` via the
        # instance, so the stub must carry the same surface.

        def get_object(self, **kw):
            raise s3.exceptions.ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(viewer_handler, "_client", lambda name: _Stub())
    assert _call("/static/app.css")["statusCode"] == 404
    # Canonical review #3: the AccessDenied degradation signal is the
    # point of the branch — pin the log event, and pin that the
    # unambiguous absent codes stay silent.
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    if code == "AccessDenied":
        assert events == [{"event": "viewer_s3_access_denied", "decision": "404"}]
    else:
        assert events == []


@pytest.mark.parametrize("code", ["SlowDown", "ServiceUnavailable", "ThrottlingException"])
def test_non_absent_s3_errors_take_the_reraise_branch(aws, monkeypatch, code):
    # Canonical review #2 (PR #167): the re-raise branch must stay —
    # non-absent codes (throttling/outage) surface as the controlled
    # 500 boundary, never as a 404 that masks an outage as "not found".
    s3 = boto3.client("s3", region_name=REGION)

    class _Stub:
        exceptions = s3.exceptions

        def get_object(self, **kw):
            raise s3.exceptions.ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(viewer_handler, "_client", lambda name: _Stub())
    assert _call("/static/app.css")["statusCode"] == 500


# --- /api/runs/{pr}/latest (bearer + GSI) --------------------------------


def test_api_latest_requires_bearer(aws):
    assert _call(f"/api/runs/{PR}/latest")["statusCode"] == 401


def test_api_latest_rejects_wrong_token(aws):
    assert (
        _call(f"/api/runs/{PR}/latest", token="wrong-token")["statusCode"] == 401  # noqa: S106
    )


def test_api_latest_returns_latest_run_exact_shape(aws):
    r = _call(f"/api/runs/{PR}/latest", token=TOKEN)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert set(body) == {"run_id", "sha", "status", "pipeline", "archive_s3_key"}
    assert body["run_id"] == RUN_ID  # latest wins (newer started_ts)
    assert body["sha"] == SHA
    assert r["headers"]["content-type"].startswith("application/json")


def test_api_latest_unknown_pr_404(aws):
    assert _call("/api/runs/456/latest", token=TOKEN)["statusCode"] == 404


# --- archive objects (bearer + strict regex) -----------------------------


def test_archive_requires_bearer(aws):
    assert _call(f"/runs/{PR}/{SHA}/{RUN_ID}/events.jsonl")["statusCode"] == 401


def test_archive_events_jsonl_served(aws):
    r = _call(f"/runs/{PR}/{SHA}/{RUN_ID}/events.jsonl", token=TOKEN)
    assert r["statusCode"] == 200
    assert r["body"] == '{"e":1}\n'


def test_archive_meta_json_served(aws):
    r = _call(f"/runs/{PR}/{SHA}/{RUN_ID}/meta.json", token=TOKEN)
    assert r["statusCode"] == 200
    assert r["body"] == '{"m":1}'


@pytest.mark.parametrize(
    "path",
    [
        f"/runs/{PR}/{SHA}/{'b' * 31}/events.jsonl",  # 31 hex — too short
        f"/runs/{PR}/{SHA}/{'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'[:32]}/meta.json",  # dashes
        f"/runs/{PR}/{SHA[:39]}/{RUN_ID}/events.jsonl",  # 39-hex sha
        f"/runs/{PR}/{SHA}/{RUN_ID}/findings.json",  # not in the allowlist
        f"/runs/{PR}/{SHA}/{RUN_ID}/../{RUN_ID}/meta.json",  # traversal
        "/runs/12x/" + SHA + "/" + RUN_ID + "/meta.json",  # non-numeric pr
    ],
)
def test_archive_regex_rejects_malformed(aws, path):
    assert _call(path, token=TOKEN)["statusCode"] == 404


# --- unparsed surfaces ----------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/favicon.ico", "/api/", "/runs/"])
def test_unknown_routes_404(aws, path):
    assert _call(path)["statusCode"] == 404


def test_non_get_methods_404(aws):
    assert _call(f"/api/runs/{PR}/latest", token=TOKEN, method="POST")["statusCode"] == 404
    assert _call(f"/runs/{PR}/{SHA}/", method="HEAD")["statusCode"] == 404


# --- Gate-39 F4 coupling: handler literal ↔ iam.tf grant ------------------


def test_ssm_literal_matches_iam_replay_token_grant():
    handler_src = (Path(__file__).resolve().parents[2] / "lambda" / "viewer_handler.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r'TOKEN_PARAMETER_NAME\s*=\s*"([^"]+)"', handler_src)
    assert m is not None, "viewer_handler TOKEN_PARAMETER_NAME literal not found"
    iam_src = (Path(__file__).resolve().parents[2] / "terraform" / "viewer.tf").read_text(
        encoding="utf-8"
    )
    arn_path = f"parameter/{m.group(1).lstrip('/')}"
    # Full coupling chain (bot F6, PR #149): the handler literal must
    # match the iam.tf LOCAL's value, and viewer.tf's policy must grant
    # that local — a stray mention in a comment or another role's grant
    # satisfies nothing.
    iam_tf = (Path(__file__).resolve().parents[2] / "terraform" / "iam.tf").read_text(
        encoding="utf-8"
    )
    local_def = re.search(r'replay_token\s*=\s*"([^"]*:parameter/[^"]+)"', iam_tf)
    assert local_def is not None, "iam.tf ssm_parameter_arn.replay_token local not found"
    assert local_def.group(1).endswith(arn_path), (
        f"iam.tf local {local_def.group(1)!r} does not end with the handler "
        f"path {arn_path!r} — drift would 500 every authed route (Gate-39 F4)"
    )
    policy = re.search(r'resource "aws_iam_role_policy" "viewer" \{(.*?)\n\}', iam_src, re.DOTALL)
    assert policy is not None, "aws_iam_role_policy.viewer not found in viewer.tf"
    assert "local.ssm_parameter_arn.replay_token" in policy.group(1), (
        "viewer.tf policy does not grant local.ssm_parameter_arn.replay_token "
        "— handler/iam drift would 500 every authed route (Gate-39 F4)"
    )


def test_api_latest_stale_row_without_run_id_never_500s(aws):
    """Bot F1 (PR #149): DynamoDB reprojects a GSI item only on rewrite,
    so rows predating the run_id projection (spec #148) lack it in the
    index until next write. The endpoint degrades to a key subset —
    never a KeyError 500."""
    boto3.client("dynamodb", region_name=REGION).put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "run#legacy"},
            "pr_number": {"N": "789"},
            "started_ts": {"S": "2026-09-26T12:00:00Z"},
            "sha": {"S": "e" * 40},
            "status": {"S": "SUCCEEDED"},
            "pipeline": {"S": "single_pass"},
        },  # no run_id, no archive_s3_key — pre-projection row shape
    )
    r = _call("/api/runs/789/latest", token=TOKEN)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert "run_id" not in body
    assert body["sha"] == "e" * 40


def test_bearer_scheme_case_insensitive(aws):
    """Bot F5 (PR #149): RFC 7235 — the auth scheme is case-insensitive."""
    headers_event = _event(f"/api/runs/{PR}/latest")
    headers_event["headers"]["authorization"] = f"bearer {TOKEN}"
    r = viewer_handler.handler(headers_event, None)
    assert r["statusCode"] == 200


def test_capitalized_authorization_header_authenticates(aws):
    """Gate-40 F2: every real client sends `Authorization` (capitalized);
    only the scheme case was pinned before. Function URLs preserve the
    client's header casing — the handler must lower() the NAME too."""
    headers_event = _event(f"/api/runs/{PR}/latest")
    headers_event["headers"] = {"Authorization": f"Bearer {TOKEN}"}
    r = viewer_handler.handler(headers_event, None)
    assert r["statusCode"] == 200


def test_internal_error_boundary_returns_controlled_500(aws, monkeypatch):
    """Bot F4 (PR #149): an internal failure (SSM outage, IAM misconfig)
    surfaces as a controlled JSON 500 — never an opaque stack or a
    provider error body."""

    def boom():
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(viewer_handler, "_expected_token", boom)
    r = _call(f"/api/runs/{PR}/latest", token=TOKEN)
    assert r["statusCode"] == 500
    assert json.loads(r["body"]) == {"message": "internal error"}
