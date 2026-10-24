"""Viewer routing integration — moto fake AWS (ARCHITECTURE.md §5.3).

Drives the real `viewer_handler.handler` with Function-URL v2 events
against moto's fake AWS — the DynamoDB state table + `pr-runs-index`
GSI (state.tf shape), and the archives bucket — same discipline as
test_fake_aws.py: real handlers, real boto3, no live network.

Pinned routes (the §5.3 tree, exactly) — every route public, no
credentials anywhere:

* `/runs/{pr}/{sha}/`          → static shell;
* `/static/{file}`             → `^static/[A-Za-z0-9._-]+$` AND explicit
  dot-segment rejection before key construction (the character class
  alone admits `static/..`);
* `/api/runs/{pr}/latest`      → GSI query, body is exactly
  `{run_id, sha, status, pipeline, archive_s3_key}`;
* `/runs/{pr}/{sha}/{rid}/{f}` → regex (rid = 32 hex, no
  dashes; f ∈ {events.jsonl, meta.json}) → S3 archive object.
"""

from __future__ import annotations

import json
import os

import boto3
import pytest
from moto import mock_aws

import viewer_handler  # noqa: E402

REGION = "us-west-2"
TABLE = "pr-reviewer-state"
BUCKET = "pr-reviewer-archives"
SHA = "a" * 40
RUN_ID = "b" * 32
RUN_ID_OLDER = "c" * 32
PR = 123


def _event(raw_path: str, method: str = "GET") -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": raw_path,
        "rawQueryString": "",
        "headers": {},
        "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.7"}},
        "isBase64Encoded": False,
    }


@pytest.fixture(scope="module")
def aws():
    # Lazy client creation inside the handler needs a region at first
    # call; tests run before any AWS env exists. Fixture-scoped, not
    # import-time — no process-env leak.
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
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
                    # UpdateExpression SET exactly these attrs — the
                    # fixture mirrors production, never invents
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


def _call(raw_path: str, method: str = "GET") -> dict:
    return viewer_handler.handler(_event(raw_path, method), None)


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
    # The character class alone admits `static/..` — the viewer must
    # reject dot segments before constructing the S3 key.
    assert _call(path)["statusCode"] == 404


@pytest.mark.parametrize("path", ["/static/app.css/x", "/static/a b", "/static/app.css/"])
def test_static_regex_rejects_out_of_class_paths(aws, path):
    assert _call(path)["statusCode"] == 404


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "AccessDenied"])
def test_missing_s3_key_maps_to_404_across_error_semantics(aws, monkeypatch, capsys, code):
    # Live S3 without s3:ListBucket answers GetObject for a missing key
    # with AccessDenied (anti-enumeration); moto returns NoSuchKey, so
    # CI cannot see that branch from moto alone. All three "absent"
    # codes must map to 404 — the surface stays {200, 404}.
    s3 = boto3.client("s3", region_name=REGION)

    class _Stub:
        exceptions = s3.exceptions  # mirror the real client's error classes —
        # the handler matches on `s3.exceptions.ClientError` via the
        # instance, so the stub must carry the same surface.

        def get_object(self, **kw):
            raise s3.exceptions.ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(viewer_handler, "_client", lambda name: _Stub())
    assert _call("/static/app.css")["statusCode"] == 404
    # The AccessDenied degradation signal is the point of the branch —
    # pin the log event, and pin that the unambiguous absent codes stay
    # silent.
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    if code == "AccessDenied":
        # The denied line carries the requested S3 key —
        # 404-vs-denied diagnosable from CloudWatch.
        assert events == [
            {"event": "viewer_s3_access_denied", "decision": "404", "key": "static/app.css"}
        ]
    else:
        assert events == []


@pytest.mark.parametrize("code", ["SlowDown", "ServiceUnavailable", "ThrottlingException"])
def test_non_absent_s3_errors_take_the_reraise_branch(aws, monkeypatch, code):
    # The re-raise branch must stay — non-absent codes (throttling /
    # outage) surface as the controlled 500 boundary, never as a 404
    # that masks an outage as "not found".
    s3 = boto3.client("s3", region_name=REGION)

    class _Stub:
        exceptions = s3.exceptions

        def get_object(self, **kw):
            raise s3.exceptions.ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(viewer_handler, "_client", lambda name: _Stub())
    assert _call("/static/app.css")["statusCode"] == 500


# --- /api/runs/{pr}/latest (public GSI) --------------------------------


def test_api_latest_served_public(aws):
    """No credentials: the latest-run lookup serves without any token."""
    r = _call(f"/api/runs/{PR}/latest")
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["run_id"] == RUN_ID


def test_api_latest_returns_latest_run_exact_shape(aws):
    r = _call(f"/api/runs/{PR}/latest")
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert set(body) == {"run_id", "sha", "status", "pipeline", "archive_s3_key"}
    assert body["run_id"] == RUN_ID  # latest wins (newer started_ts)
    assert body["sha"] == SHA
    assert r["headers"]["content-type"].startswith("application/json")


def test_api_latest_unknown_pr_404(aws):
    assert _call("/api/runs/456/latest")["statusCode"] == 404


# --- archive objects (public, strict regex) -----------------------------


def test_archive_events_jsonl_served(aws):
    r = _call(f"/runs/{PR}/{SHA}/{RUN_ID}/events.jsonl")
    assert r["statusCode"] == 200
    assert r["body"] == '{"e":1}\n'


def test_archive_meta_json_served(aws):
    r = _call(f"/runs/{PR}/{SHA}/{RUN_ID}/meta.json")
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
    assert _call(path)["statusCode"] == 404


# --- unparsed surfaces ----------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/favicon.ico", "/api/", "/runs/"])
def test_unknown_routes_404(aws, path):
    assert _call(path)["statusCode"] == 404


def test_non_get_methods_404(aws):
    assert _call(f"/api/runs/{PR}/latest", method="POST")["statusCode"] == 404
    assert _call(f"/runs/{PR}/{SHA}/", method="HEAD")["statusCode"] == 404


def test_api_latest_stale_row_without_run_id_never_500s(aws):
    """DynamoDB reprojects a GSI item only on rewrite, so rows
    predating the run_id projection lack it in the index until next
    write. The endpoint degrades to a key subset — never a KeyError
    500."""
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
    r = _call("/api/runs/789/latest")
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert "run_id" not in body
    assert body["sha"] == "e" * 40


def test_internal_error_boundary_returns_controlled_500(aws, monkeypatch):
    """An internal failure (DynamoDB outage, IAM misconfig) surfaces as a
    controlled JSON 500 — never an opaque stack or a provider error
    body."""

    def boom():
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(viewer_handler, "_state_table", boom)
    r = _call(f"/api/runs/{PR}/latest")
    assert r["statusCode"] == 500
    assert json.loads(r["body"]) == {"message": "internal error"}
