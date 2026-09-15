# serverless-pr-reviewer — Lambda compute (T020, HLD §2.1–§2.3, §7.1).
#
# Two functions (ingress 5s/512MB/reserved 2; worker 120s/256MB/reserved 5),
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
  memory_size                    = 512
  reserved_concurrent_executions = 2

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

  timeout = 120
  # SPR-60 (Mars decision 2026-09-14): memory 256 MB (HLD §5; trimmed from
  # the 512 MB scratch value, with headroom for the LLM round-trip within
  # the 15 s end-to-end budget). Reserved concurrency 5 (right-sized;
  # see the ingress note — sum 7 of quota-10).
  memory_size                    = 256
  reserved_concurrent_executions = 5

  environment {
    variables = {
      GLM_ALLOWED_HOSTS = "api.z.ai"
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
}
