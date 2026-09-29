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

# Lazy client creation inside the handler needs a region at first call;
# tests run before any AWS env exists (mars-law clean shell).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

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
                Item={
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
    iam_src = (Path(__file__).resolve().parents[2] / "terraform" / "iam.tf").read_text(
        encoding="utf-8"
    )
    arn_path = f"parameter/{m.group(1).lstrip('/')}"
    assert arn_path in iam_src, (
        f"iam.tf does not grant {arn_path} — handler/iam drift would 500 "
        "every authed route at runtime (Gate-39 F4)"
    )
