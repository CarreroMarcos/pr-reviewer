"""Viewer Lambda — the internet-reachable replay-site surface
(ARCHITECTURE.md §5.3).

Sits behind the NONE-auth Function URL and routes per §5.3 — every
route is public (the reviewed repo is public, and the archived runs
carry nothing beyond it: published findings, agent reasoning, and run
metadata — no credentials, no non-public input):

    GET /runs/{pr}/{sha}/                         → static/index.html
    GET /static/{file}                            → S3 static/{file}
    GET /api/runs/{pr}/latest                     → GSI pr-runs-index query
    GET /runs/{pr}/{sha}/{rid}/{file}             → S3 archive object

Security posture (all pinned by tests/integration/test_viewer_routing.py):

* Dot-segment rejection happens BEFORE any S3 key is constructed — the
  `/static/` character class alone admits `static/..`; every decoded
  path segment resolving to `.` or `..` is a 404.
* Strict allowlist regexes on every route; anything unparsed is 404 — the
  URL surface is exactly the §5.3 tree, nothing more.
* Served bodies are UNTRUSTED model-controlled data: this handler
  returns bytes verbatim; sanitization is the render boundary
  (ARCHITECTURE.md §5.3, the static shell), not the transport's.
* Single-origin design (static shell + /api share ONE Function URL
  origin) — no CORS headers by design.

Self-contained by packaging: viewer.zip carries ONLY this module
(terraform/viewer.tf `archive_file`).
"""

from __future__ import annotations

import json
import re
import urllib.parse

import boto3

STATE_TABLE = "pr-reviewer-state"  # terraform/state.tf aws_dynamodb_table.state
ARCHIVES_BUCKET = "pr-reviewer-archives"  # terraform/archives.tf aws_s3_bucket.archives

_STATIC_RE = re.compile(r"^static/[A-Za-z0-9._-]+$")
_SHELL_RE = re.compile(r"^runs/\d+/[0-9a-f]{40}/?$")
_LATEST_RE = re.compile(r"^api/runs/(\d+)/latest$")
_ARCHIVE_RE = re.compile(r"^runs/\d+/[0-9a-f]{40}/[0-9a-f]{32}/(events\.jsonl|meta\.json)$")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}

_clients: dict[str, object] = {}
_table = None


def _client(name: str):
    """Lazy boto3 client cache: no clients at import (tests import this
    module without an AWS region), one client per runtime per service."""
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _state_table():
    """Cached DynamoDB table resource."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(STATE_TABLE)
    return _table


def _response(status: int, body: str, content_type: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": content_type},
        "body": body,
        "isBase64Encoded": False,
    }


def _not_found() -> dict:
    return _response(404, json.dumps({"message": "not found"}), "application/json")


def _serve_s3(key: str) -> dict:
    ext = key.rsplit(".", 1)[-1].lower()
    content_type = _CONTENT_TYPES.get(f".{ext}", "application/octet-stream")
    s3 = _client("s3")
    try:
        obj = s3.get_object(Bucket=ARCHIVES_BUCKET, Key=key)
    except s3.exceptions.ClientError as e:
        # Live S3 without s3:ListBucket answers GetObject for a MISSING
        # key with AccessDenied (anti-enumeration); moto returns
        # NoSuchKey. All three "absent" codes — NoSuchKey, the listless
        # NotFound variant, and AccessDenied — map to 404: the surface
        # stays {200, 404} and the 500 path never fires for absent
        # keys. Error classes ride the boto3 client instance
        # (stdlib+boto3 import contract).
        if e.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "AccessDenied"}:
            if e.response["Error"]["Code"] == "AccessDenied":
                # Ambiguous absent-vs-denied (no s3:ListBucket → live S3
                # answers missing keys with AccessDenied). API stays 404,
                # but the degradation signal is explicit in logs — a role
                # regression must be grep-able, never silent. The
                # requested key rides the line: pure path data the
                # requester already sent — 404-vs-denied is diagnosable
                # from CloudWatch alone.
                print(
                    json.dumps(
                        {
                            "event": "viewer_s3_access_denied",
                            "decision": "404",
                            "key": key,
                        }
                    ),
                    flush=True,
                )
            return _not_found()
        raise
    try:
        return _response(200, obj["Body"].read().decode("utf-8"), content_type)
    except UnicodeDecodeError:
        # Corrupt/foreign object: treat as absent — the API surface stays
        # {200, 404} and "unreadable" never leaks a 500.
        return _not_found()


def _serve_latest(pr: int) -> dict:
    result = _state_table().query(
        IndexName="pr-runs-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pr_number").eq(pr),
        ScanIndexForward=False,  # newest started_ts first (ARCH §5.3)
        Limit=1,
    )
    items = result.get("Items", [])
    if not items:
        return _not_found()
    # Subset semantics: DynamoDB reprojects a GSI item only on rewrite,
    # so rows predating the run_id projection lack it until next write
    # — return what exists, never 500.
    item = items[0]
    body = {
        k: item[k] for k in ("run_id", "sha", "status", "pipeline", "archive_s3_key") if k in item
    }
    return _response(200, json.dumps(body), "application/json")


def handler(event: dict, context) -> dict:  # noqa: ARG001 (Lambda signature)
    try:
        return _route(event)
    except Exception:  # controlled 500 — no internals leak
        return _response(500, json.dumps({"message": "internal error"}), "application/json")


def _route(event: dict) -> dict:
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "GET")
    if method != "GET":
        return _not_found()

    # Decode once; ALL routing (and the dot-segment gate) sees the
    # decoded path so `static/%2e%2e` cannot smuggle a traversal.
    path = urllib.parse.unquote(event.get("rawPath", "")).lstrip("/")
    segments = [s for s in path.split("/") if s]
    if any(seg in (".", "..") for seg in segments):
        return _not_found()  # before ANY key construction

    if _STATIC_RE.fullmatch(path):
        return _serve_s3(path)

    if _SHELL_RE.fullmatch(path):
        return _serve_s3("static/index.html")

    latest = _LATEST_RE.fullmatch(path)
    if latest:
        return _serve_latest(int(latest.group(1)))

    if _ARCHIVE_RE.fullmatch(path):
        return _serve_s3(path)

    return _not_found()
