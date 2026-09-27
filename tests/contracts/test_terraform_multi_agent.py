"""T031: terraform multi-agent contract (HLD §8 checklists 3, 5, 7; §7 GSI).

Parses the terraform TEXT (no terraform binary needed — house pattern
from tests/contracts/test_terraform_contract.py) and pins the
multi-agent infrastructure surface:

* GSI `pr-runs-index` on the state table (`pr_number (N)` +
  `started_ts (S)`, 5/5, `INCLUDE` projecting exactly `["sha",
  "status", "pipeline", "archive_s3_key", "archive_written_at",
  "findings_n"]`) — HLD §7 + checklist 5;
* base table 20/20 rebalance (Always Free 25/25 envelope: 20 base + 5
  GSI) — checklist 5;
* ESM `scaling_config { maximum_concurrency = 2 }` — checklist 3;
* worker function unreserved (no `reserved_concurrent_executions`) —
  checklist 3;
* all 12 multi-agent env vars present with the T005 defaults, plus the
  naming pin (no bare `MARGIN_S` / `DEGRADED_BUDGET_MARGIN_S`), plus a
  three-way cross-check against `common.config` defaults — checklist 7.

(Alarm-shape assertions land with T034 in a later commit of this PR.)

RED state: GSI/ESM-scaling/env-vars are absent — assertion failures.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
STATE_TF = (TERRAFORM_DIR / "state.tf").read_text(encoding="utf-8")
COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")

# The 12 HLD §8 checklist-7 vars with the T005 defaults, exactly as they
# appear in HCL (all quoted — Lambda env is strings, matching the
# os.environ reads in common/config.py).
EXPECTED_ENV = {
    "MULTI_AGENT": "0",
    "MULTI_AGENT_PHASE0": "0",
    "FANOUT_CONCURRENCY": "3",
    "MUTEX_LEASE_TTL_S": "900",
    "REASONING_MAX_CHARS": "4000",
    "REASONING_EFFORT": "low",
    "WAVE_WAIT_FOR_S": "300",
    "VERIFIER_WAIT_FOR_S": "240",
    "SYNTHESIZER_WAIT_FOR_S": "180",
    "SINGLE_PASS_WAIT_FOR_S": "240",
    "SOCKET_READ_TIMEOUT_S": "240",
    "BUDGET_MARGIN_S": "60",
}

EXPECTED_PROJECTION = [
    "sha",
    "status",
    "pipeline",
    "archive_s3_key",
    "archive_written_at",
    "findings_n",
]


def _resource_block(text: str, resource_type: str, name: str) -> str:
    """Source of the named resource block, brace-matched (handles nested
    blocks and `${...}` interpolations — both balance)."""
    start = re.search(
        rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{', text
    )
    assert start is not None, f"resource {resource_type}.{name} not found"
    depth, i = 0, start.end() - 1
    while True:
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start.start() : i + 1]
        i += 1


def _int_assignment(block: str, key: str) -> int:
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*(?:#.*)?$", block, re.MULTILINE)
    assert match is not None, f"{key!r} int assignment not found"
    return int(match.group(1))


def _str_assignment(block: str, key: str) -> str:
    match = re.search(
        rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"\s*(?:#.*)?$', block, re.MULTILINE
    )
    assert match is not None, f"{key!r} string assignment not found"
    return match.group(1)


def _worker_env_map() -> dict[str, str]:
    """The worker `environment { variables = {...} }` mapping, parsed from
    the worker function block (HCL `"KEY" = "value"` lines)."""
    worker = _resource_block(COMPUTE_TF, "aws_lambda_function", "worker")
    env_start = re.search(r"environment\s*\{\s*variables\s*=\s*\{", worker)
    assert env_start is not None, "worker environment block not found"
    depth, i = 0, env_start.end() - 1
    while True:
        char = worker[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = worker[env_start.end() : i]
    pairs = re.findall(r'^\s*([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', body, re.MULTILINE)
    return dict(pairs)


def _gsi_block() -> str:
    table = _resource_block(STATE_TF, "aws_dynamodb_table", "state")
    gsi_start = re.search(r"global_secondary_index\s*\{", table)
    assert gsi_start is not None, "pr-runs-index GSI block not found"
    depth, i = 0, gsi_start.end() - 1
    while True:
        char = table[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return table[gsi_start.start() : i + 1]
        i += 1


# --- GSI + base-table rebalance (checklist 5; §7 GSI) ----------------------------------------


def test_gsi_pr_runs_index_exact_shape():
    gsi = _gsi_block()
    assert _str_assignment(gsi, "name") == "pr-runs-index"
    assert _str_assignment(gsi, "hash_key") == "pr_number"
    assert _str_assignment(gsi, "range_key") == "started_ts"
    assert _int_assignment(gsi, "read_capacity") == 5
    assert _int_assignment(gsi, "write_capacity") == 5
    assert _str_assignment(gsi, "projection_type") == "INCLUDE"
    projected = re.findall(r'"([a-z_0-9]+)"', gsi.split("non_key_attributes", 1)[1])
    assert projected == EXPECTED_PROJECTION


def test_gsi_key_attribute_types():
    table = _resource_block(STATE_TF, "aws_dynamodb_table", "state")
    assert re.search(
        r'attribute\s*\{\s*name\s*=\s*"pr_number"\s*type\s*=\s*"N"\s*\}', table
    ) is not None
    assert re.search(
        r'attribute\s*\{\s*name\s*=\s*"started_ts"\s*type\s*=\s*"S"\s*\}', table
    ) is not None


def test_base_table_20_20():
    table = _resource_block(STATE_TF, "aws_dynamodb_table", "state")
    # Scope the capacity reads to the TABLE level (before any nested
    # block): split off the first nested block.
    head = re.split(r"\n\s*(?:attribute|global_secondary_index|ttl)\s*\{", table, maxsplit=1)[0]
    assert _int_assignment(head, "read_capacity") == 20
    assert _int_assignment(head, "write_capacity") == 20


# --- ESM scaling + unreserved worker (checklist 3) ----------------------------------------------


def test_esm_scaling_max_concurrency_2():
    esm = _resource_block(COMPUTE_TF, "aws_lambda_event_source_mapping", "work")
    scaling = re.search(r"scaling_config\s*\{([^}]*)\}", esm)
    assert scaling is not None, "ESM scaling_config block not found"
    assert _int_assignment(scaling.group(1), "maximum_concurrency") == 2


def test_worker_unreserved():
    worker = _resource_block(COMPUTE_TF, "aws_lambda_function", "worker")
    assert "reserved_concurrent_executions" not in worker


# --- twelve env vars + naming pin + config cross-check (checklist 7) ---------------------------------


def test_twelve_env_vars_with_t005_defaults():
    assert _worker_env_map() == EXPECTED_ENV


def test_withdrawn_env_names_absent():
    assert re.search(r"^\s*MARGIN_S\s*=", COMPUTE_TF, re.MULTILINE) is None
    assert re.search(r"^\s*DEGRADED_BUDGET_MARGIN_S\s*=", COMPUTE_TF, re.MULTILINE) is None


def test_env_defaults_match_config():
    """Three-way pin (house pattern): terraform values equal the
    `common.config` defaults — one source of truth, no drift."""
    from common.config import (
        DEFAULT_BUDGET_MARGIN_S,
        DEFAULT_FANOUT_CONCURRENCY,
        DEFAULT_MULTI_AGENT,
        DEFAULT_MULTI_AGENT_PHASE0,
        DEFAULT_MUTEX_LEASE_TTL_S,
        DEFAULT_REASONING_EFFORT,
        DEFAULT_REASONING_MAX_CHARS,
        DEFAULT_SINGLE_PASS_WAIT_FOR_S,
        DEFAULT_SOCKET_READ_TIMEOUT_S,
        DEFAULT_SYNTHESIZER_WAIT_FOR_S,
        DEFAULT_VERIFIER_WAIT_FOR_S,
        DEFAULT_WAVE_WAIT_FOR_S,
    )

    assert EXPECTED_ENV == {
        "MULTI_AGENT": str(DEFAULT_MULTI_AGENT),
        "MULTI_AGENT_PHASE0": str(DEFAULT_MULTI_AGENT_PHASE0),
        "FANOUT_CONCURRENCY": str(DEFAULT_FANOUT_CONCURRENCY),
        "MUTEX_LEASE_TTL_S": str(DEFAULT_MUTEX_LEASE_TTL_S),
        "REASONING_MAX_CHARS": str(DEFAULT_REASONING_MAX_CHARS),
        "REASONING_EFFORT": DEFAULT_REASONING_EFFORT,
        "WAVE_WAIT_FOR_S": str(DEFAULT_WAVE_WAIT_FOR_S),
        "VERIFIER_WAIT_FOR_S": str(DEFAULT_VERIFIER_WAIT_FOR_S),
        "SYNTHESIZER_WAIT_FOR_S": str(DEFAULT_SYNTHESIZER_WAIT_FOR_S),
        "SINGLE_PASS_WAIT_FOR_S": str(DEFAULT_SINGLE_PASS_WAIT_FOR_S),
        "SOCKET_READ_TIMEOUT_S": str(DEFAULT_SOCKET_READ_TIMEOUT_S),
        "BUDGET_MARGIN_S": str(DEFAULT_BUDGET_MARGIN_S),
    }
