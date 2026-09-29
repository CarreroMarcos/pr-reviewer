"""Viewer Lambda — the internet-reachable replay-site surface (HLD §7).

Sits behind the NONE-auth Function URL and routes per the §7 Routing
Specification:

    GET /runs/{pr}/{sha}/                         → static/index.html (unauth shell)
    GET /static/{file}                            → S3 static/{file}   (unauth)
    GET /api/runs/{pr}/latest   (Bearer)          → GSI pr-runs-index query
    GET /runs/{pr}/{sha}/{rid}/{file} (Bearer)    → S3 archive object

Security posture (all pinned by tests/integration/test_viewer_routing.py):

* Dot-segment rejection happens BEFORE any S3 key is constructed — the
  `/static/` character class alone admits `static/..` (bot review #3);
  every decoded path segment resolving to `.` or `..` is a 404.
* Strict allowlist regexes on every route; anything unparsed is 404 —
  the URL surface is exactly the §7 tree, nothing more.
* Bearer routes compare via `hmac.compare_digest` against the token in
  SSM. The token is read PER INVOCATION (`WithDecryption`) — no cache —
  so T056's rotation ("new SSM value, no code deploy") is effective on
  the next request without a recycle. No `kms:Decrypt` is needed: the
  parameter is a plain SecureString on the AWS-managed `aws/ssm` key
  and SSM decrypts server-side (HLD §2.6; DECISIONS 2026-09-28
  superseded §7's decrypt clause).
* Served bodies are UNTRUSTED model-controlled data (§7 raw-archive
  contract): this handler returns bytes verbatim; sanitization is the
  render boundary (§5, T055's static shell), not the transport's.
* Single-origin design (static shell + /api share ONE Function URL
  origin) — no CORS headers by design (Gate-38 refutation).

Self-contained by packaging: viewer.zip carries ONLY this module
(terraform/viewer.tf `archive_file`), so the resource names below are
pinned here and kept in lockstep with terraform via the routing suite
(SSM-literal ↔ iam.tf grant coupling, Gate-39 F4).
"""

from __future__ import annotations

import hmac
import json
import re
import urllib.parse

import boto3

STATE_TABLE = "pr-reviewer-state"  # terraform/state.tf aws_dynamodb_table.state
ARCHIVES_BUCKET = "pr-reviewer-archives"  # terraform/archives.tf aws_s3_bucket.archives
TOKEN_PARAMETER_NAME = "/pr-reviewer/replay-token"  # noqa: S105 — SSM path, not a credential (house convention, config.py:30)

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
    """Cached DynamoDB table resource (bot F3, PR #149)."""
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


def _unauthorized() -> dict:
    return _response(401, json.dumps({"message": "unauthorized"}), "application/json")


def _expected_token() -> str:
    param = _client("ssm").get_parameter(Name=TOKEN_PARAMETER_NAME, WithDecryption=True)
    return param["Parameter"]["Value"]


def _authorized(headers: dict) -> bool:
    """Bearer check: scheme prefix (case-insensitive per RFC 7235 — bot
    F5), then constant-time token compare."""
    raw = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value
            break
    if not raw:
        return False
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], _expected_token())


def _serve_s3(key: str) -> dict:
    ext = key.rsplit(".", 1)[-1].lower()
    content_type = _CONTENT_TYPES.get(f".{ext}", "application/octet-stream")
    s3 = _client("s3")
    try:
        obj = s3.get_object(Bucket=ARCHIVES_BUCKET, Key=key)
    except s3.exceptions.NoSuchKey:
        return _not_found()
    try:
        return _response(200, obj["Body"].read().decode("utf-8"), content_type)
    except UnicodeDecodeError:
        # Corrupt/foreign object: treat as absent — the API surface stays
        # {200, 401, 404} and "unreadable" never leaks a 500 (bot R2).
        return _not_found()


def _serve_latest(pr: int) -> dict:
    result = _state_table().query(
        IndexName="pr-runs-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pr_number").eq(pr),
        ScanIndexForward=False,  # newest started_ts first (HLD §7 latest-run)
        Limit=1,
    )
    items = result.get("Items", [])
    if not items:
        return _not_found()
    # Subset semantics (bot F1, PR #149): DynamoDB reprojects a GSI item
    # only on rewrite, so rows predating the run_id projection (spec
    # #148) lack it until next write — return what exists, never 500.
    item = items[0]
    body = {
        k: item[k] for k in ("run_id", "sha", "status", "pipeline", "archive_s3_key") if k in item
    }
    return _response(200, json.dumps(body), "application/json")


def handler(event: dict, context) -> dict:  # noqa: ARG001 (Lambda signature)
    try:
        return _route(event)
    except Exception:  # controlled 500 — no internals leak (bot F4/F2)
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
        return _not_found()  # before ANY key construction (bot review #3)

    if _STATIC_RE.fullmatch(path):
        return _serve_s3(path)

    if _SHELL_RE.fullmatch(path):
        return _serve_s3("static/index.html")

    latest = _LATEST_RE.fullmatch(path)
    if latest:
        if not _authorized(event.get("headers", {}) or {}):
            return _unauthorized()
        return _serve_latest(int(latest.group(1)))

    if _ARCHIVE_RE.fullmatch(path):
        if not _authorized(event.get("headers", {}) or {}):
            return _unauthorized()
        return _serve_s3(path)

    return _not_found()
