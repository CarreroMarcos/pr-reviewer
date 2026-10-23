# serverless-pr-reviewer — CloudWatch log groups (T024, HLD §4.3).
#
# One log group per Lambda function under the AWS /aws/lambda/<function-name>
# convention, each with 7-day retention (HLD §4.3; cost budget §4.1).

resource "aws_cloudwatch_log_group" "ingress" {
  name              = "/aws/lambda/pr-reviewer-ingress"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/pr-reviewer-worker"
  retention_in_days = 7
}

# T056: viewer replay site log group — same 7-day retention discipline
# (HLD §4.3; cost budget §4.1).
resource "aws_cloudwatch_log_group" "viewer" {
  name              = "/aws/lambda/pr-reviewer-viewer"
  retention_in_days = 7
}

# --- Alerts SNS topic & metric filters ---------------------------------------
#
# Metric alarms were decommissioned to stay within the AWS Free Tier 10-alarm
# limit (Global-CW:AlarmMonitorUsage). The SNS topic and log metric filters
# remain active for log-derived metrics and future alerting needs.


resource "aws_sns_topic" "alerts" {
  name = "pr-reviewer-alerts"

  tags = {
    Owner = var.alert_owner
  }
}

# --- Log metric filters (custom signals for the 401 + spend alarms) ---------

resource "aws_cloudwatch_log_metric_filter" "ingress_unauthorized" {
  name           = "pr-reviewer-ingress-unauthorized"
  log_group_name = aws_cloudwatch_log_group.ingress.name
  pattern        = "{ $.statusCode = 401 }"

  metric_transformation {
    name      = "UnauthorizedCount"
    namespace = "pr-reviewer/ingress"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "worker_token_usage" {
  name           = "pr-reviewer-worker-token-usage"
  log_group_name = aws_cloudwatch_log_group.worker.name
  # Every structured worker line carries token_usage >= 0 (logs.py guard);
  # Lambda platform lines (START/REPORT/END) carry no such field and are
  # excluded by the pattern.
  pattern = "{ $.token_usage > -1 }"

  metric_transformation {
    name      = "TokenUsageSum"
    namespace = "pr-reviewer/llm"
    value     = "$.token_usage"
  }
}

resource "aws_cloudwatch_log_metric_filter" "worker_contention" {
  name           = "pr-reviewer-worker-contention"
  log_group_name = aws_cloudwatch_log_group.worker.name
  # The contender logs the bare term at the contention path (T048b, Mars
  # 2026-09-28) — the envelope event itself never reaches logs (fixed-field
  # vocabulary). A bare-term pattern keeps the signal independent of the
  # log formatter's field shapes.
  pattern = "concurrency_single_pass"

  metric_transformation {
    name      = "ContentionCount"
    namespace = "pr-reviewer/worker"
    value     = "1"
  }
}

# --- Pipeline-mix signals (T077, Gate-56 advisory A2 carrier) ---------------
# The drift alarm is an ABSENCE alarm, not a per-run difference. Its traffic
# term counts NON-docs publishes only (Gate-58 advisory to SPR-167):
# deliberate T076 docs-only routes are subtracted via runs_docs_only, so
# prose-heavy repos never false-page. Contender races and degraded
# fallbacks remain traffic — sustained contention without any multi_agent
# run is itself worth a look. It breaches only when non-docs publishing
# continues (>= 3) while the multi_agent count stays under 1 across 3
# consecutive 20-min periods.

resource "aws_cloudwatch_log_metric_filter" "runs_published" {
  name           = "pr-reviewer-worker-runs-published"
  log_group_name = aws_cloudwatch_log_group.worker.name
  # Only the happy-path publish status counts as traffic; degraded and
  # discarded lines carry their own dispositions (and their own signals).
  pattern = "{ $.status = \"published\" }"

  metric_transformation {
    name      = "RunsPublished"
    namespace = "pr-reviewer/worker"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "runs_multi_agent" {
  name           = "pr-reviewer-worker-runs-multi-agent"
  log_group_name = aws_cloudwatch_log_group.worker.name
  # The publish log line carries pipeline (resolve_pipeline of the run's
  # own events, T077); a window with no matching lines yields no datapoints
  # — the alarm's FILL rewrites that absence to 0.
  pattern = "{ $.pipeline = \"multi_agent\" }"

  metric_transformation {
    name      = "RunsMultiAgent"
    namespace = "pr-reviewer/worker"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "runs_docs_only" {
  name           = "pr-reviewer-worker-runs-docs-only"
  log_group_name = aws_cloudwatch_log_group.worker.name
  # The docs-skip path logs the bare term (T048b precedent: the envelope
  # event never reaches logs); counts deliberate T076 docs routes so the
  # drift alarm's traffic term can exclude them (Gate-58 advisory to
  # SPR-167 — prose-heavy repos must not false-page).
  pattern = "docs_only"

  metric_transformation {
    name      = "RunsDocsOnly"
    namespace = "pr-reviewer/worker"
    value     = "1"
  }
}

# --- CloudWatch Metric Alarms decommissioned (AWS Free Tier limit) ------------
# All 10 CloudWatch metric alarms (pipeline_mix_drift, dlq_depth, ingress_401,
# ingress_admission_429, worker_error_rate, dynamodb_throttling, work_queue_depth,
# daily_llm_spend, worker_invocation_spike, contention_rate) have been removed
# to prevent exceeding the AWS Free Tier 10-alarm limit (Global-CW:AlarmMonitorUsage).
# Metric filters and log groups remain active.
