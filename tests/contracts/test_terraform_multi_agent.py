"""T031: terraform multi-agent contract (HLD §8 checklists 3, 5, 7; §7 GSI).

Parses the terraform TEXT (no terraform binary needed — house pattern
from tests/contracts/test_terraform_contract.py) and pins the
multi-agent infrastructure surface:

* GSI `pr-runs-index` on the state table (`pr_number (N)` +
  `started_ts (S)`, 5/5, `INCLUDE` projecting exactly `["run_id",
  "sha", "status", "pipeline", "archive_s3_key", "archive_written_at",
  "findings_n"]` — HLD §7 + checklist 5; run_id projected per the Mars
  ruling 2026-09-28, DECISIONS);
* base table 20/20 rebalance (Always Free 25/25 envelope: 20 base + 5
  GSI) — checklist 5;
* ESM `scaling_config { maximum_concurrency = 2 }` — checklist 3;
* worker function unreserved (no `reserved_concurrent_executions`) —
  checklist 3;
* all 12 multi-agent env vars present with the T005 defaults, plus the
  naming pin (no bare `MARGIN_S` / `DEGRADED_BUDGET_MARGIN_S`), plus a
  three-way cross-check against `common.config` defaults — checklist 7;
* viewer role read policy (`aws_iam_role_policy.viewer`): `s3:GetObject`
  on the archives bucket's `runs/*` + `static/*`, `dynamodb:Query`
  scoped to the `pr-runs-index` GSI ARN, `ssm:GetParameter` on the
  replay-token ARN, and NO `kms:Decrypt` anywhere (Mars ruling
  2026-09-28 — §7's decrypt clause superseded by §2.6 mechanics,
  DECISIONS) — HLD §7 Viewer IAM.

(Alarm-shape assertions included: the daily LLM-spend alarm threshold
derives from the budget variable × the 5-call factor — the Mars-set
budget value is snapshotted (T042, 2026-09-27) and drift = fail.)

Commit-ladder note: these assertions failed red against the pre-GSI
terraform (T031's creator commit, re-proven in a detached worktree); at
this head they pin the final green surface.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
STATE_TF = (TERRAFORM_DIR / "state.tf").read_text(encoding="utf-8")
COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")
OBSERVABILITY_TF = (TERRAFORM_DIR / "observability.tf").read_text(encoding="utf-8")
VARIABLES_TF = (TERRAFORM_DIR / "variables.tf").read_text(encoding="utf-8")
VIEWER_TF = (TERRAFORM_DIR / "viewer.tf").read_text(encoding="utf-8")
IAM_TF = (TERRAFORM_DIR / "iam.tf").read_text(encoding="utf-8")

# The 12 HLD §8 checklist-7 vars. T005 defaults for all but MULTI_AGENT,
# which T050 flipped to "1" at deploy time (Mars approval 2026-09-30; D8
# gates passed via the r7 record — DECISIONS 2026-09-30). All quoted —
# Lambda env is strings, matching the os.environ reads in common/config.py.
EXPECTED_ENV = {
    "MULTI_AGENT": "1",  # T050 activation; common.config default stays 0 (shadow fallback)
    "MULTI_AGENT_PHASE0": "1",
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
    "run_id",
    "sha",
    "status",
    "pipeline",
    "archive_s3_key",
    "archive_written_at",
    "findings_n",
]


def _resource_block(text: str, resource_type: str, name: str, kind: str = "resource") -> str:
    """Source of the named block, brace-matched (handles nested
    blocks and `${...}` interpolations — both balance). `kind` covers
    `resource` blocks (`resource_type` + `name`) and `variable` blocks
    (`resource_type` is the variable name, `name` ignored)."""
    if kind == "resource":
        pattern = rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{'
        label = f"resource {resource_type}.{name}"
    else:
        pattern = rf'variable\s+"{re.escape(resource_type)}"\s*\{{'
        label = f"variable {resource_type}"
    start = re.search(pattern, text)
    assert start is not None, f"{label} not found"
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
    match = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"\s*(?:#.*)?$', block, re.MULTILINE)
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
    assert (
        re.search(r'attribute\s*\{\s*name\s*=\s*"pr_number"\s*type\s*=\s*"N"\s*\}', table)
        is not None
    )
    assert (
        re.search(r'attribute\s*\{\s*name\s*=\s*"started_ts"\s*type\s*=\s*"S"\s*\}', table)
        is not None
    )


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


# --- twelve env vars + naming pin + config cross-check (list 7) ------------------------------


def test_twelve_env_vars_with_t005_defaults():
    env = _worker_env_map()
    assert env.pop("GLM_ALLOWED_HOSTS") == "api.z.ai"  # pre-existing, untouched
    # Archive contract target (T030, wired live by T069): read directly
    # by worker_handler (os.environ), not through common.config.
    assert env.pop("ARCHIVE_BUCKET") == "pr-reviewer-archives"
    # T075: replay footer base — wired from the viewer Function URL
    # resource, pinned in its own test below; not a §8 12-var knob.
    assert env.pop("REPLAY_BASE_URL") == "${aws_lambda_function_url.viewer.function_url}"
    assert env == EXPECTED_ENV


def test_worker_replay_base_url_is_viewer_function_url():
    """T075: the replay footer's base rides the viewer Function URL
    resource attribute — never a committed literal (PR #167 canonical
    LOW posture); outside the §8 12-var contract (spec PR #168)."""
    env = _worker_env_map()
    assert env["REPLAY_BASE_URL"] == "${aws_lambda_function_url.viewer.function_url}"


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

    # The str() cross-check presumes HCL-string-safe defaults: int or str,
    # never bool ("True" != HCL "1") or float-repr drift. Pins the premise
    # so a future type change fails loudly here, not subtly in terraform.
    for name, value in [
        ("MULTI_AGENT", DEFAULT_MULTI_AGENT),
        ("MULTI_AGENT_PHASE0", DEFAULT_MULTI_AGENT_PHASE0),
        ("FANOUT_CONCURRENCY", DEFAULT_FANOUT_CONCURRENCY),
        ("MUTEX_LEASE_TTL_S", DEFAULT_MUTEX_LEASE_TTL_S),
        ("REASONING_MAX_CHARS", DEFAULT_REASONING_MAX_CHARS),
        ("REASONING_EFFORT", DEFAULT_REASONING_EFFORT),
        ("WAVE_WAIT_FOR_S", DEFAULT_WAVE_WAIT_FOR_S),
        ("VERIFIER_WAIT_FOR_S", DEFAULT_VERIFIER_WAIT_FOR_S),
        ("SYNTHESIZER_WAIT_FOR_S", DEFAULT_SYNTHESIZER_WAIT_FOR_S),
        ("SINGLE_PASS_WAIT_FOR_S", DEFAULT_SINGLE_PASS_WAIT_FOR_S),
        ("SOCKET_READ_TIMEOUT_S", DEFAULT_SOCKET_READ_TIMEOUT_S),
        ("BUDGET_MARGIN_S", DEFAULT_BUDGET_MARGIN_S),
    ]:
        assert isinstance(value, (int, str)) and not isinstance(value, bool), (
            f"{name} must be int/str for the str() cross-check, got {type(value).__name__}"
        )

    # MULTI_AGENT is the ONE documented divergence from the three-way pin:
    # deployed "1" per T050 activation (Mars approval 2026-09-30), while
    # the code default stays 0 so a lost env var degrades to shadow mode,
    # never single-pass. Every other var: terraform == config default.
    assert EXPECTED_ENV == {
        "MULTI_AGENT": "1",
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


# --- daily spend alarm recalibration (checklist 8; T034) ---------------------------------------


def test_spend_alarm_threshold():
    """T042 snapshot (Mars, 2026-09-27): the alarm exists, its threshold
    derives from the config budget scaled by the 5-call factor, and the
    budget default is pinned to the Mars-set $1 ($5/day alarm) — any
    drift from that number fails."""
    alarm = _resource_block(OBSERVABILITY_TF, "aws_cloudwatch_metric_alarm", "daily_llm_spend")
    threshold = re.search(r"^\s*threshold\s*=\s*(.+?)\s*(?:#.*)?$", alarm, re.MULTILINE)
    assert threshold is not None, "daily_llm_spend threshold not found"
    threshold_norm = " ".join(threshold.group(1).split())
    # Whitespace-normalized (innocent refactors must not trip drift) but
    # order-strict: `5 * var.…` or any other factor form still fails, so
    # the ×5 factor itself stays pinned.
    assert threshold_norm == "var.daily_llm_spend_budget_usd * 5", (
        f"threshold not budget x5: {threshold.group(1)!r}"
    )
    var_block = _resource_block(VARIABLES_TF, "daily_llm_spend_budget_usd", "", kind="variable")
    default = re.search(r"^\s*default\s*=\s*(\d+(?:\.\d+)?)\s*$", var_block, re.MULTILINE)
    assert default is not None, "budget default is not a numeric literal"
    assert float(default.group(1)) == 1.0, (
        f"budget drifted from the Mars-set $1: {default.group(1)!r}"
    )


# --- contention-rate alarm (HLD D9 lease TTL pin; T048a/b, Mars 2026-09-28) ---


def test_contention_metric_filter():
    """T048a: the worker log group carries a metric filter counting
    `concurrency_single_pass` emissions. The envelope event never reaches
    logs (fixed-field vocabulary), so the worker logs the bare term at the
    contender path (Mars ruling 2026-09-28) and the filter counts those
    lines — the pattern must name exactly that term."""
    filt = _resource_block(
        OBSERVABILITY_TF, "aws_cloudwatch_log_metric_filter", "worker_contention"
    )
    assert "log_group_name = aws_cloudwatch_log_group.worker.name" in filt
    pattern = re.search(r'^\s*pattern\s*=\s*"([^"]*)"', filt, re.MULTILINE)
    assert pattern is not None, "filter pattern not found"
    assert "concurrency_single_pass" in pattern.group(1), (
        f"filter pattern does not count the contention term: {pattern.group(1)!r}"
    )
    m = re.search(r"metric_transformation\s*\{([^}]*)\}", filt)
    assert m is not None, "metric_transformation block not found"
    body = m.group(1)
    assert re.search(r'name\s*=\s*"ContentionCount"', body)
    assert re.search(r'namespace\s*=\s*"pr-reviewer/worker"', body)
    assert re.search(r'value\s*=\s*"1"', body)


def test_contention_rate_alarm():
    """T048a: the contention-rate alarm exists, measures the filter's
    metric, wires the shared SNS topic, and stays silent on quiet logs
    (HLD D9: operability signal, not a correctness gate)."""
    alarm = _resource_block(OBSERVABILITY_TF, "aws_cloudwatch_metric_alarm", "contention_rate")
    assert re.search(
        r"metric_name\s*=\s*aws_cloudwatch_log_metric_filter\.worker_contention"
        r"\.metric_transformation\[0\]\.name",
        alarm,
    ), "alarm does not measure the worker_contention filter metric"
    assert re.search(r'namespace\s*=\s*"pr-reviewer/worker"', alarm)
    assert re.search(r'statistic\s*=\s*"Sum"', alarm)
    assert re.search(r'comparison_operator\s*=\s*"GreaterThanThreshold"', alarm)
    assert re.search(r'treat_missing_data\s*=\s*"notBreaching"', alarm)
    assert "aws_sns_topic.alerts.arn" in alarm, "alarm not wired to the shared SNS topic"
    th = re.search(r"^\s*threshold\s*=\s*(.+?)\s*(?:#.*)?$", alarm, re.MULTILINE)
    assert th is not None, "contention_rate threshold not found"
    assert th.group(1).strip() == "var.contention_rate_threshold", (
        f"threshold not the tunable variable: {th.group(1)!r}"
    )


def test_contention_rate_threshold_default():
    """T048a: the threshold default is a numeric literal pinned at 10
    sustained contention hits / 5 min — provisional implementer default
    (Mars approved the scope 2026-09-28; tune from Phase-1 telemetry)."""
    var_block = _resource_block(VARIABLES_TF, "contention_rate_threshold", "", kind="variable")
    default = re.search(r"^\s*default\s*=\s*(\d+)\s*$", var_block, re.MULTILINE)
    assert default is not None, "threshold default is not a numeric literal"
    assert int(default.group(1)) == 10, f"threshold default drifted: {default.group(1)!r}"


def test_contention_term_couples_emission_and_filter():
    """T048b (bot review #1, PR #142): the filter's term and the worker's
    log call share one source of truth by construction — this test reads
    both ends, so a rename on either side fails here instead of silently
    leaving the alarm permanently dormant."""
    worker_src = (TERRAFORM_DIR.parent / "lambda" / "worker_handler.py").read_text(encoding="utf-8")
    assert re.search(
        r'logger\.info\(\s*"concurrency_single_pass mutex=%s elapsed_ms=%s"', worker_src
    ), "contender emission no longer leads with the filter's term + triage fields"
    filt = _resource_block(
        OBSERVABILITY_TF, "aws_cloudwatch_log_metric_filter", "worker_contention"
    )
    pattern = re.search(r'^\s*pattern\s*=\s*"([^"]*)"', filt, re.MULTILINE)
    assert pattern is not None, "filter pattern not found"
    assert "concurrency_single_pass" in pattern.group(1)


# --- T052: viewer role read policy (HLD §7 Viewer IAM) -------------------
#
# Mars ruling 2026-09-28 (DECISIONS, SPR-140 10419): §7's `kms:Decrypt`
# clause is superseded by §2.6 mechanics — SecureStrings ride the AWS-
# managed `aws/ssm` key and SSM decrypts server-side via WithDecryption
# (iam.tf note #6, applied truth since T035). The absence pin below is
# the enforcement: the superseded clause cannot silently return.


def _viewer_policy_block() -> str:
    return _resource_block(VIEWER_TF, "aws_iam_role_policy", "viewer")


def test_viewer_role_policy_s3_getobject_runs_and_static():
    """T052: s3:GetObject scoped to the archives bucket's runs/* and
    static/* prefixes — static shell + archive serving only (HLD §7)."""
    block = _viewer_policy_block()
    assert re.search(r"role\s*=\s*aws_iam_role\.viewer\.id", block), (
        "viewer policy does not attach to the viewer role"
    )
    assert '"s3:GetObject"' in block, "no s3:GetObject statement"
    assert re.search(r"aws_s3_bucket\.archives\.arn\}/runs/\*", block), (
        "s3:GetObject missing the runs/* prefix"
    )
    assert re.search(r"aws_s3_bucket\.archives\.arn\}/static/\*", block), (
        "s3:GetObject missing the static/* prefix"
    )


def test_viewer_role_policy_dynamodb_query_gsi_only():
    """T052: dynamodb:Query scoped to the pr-runs-index GSI ARN — the
    viewer answers latest-run-per-PR queries, never base-table reads
    (HLD §7)."""
    block = _viewer_policy_block()
    assert '"dynamodb:Query"' in block, "no dynamodb:Query statement"
    assert re.search(r"aws_dynamodb_table\.state\.arn\}/index/pr-runs-index", block), (
        "dynamodb:Query not scoped to the pr-runs-index GSI"
    )


def test_viewer_role_policy_ssm_token_and_no_kms_decrypt():
    """T052: ssm:GetParameter on the replay-token ARN (the local map in
    iam.tf pins the parameter path) and NO kms:Decrypt anywhere in the
    viewer surface — SecureStrings need no per-key grant (§2.6
    mechanics; superseded §7 clause must not return)."""
    block = _viewer_policy_block()
    assert '"ssm:GetParameter"' in block, "no ssm:GetParameter statement"
    assert re.search(r"local\.ssm_parameter_arn\.replay_token", block), (
        "ssm:GetParameter not scoped to the replay-token ARN local"
    )
    assert "parameter/pr-reviewer/replay-token" in IAM_TF, (
        "iam.tf locals map does not pin the replay-token parameter path"
    )
    assert "kms:Decrypt" not in block, "superseded kms:Decrypt grant present in policy"
    viewer_code = re.sub(r"(?m)^\s*(?:#|//).*$", "", VIEWER_TF)
    assert "kms:Decrypt" not in viewer_code, "kms:Decrypt granted in viewer.tf code"


def test_no_kms_actions_anywhere_in_terraform():
    """Gate 39 F2 hardening: the superseded decrypt clause cannot return
    via variant spellings or out-of-block grants — case-insensitive
    `kms:` prefix scan over every .tf file's comment-stripped code.
    iam.tf note #6 is the standing truth (NO kms grants stack-wide); a
    future customer-managed-key ruling amends DECISIONS and this pin in
    the same change."""
    for tf in sorted(TERRAFORM_DIR.glob("*.tf")):
        code = re.sub(r"(?m)^\s*(?:#|//).*$", "", tf.read_text(encoding="utf-8"))
        assert re.search(r"kms:", code, re.IGNORECASE) is None, (
            f"kms: action present in {tf.name} — KMS grants require a "
            "Mars ruling amending DECISIONS 2026-09-28 first"
        )


def test_runs_published_metric_filter():
    """T077: the worker log group carries a filter counting published
    review lines — the traffic term of the pipeline-mix drift alarm."""
    filt = _resource_block(OBSERVABILITY_TF, "aws_cloudwatch_log_metric_filter", "runs_published")
    assert "log_group_name = aws_cloudwatch_log_group.worker.name" in filt
    pattern = re.search(r'^\s*pattern\s*=\s*"(.*)"\s*$', filt, re.MULTILINE)
    assert pattern is not None, "filter pattern not found"
    assert '$.status = \\"published\\"' in pattern.group(1), (
        f"filter does not count published lines: {pattern.group(1)!r}"
    )
    m = re.search(r"metric_transformation\s*\{([^}]*)\}", filt)
    assert m is not None, "metric_transformation block not found"
    body = m.group(1)
    assert re.search(r'name\s*=\s*"RunsPublished"', body)
    assert re.search(r'namespace\s*=\s*"pr-reviewer/worker"', body)
    assert re.search(r'value\s*=\s*"1"', body)


def test_runs_multi_agent_metric_filter():
    """T077: the worker log group carries a filter counting lines whose
    pipeline discriminator is multi_agent — the routing term of the
    pipeline-mix drift alarm."""
    filt = _resource_block(OBSERVABILITY_TF, "aws_cloudwatch_log_metric_filter", "runs_multi_agent")
    assert "log_group_name = aws_cloudwatch_log_group.worker.name" in filt
    pattern = re.search(r'^\s*pattern\s*=\s*"(.*)"\s*$', filt, re.MULTILINE)
    assert pattern is not None, "filter pattern not found"
    assert '$.pipeline = \\"multi_agent\\"' in pattern.group(1), (
        f"filter does not count multi_agent lines: {pattern.group(1)!r}"
    )
    m = re.search(r"metric_transformation\s*\{([^}]*)\}", filt)
    assert m is not None, "metric_transformation block not found"
    body = m.group(1)
    assert re.search(r'name\s*=\s*"RunsMultiAgent"', body)
    assert re.search(r'namespace\s*=\s*"pr-reviewer/worker"', body)


def test_pipeline_mix_drift_alarm():
    """T077: the absence-semantics drift alarm — publishing traffic with
    zero multi_agent runs across 3x20min pages via the shared topic.
    FILL(., 0) makes an empty window evaluate as 0 instead of falling to
    notBreaching (which would silence exactly the drift case); legitimate
    single-agent publishes never breach on their own."""
    alarm = _resource_block(OBSERVABILITY_TF, "aws_cloudwatch_metric_alarm", "pipeline_mix_drift")
    assert "FILL(multi, 0) >= 1" in alarm, "multi-absence term missing"
    assert "FILL(published, 0) >= 3" in alarm, "traffic-continued term missing"
    assert re.search(r"evaluation_periods\s*=\s*3", alarm)
    assert re.search(r"period\s*=\s*1200", alarm)
    assert re.search(r'comparison_operator\s*=\s*"LessThanThreshold"', alarm)
    assert re.search(r"threshold\s*=\s*0", alarm)
    assert re.search(r'treat_missing_data\s*=\s*"notBreaching"', alarm)
    assert "aws_sns_topic.alerts.arn" in alarm, "alarm not wired to the shared topic"
    assert re.search(
        r"metric_name\s*=\s*aws_cloudwatch_log_metric_filter\.runs_multi_agent",
        alarm,
    )
    assert re.search(
        r"metric_name\s*=\s*aws_cloudwatch_log_metric_filter\.runs_published",
        alarm,
    )
