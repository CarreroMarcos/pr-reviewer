"""Worker-role IAM ↔ dynamodb-usage contract (T067).

The 2026-09-27 incident class: `mutex.release` performs `delete_item`
on the state table, the worker role never had `dynamodb:DeleteItem`,
and the gap was invisible to every suite — state-machine tests run an
in-memory table, so authorization is never exercised. This pin keeps
the worker role's state-table grant in lockstep with the table
operations the lambda code actually performs: a call without a grant
is the crash class (AccessDenied mid-review); a grant without a call
is a dead privilege. Both fail loudly here.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
IAM_TF = (TERRAFORM_DIR / "iam.tf").read_text(encoding="utf-8")
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


def _worker_state_table_actions():
    """Actions of the worker's state-table statement — the grant block
    containing UpdateItem (the ingress statement grants GetItem/PutItem
    only, under a LeadingKeys condition)."""
    for block in re.findall(r"Action\s*=\s*\[(.*?)\]", IAM_TF, re.DOTALL):
        actions = set(re.findall(r'"(dynamodb:[A-Za-z]+)"', block))
        if "dynamodb:UpdateItem" in actions:
            return actions
    return set()


def test_worker_grants_every_table_operation_the_code_performs():
    methods = _worker_table_methods()
    assert methods, "worker table methods not found — the scan broke"
    unmapped = methods - set(OPERATION_ACTIONS)
    assert not unmapped, (
        "worker code calls table methods with no IAM mapping "
        f"{sorted(unmapped)} — extend OPERATION_ACTIONS and iam.tf together"
    )
    required = {OPERATION_ACTIONS[m] for m in methods}
    granted = _worker_state_table_actions()
    missing = required - granted
    assert not missing, f"worker table ops missing from the iam.tf grant: {sorted(missing)}"


def test_worker_state_table_grant_is_exact():
    """No dead privileges: the statement grants exactly the mapped
    actions. A wider grant needs a conscious map entry first."""
    assert _worker_state_table_actions() == set(OPERATION_ACTIONS.values())
