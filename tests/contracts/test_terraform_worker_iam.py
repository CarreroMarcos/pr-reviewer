"""Worker-role IAM ↔ dynamodb-usage contract (T067).

The 2026-09-27 incident class: `mutex.release` performs `delete_item`
on the state table, the worker role never had `dynamodb:DeleteItem`,
and the gap was invisible to every suite — state-machine tests run an
in-memory table, so authorization is never exercised. This pin keeps
the worker role's state-table grant in lockstep with the table
operations the lambda code actually performs: a call without a grant
is the crash class (AccessDenied mid-review) and fails loudly; a
grant outside the conscious map fails loudly; a mapped-but-uncalled
grant is an allowed, visible residual (see below).

Known blind spots (a tripwire, not a proof): the scan is textual, so
occurrences inside comments or string literals count as call sites
(false positives are safe — they over-require grants); dynamic
dispatch (`getattr(table, op)`) and client-level (non-resource)
dynamo calls would bypass the scan entirely; the ingress role and
non-table actions (SSM, SQS, S3) are other surfaces with their own
contracts. Dead-privilege detection is map-level: a granted action
the worker code never calls (PutItem today — the delivery write lives
in ingress) is allowed as a consciously mapped residual, while a
grant outside the map fails. The `\\n}` resource anchor assumes no
column-0 `}` inside the body (terraform fmt output satisfies this);
a future here-doc would truncate the match, dropping actions, which
fails the missing-grant test in the safe direction — robust HCL
block parsing for the contract suite is SPR-155's scope. The
action-extraction regex likewise assumes list-form `Action = [...]`
statements; a string-form Action would be missed (also safe
direction: toward missing-grant).

S3 surface (T069, second 2026-09-27 gap): the archive contract's two
puts (worker_handler.py:1900-1901) had no grant AND no ARCHIVE_BUCKET
env (the `if not bucket` guard then skips silently — no warning), so
the bucket stayed empty since the T042 deploy. The pin covers the S3
call map the same way; the env wiring is pinned in
test_terraform_multi_agent (GLM_ALLOWED_HOSTS pop-row precedent).
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
try:
    IAM_TF = (TERRAFORM_DIR / "iam.tf").read_text(encoding="utf-8")
    COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")
    ARCHIVES_TF = (TERRAFORM_DIR / "archives.tf").read_text(encoding="utf-8")
except OSError as exc:
    raise RuntimeError(
        "cannot read terraform/*.tf relative to tests/contracts — this "
        "contract assumes the repo layout (repo root two levels up); if the "
        "layout moved, update TERRAFORM_DIR here"
    ) from exc
LAMBDA_DIR = Path(__file__).resolve().parent.parent.parent / "lambda"

# Every dynamodb table method the code could call → the IAM action it
# requires. A new call site must extend this map AND iam.tf in the same
# change; the unmapped-method pin below forces that consciously.
OPERATION_ACTIONS = {
    "get_item": "dynamodb:GetItem",
    "put_item": "dynamodb:PutItem",
    "update_item": "dynamodb:UpdateItem",
    "delete_item": "dynamodb:DeleteItem",
}

# Superset scan: these resource-style table methods are all
# authorization points; any that appears in worker code must have an
# entry in OPERATION_ACTIONS.
TABLE_METHODS = frozenset(OPERATION_ACTIONS) | {
    "query",
    "scan",
    "batch_get_item",
    "batch_write_item",
    "transact_get_items",
    "transact_write_items",
}

# The worker role executes the worker bundle: common/ + worker_handler.py.
# (ingress_handler.py runs under the ingress role, whose statement is
# scoped by dynamodb:LeadingKeys — a separate surface, not this contract.)
WORKER_CODE = (LAMBDA_DIR / "common", LAMBDA_DIR / "worker_handler.py")


def _worker_table_methods():
    return _worker_call_sites(TABLE_METHODS)


# S3 surface (T069): the archive path's two puts need object-level
# PutObject; a new call site must extend this map AND iam.tf together.
S3_OPERATION_ACTIONS = {
    "put_object": "s3:PutObject",
}

S3_METHODS = frozenset(S3_OPERATION_ACTIONS) | {
    "get_object",
    "head_object",
    "list_objects_v2",
    "delete_object",
    "copy_object",
    # High-level transfer APIs are multipart-capable: adopting one needs
    # extra grants (s3:AbortMultipartUpload, ListMultipartUploadParts)
    # added to S3_OPERATION_ACTIONS consciously — the unmapped tripwire
    # below forces that conversation.
    "upload_file",
    "download_file",
}


def _worker_s3_methods():
    return _worker_call_sites(S3_METHODS)


def _worker_call_sites(method_names):
    found = set()
    for path in WORKER_CODE:
        paths = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for source in paths:
            text = source.read_text(encoding="utf-8")
            found |= {m for m in method_names if re.search(rf"\.{m}\(", text)}
    return found


def _worker_policy_block():
    """The worker role's policy resource body — anchored by resource
    name, not inferred from action contents (role association must not
    drift if another statement gains a coincidental action)."""
    match = re.search(r'resource "aws_iam_role_policy" "worker" \{(.*?)\n\}', IAM_TF, re.DOTALL)
    assert match is not None, (
        "worker policy block not found in iam.tf — the scan broke "
        "(resource renamed, or the file reformatted past the \\n} anchor)"
    )
    return match.group(1)


def _worker_state_table_actions():
    return _worker_statement_actions("dynamodb")


def _worker_statement_actions(prefix):
    """Union of `<prefix>:*` actions across ALL statements of the
    worker policy — a second (differently-scoped) statement must not
    escape the pin."""
    block = _worker_policy_block()
    actions: set[str] = set()
    for action_block in re.findall(r"Action\s*=\s*\[(.*?)\]", block, re.DOTALL):
        actions |= set(re.findall(rf'"({prefix}:[A-Za-z]+)"', action_block))
    return actions


def _worker_s3_actions():
    return _worker_statement_actions("s3")


def _required_actions():
    return {OPERATION_ACTIONS[m] for m in _worker_table_methods()}


def test_no_unmapped_table_operations():
    methods = _worker_table_methods()
    assert methods, "worker table methods not found — the scan broke"
    unmapped = methods - set(OPERATION_ACTIONS)
    assert not unmapped, (
        "worker code calls table methods with no IAM mapping "
        f"{sorted(unmapped)} — extend OPERATION_ACTIONS and iam.tf together"
    )


def test_worker_grants_every_table_operation_the_code_performs():
    missing = _required_actions() - _worker_state_table_actions()
    assert not missing, f"worker table ops missing from the iam.tf grant: {sorted(missing)}"


def test_worker_grants_only_mapped_actions():
    """No unmapped grants: every granted action is a consciously mapped
    one. A grant outside the map (wildcards, future/renamed actions)
    fails; a mapped-but-uncalled grant is an accepted residual the map
    keeps visible (PutItem today — the delivery write lives in
    ingress)."""
    granted = _worker_state_table_actions()
    unmapped = granted - set(OPERATION_ACTIONS.values())
    assert not unmapped, (
        "granted actions outside the conscious map "
        f"{sorted(unmapped)} — extend OPERATION_ACTIONS or drop the grant"
    )


def test_worker_s3_grant_covers_the_archive_puts():
    required = {S3_OPERATION_ACTIONS[m] for m in _worker_s3_methods()}
    assert required, "worker s3 methods not found — the scan broke"
    missing = required - _worker_s3_actions()
    assert not missing, f"worker s3 ops missing from the iam.tf grant: {sorted(missing)}"


def test_worker_s3_grant_only_mapped():
    unmapped = _worker_s3_actions() - set(S3_OPERATION_ACTIONS.values())
    assert not unmapped, (
        "granted s3 actions outside the conscious map "
        f"{sorted(unmapped)} — extend S3_OPERATION_ACTIONS or drop the grant"
    )


def test_no_unmapped_s3_operations():
    methods = _worker_s3_methods()
    unmapped = methods - set(S3_OPERATION_ACTIONS)
    assert not unmapped, (
        "worker code calls s3 methods with no IAM mapping "
        f"{sorted(unmapped)} — extend S3_OPERATION_ACTIONS and iam.tf together"
    )


def test_archive_bucket_name_is_consistent_across_surfaces():
    """The env literal (compute.tf), the bucket resource (archives.tf)
    and the IAM resource reference must name the same bucket: literals
    that drift would send the puts somewhere the grant does not cover,
    or skip silently (the T069 failure mode)."""
    env = re.search(r'ARCHIVE_BUCKET\s*=\s*"([^"]*)"', COMPUTE_TF)
    assert env is not None, "ARCHIVE_BUCKET env line missing from compute.tf (the T069 wiring)"
    bucket = re.search(r'bucket\s*=\s*"([^"]*)"', ARCHIVES_TF)
    assert bucket is not None, "bucket name not found in archives.tf — the scan broke"
    assert env.group(1) == bucket.group(1), (
        f"compute.tf ARCHIVE_BUCKET {env.group(1)!r} != archives.tf bucket {bucket.group(1)!r}"
    )
    assert re.search(r'Resource\s*=\s*\["\$\{aws_s3_bucket\.archives\.arn\}/runs/\*"\]', IAM_TF), (
        "the worker S3 statement must target the archives bucket resource "
        "under the runs/ prefix (the key prefix archive_mod.s3_key writes)"
    )
