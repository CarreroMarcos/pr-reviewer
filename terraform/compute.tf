# serverless-pr-reviewer — Lambda compute (T020, HLD §2.1–§2.3, §7.1).
#
# Two functions (ingress 5s/512MB; worker 900s/1769MB), both unreserved
# (reserved concurrency dropped at the 2026-09-15 ruling — see the ingress
# note below; restore 2/5 only after a quota raise),
# a public Function URL on ingress (AuthType NONE, CORS disabled by omission),
# and the SQS event source mapping (batch_size = 1).
#
# Packaging: archive_file zips embed lambda/common/ (the single shared
# contract, HLD §7.1) alongside each thin handler. Role ARNs resolve to
# terraform/iam.tf (T023); the queue ARN to terraform/messaging.tf (T021) —
# full `terraform validate` waits for T024 when all BOM files exist.

data "archive_file" "ingress" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/ingress.zip"

  excludes = [
    "worker_handler.py",
    "**/__pycache__",
    "*.pyc",
  ]
}

data "archive_file" "worker" {
  type        = "zip"
  output_path = "${path.module}/worker.zip"

  # prompts/ ships in the worker zip at zip-root prompts/ so
  # _load_system_prompt resolves the versioned contract file (T035).
  source {
    content  = file("${path.module}/../prompts/system_prompt.md")
    filename = "prompts/system_prompt.md"
  }

  # lambda/ modules enumerated at plan time (auto-includes new modules;
  # the ingress handler stays out of the worker artifact).
  # Invariant: the runtime layout law is Python stdlib+boto3 only, so
  # non-.py files are excluded from the zip by design ("**/*.py"); adding
  # a new file type requires revisiting this pattern.
  dynamic "source" {
    for_each = {
      for f in fileset("${path.module}/../lambda", "**/*.py") : f => f
      if f != "ingress_handler.py"
    }
    content {
      content  = file("${path.module}/../lambda/${source.value}")
      filename = source.value
    }
  }
}

resource "aws_lambda_function" "ingress" {
  function_name = "pr-reviewer-ingress"
  runtime       = "python3.12"
  handler       = "ingress_handler.handler"
  role          = aws_iam_role.ingress.arn

  filename         = data.archive_file.ingress.output_path
  source_code_hash = data.archive_file.ingress.output_base64sha256

  timeout = 5
  # SPR-60 (Mars decision 2026-09-14): right-sized reservations — ingress 2,
  # worker 5 (sum 7 of the account quota-10, leaving 3 floating).
  # Storm-containment posture only (guaranteed webhook availability +
  # worker blast-radius cap), not a throughput need. Supersedes the HLD §5
  # value (25).
  # Memory stays 512 MB (known-good): CPU scales with memory and the lazy
  # boto3 import cannot fit a cold start into the 5 s budget at 128 MB
  # (observed: Sandbox.Timedout on first invoke). 128 MB was re-attempted
  # under SPR-60 and rejected by operator decision (2026-09-14) after the
  # canonical review surfaced this prior failure — revert over accept-risk.
  # The memory trim is worker-only.
  #
  # Reserved concurrency DROPPED (operator ruling 2026-09-15, live-apply
  # evidence): the account's total Lambda concurrency is 10 and AWS
  # enforces >=10 unreserved account-wide, so no reservation is landable
  # until a quota raise. Restore the ingress/worker reserves (2/5) only
  # after that raise (SPR-60 "at quota raise" intent).
  memory_size = 512

  # WORK_QUEUE_URL has no safe default in the handler ("" → send fails, 500):
  # the queue URL is account-specific, so it must be wired (surfaced by the
  # T035 acceptance harness — first live delivery 500ed with Environment null).
  # STATE_TABLE_NAME / WEBHOOK_SECRET_NAME keep their handler defaults, which
  # match this stack's names exactly.
  environment {
    variables = {
      WORK_QUEUE_URL = aws_sqs_queue.work.url
    }
  }
}

resource "aws_lambda_function" "worker" {
  function_name = "pr-reviewer-worker"
  runtime       = "python3.12"
  handler       = "worker_handler.handler"
  role          = aws_iam_role.worker.arn

  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256

  timeout = 900
  # SPR-60's 256 MB trim was superseded 2026-09-19 (spec-002, Mars ruling):
  # 900 s / 1769 MB (1 full vCPU); sizing rationale and free-tier math in
  # specs/002-worker-sizing-hcp/spec.md. Reserved concurrency: see the
  # ingress note — dropped at the 2026-09-15 ruling, restore 5 at quota raise.
  memory_size = 1769

  environment {
    variables = {
      GLM_ALLOWED_HOSTS = "api.z.ai"
      # T033: the 12 HLD §8 checklist-7 multi-agent knobs with the T005
      # defaults (cross-checked against common.config in
      # tests/contracts/test_terraform_multi_agent.py). REASONING_EFFORT
      # stays "low" PROVISIONAL — Phase-0 data picks the ship config.
      MULTI_AGENT            = "0"
      MULTI_AGENT_PHASE0     = "1"
      FANOUT_CONCURRENCY     = "3"
      MUTEX_LEASE_TTL_S      = "900"
      REASONING_MAX_CHARS    = "4000"
      REASONING_EFFORT       = "low"
      WAVE_WAIT_FOR_S        = "300"
      VERIFIER_WAIT_FOR_S    = "240"
      SYNTHESIZER_WAIT_FOR_S = "180"
      SINGLE_PASS_WAIT_FOR_S = "240"
      SOCKET_READ_TIMEOUT_S  = "240"
      BUDGET_MARGIN_S        = "60"
    }
  }
}

resource "aws_lambda_function_url" "ingress" {
  function_name      = aws_lambda_function.ingress.function_name
  authorization_type = "NONE"

  # No cors block: CORS is disabled by omission (HLD §5.2). The only
  # legitimate caller is GitHub's non-browser webhook dispatcher.
}

# NOTE (T035, first-deploy finding): since Lambda's Oct-2025 hardening, NONE-auth
# function URLs need TWO resource-policy statements: lambda:InvokeFunctionUrl
# (auto-added by aws_lambda_function_url with auth NONE) AND lambda:InvokeFunction
# gated on lambda:InvokedViaFunctionUrl=true. aws_lambda_permission cannot express
# the second statement until provider v6 (invoked_via_function_url arg,
# hashicorp/terraform-provider-aws#44829). Applied out-of-band for the scratch
# stack, pinned here for the provider-bump ticket:
#   aws lambda add-permission --function-name pr-reviewer-ingress \
#     --statement-id AllowPublicFunctionUrlInvoke --action lambda:InvokeFunction \
#     --principal "*" --invoked-via-function-url --region us-west-2

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1

  # T032 (HLD §8 checklist 3): cap concurrent worker executions at 2 —
  # the D9 application-level mutex serializes fan-out, this bounds the
  # account-wide concurrency beside it. Worker stays unreserved.
  scaling_config {
    maximum_concurrency = 2
  }
}
