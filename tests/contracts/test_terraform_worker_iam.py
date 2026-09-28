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
block parsing for the contract suite is SPR-155's scope.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
try:
    IAM_TF = (TERRAFORM_DIR / "iam.tf").read_text(encoding="utf-8")
except OSError as exc:
    raise RuntimeError(
        "cannot read terraform/iam.tf relative to tests/contracts — this "
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
    found = set()
    for path in WORKER_CODE:
        paths = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for source in paths:
            text = source.read_text(encoding="utf-8")
            found |= {m for m in TABLE_METHODS if re.search(rf"\.{m}\(", text)}
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
    """Union of dynamodb actions across ALL statements of the worker
    policy — a second (differently-scoped) dynamodb statement must not
    escape the pin."""
    block = _worker_policy_block()
    actions: set[str] = set()
    for action_block in re.findall(r"Action\s*=\s*\[(.*?)\]", block, re.DOTALL):
        actions |= set(re.findall(r'"(dynamodb:[A-Za-z]+)"', action_block))
    return actions


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
