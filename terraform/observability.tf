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

# --- T055 alarms (HLD §4.3; SC-006) ------------------------------------------
#
# Seven alarms, each wired to the shared SNS topic and each carrying the
# named owner as an `Owner` tag plus an alarm description naming that owner
# (HLD §4.3: "each with threshold, SNS topic, and a named owner").
# All thresholds and money figures are variables (variables.tf; HLD §7.2,
# T060) — no cost literal lives in this file.
#
# Signal anchoring (no invented metric names):
# - DLQ depth / work-queue depth: SQS ApproximateNumberOfMessagesVisible on
#   the real queue names from messaging.tf (pr-reviewer-dlq / pr-reviewer-work).
# - 429 admission count: Lambda Throttles on pr-reviewer-ingress. HLD §2.1
#   admission semantics say excess delivery attempts past the concurrency
#   ceiling return 429, and a concurrency throttle is exactly that event as
#   seen from inside AWS — the closest real signal to the loss boundary.
# - Worker error rate: Lambda Errors / Invocations metric math on
#   pr-reviewer-worker (percent per 5 minutes).
# - DynamoDB throttling: ThrottledRequests on pr-reviewer-state (state.tf).
# - Ingress 401 count: log metric filter on the ingress log group matching
#   `statusCode = 401` — the exact shape ingress_handler.py emits on every
#   disposition via `common.logs` (`statusCode` + `decision` + `reason`;
#   one structured line per request, so 401 outcomes match this filter).
#   The alarm is live; missing data (no deliveries) stays notBreaching.
# - Daily LLM spend: log metric filter summing the worker's structured
#   `token_usage` field (emitted on every terminal path via worker_handler
#   `_emit` to stdout, HLD §5.4 fixed field set), converted to spend by
#   metric math against var.llm_usd_per_million_tokens and compared to
#   var.daily_llm_spend_budget_usd. Missing data (a day with no reviews)
#   is notBreaching, never an alarm.

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

# 1. DLQ depth > 0 — the primary failure signal (HLD §4.3).

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "pr-reviewer-dlq-depth"
  alarm_description   = "DLQ pr-reviewer-dlq holds unprocessed work; redrive per docs/runbook-redrive.md. Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = var.dlq_depth_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 2. Ingress 401-rate spike — mis-rotation or probing (HLD §4.3).

resource "aws_cloudwatch_metric_alarm" "ingress_unauthorized_spike" {
  alarm_name          = "pr-reviewer-ingress-401-spike"
  alarm_description   = "Spike in ingress 401 responses (webhook-secret mis-rotation or probing); rotate per HLD §2.6 if legitimate traffic fails. Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.ingress_unauthorized.metric_transformation[0].name
  namespace           = "pr-reviewer/ingress"
  period              = 300
  statistic           = "Sum"
  threshold           = var.ingress_unauthorized_count_threshold
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 3. 429 admission count — the HLD §2.1 loss boundary.

resource "aws_cloudwatch_metric_alarm" "ingress_admission_throttles" {
  alarm_name          = "pr-reviewer-ingress-429-admission"
  alarm_description   = "Ingress invocations throttled past the admission ceiling (GitHub sees 429; recovery is admin redelivery within 3 days, HLD §2.1). Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = var.ingress_throttle_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.ingress.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 4. Worker error rate (HLD §4.3).

resource "aws_cloudwatch_metric_alarm" "worker_error_rate" {
  alarm_name          = "pr-reviewer-worker-error-rate"
  alarm_description   = "Worker Lambda error rate above budget; check worker logs then the DLQ alarm. Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = var.worker_error_rate_percent
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "error_rate"
    expression  = "100 * errors / invocations"
    label       = "Worker error percent"
    return_data = true
  }

  metric_query {
    id          = "errors"
    return_data = false

    metric {
      metric_name = "Errors"
      namespace   = "AWS/Lambda"
      period      = 300
      stat        = "Sum"

      dimensions = {
        FunctionName = aws_lambda_function.worker.function_name
      }
    }
  }

  metric_query {
    id          = "invocations"
    return_data = false

    metric {
      metric_name = "Invocations"
      namespace   = "AWS/Lambda"
      period      = 300
      stat        = "Sum"

      dimensions = {
        FunctionName = aws_lambda_function.worker.function_name
      }
    }
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 5. DynamoDB throttling on pr-reviewer-state (HLD §4.3; capacity §2.4).

resource "aws_cloudwatch_metric_alarm" "dynamodb_throttling" {
  alarm_name          = "pr-reviewer-dynamodb-throttling"
  alarm_description   = "Throttled requests on pr-reviewer-state; burst budget is 20 WCU/s at worker concurrency 5 (HLD §2.4). Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ThrottledRequests"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = var.dynamodb_throttle_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    TableName = aws_dynamodb_table.state.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 6. Work-queue depth abnormal (HLD §4.3; PR-flood surfacing per §5.2).

resource "aws_cloudwatch_metric_alarm" "work_queue_depth" {
  alarm_name          = "pr-reviewer-work-queue-depth"
  alarm_description   = "Abnormal backlog on pr-reviewer-work (sustained flood or stuck worker); spend is bounded by worker concurrency. Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = var.work_queue_depth_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.work.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}

# 7. Daily LLM spend vs the config-driven budget (HLD §4.3; HLD §7.2 pricing
# is configuration, never hard-coded — both operands are variables).

resource "aws_cloudwatch_metric_alarm" "daily_llm_spend" {
  alarm_name          = "pr-reviewer-daily-llm-spend"
  alarm_description   = "Daily LLM spend above the configured budget; kill switch is worker reserved concurrency to 0 (HLD §4.3). Owner: ${var.alert_owner}."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = var.daily_llm_spend_budget_usd
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "spend_usd"
    expression  = "tokens * ${var.llm_usd_per_million_tokens} / 1000000"
    label       = "Daily LLM spend USD"
    return_data = true
  }

  metric_query {
    id          = "tokens"
    return_data = false

    metric {
      metric_name = aws_cloudwatch_log_metric_filter.worker_token_usage.metric_transformation[0].name
      namespace   = "pr-reviewer/llm"
      period      = 86400
      stat        = "Sum"
    }
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Owner = var.alert_owner
  }
}
