# serverless-pr-reviewer — input variables (T004).
#
# T004 defines no variables: the provider scaffold needs none, and
# HLD §7.1 (Terraform BOM) names no variable inputs at this stage.
# Later tasks (T020–T024 resources, T055 alarms, T060 cost flow-down)
# declare their variables here.

# --- T055 alarm + budget configuration (HLD §4.3; T060) --------------------
#
# Every cost figure and alarm threshold is a variable: HLD §7.2 and the
# constitution require pricing in alarms/budgets to be configuration
# parameters, never hard-coded. Money values are plain numbers in USD
# (no currency symbols in this file); token prices are per million tokens.

variable "alert_owner" {
  description = "Named owner paged by every T055 alarm (HLD §4.3: each alarm carries threshold, SNS topic, and a named owner)."
  type        = string
  default     = "Marcos Carrero"
}

variable "dlq_depth_threshold" {
  description = "DLQ depth alarm threshold (messages visible on pr-reviewer-dlq). HLD §4.3: DLQ depth > 0 is the primary failure signal."
  type        = number
  default     = 0
}

variable "ingress_unauthorized_count_threshold" {
  description = "Ingress 401-count alarm threshold (unauthorized Function URL responses per 5 minutes; mis-rotation or probing, HLD §4.3)."
  type        = number
  default     = 5
}

variable "ingress_throttle_threshold" {
  description = "Ingress admission-loss alarm threshold (throttled invocations per 5 minutes; the HLD §2.1 429 loss boundary)."
  type        = number
  default     = 0
}

variable "worker_error_rate_percent" {
  description = "Worker error-rate alarm threshold (Lambda Errors as a percent of invocations per 5 minutes, HLD §4.3)."
  type        = number
  default     = 5
}

variable "dynamodb_throttle_threshold" {
  description = "DynamoDB throttling alarm threshold (ThrottledRequests per 5 minutes on pr-reviewer-state, HLD §4.3)."
  type        = number
  default     = 0
}

variable "work_queue_depth_threshold" {
  description = "Work-queue depth alarm threshold (messages visible on pr-reviewer-work; abnormal backlog, HLD §4.3)."
  type        = number
  default     = 10
}

variable "llm_usd_per_million_tokens" {
  description = "Blended LLM price in USD per million tokens (provider-observed input/output rates vary by listing; HLD §2.7 treats all cost figures as order-of-magnitude configuration)."
  type        = number
  default     = 0.2
}

variable "daily_llm_spend_budget_usd" {
  description = "Daily LLM spend budget in USD; the spend alarm fires above it (config-driven budget, HLD §4.3)."
  type        = number
  # Mars, 2026-09-27 (T034): observed 1-call baseline peaked at $0.23/day
  # (Sep 13–27 token metric); ×5 ≈ $1.17, rounded to a $1 budget ⇒ $5/day
  # alarm with headroom for the multi-agent pilot.
  default = 1
}

variable "operator_principal_arn" {
  description = "IAM principal permitted to assume the operator role with MFA (HLD §5.1, failure mode 21). When empty, defaults to the AWS account root principal."
  type        = string
  default     = ""
}

variable "contention_rate_threshold" {
  description = "Mutex-contention (concurrency_single_pass) log lines per 5 min above which the contention-rate alarm fires (HLD D9 operability item)."
  type        = number
  # Provisional implementer default (Mars approved the T048a/b scope
  # 2026-09-28); tune from Phase-1 telemetry.
  default = 10
}
