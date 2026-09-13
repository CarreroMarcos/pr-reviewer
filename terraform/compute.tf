# serverless-pr-reviewer — Lambda compute (T020, HLD §2.1–§2.3, §7.1).
#
# Two functions (ingress 5s/128MB/reserved 25; worker 120s/256MB/reserved 5),
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
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/worker.zip"

  excludes = [
    "ingress_handler.py",
    "**/__pycache__",
    "*.pyc",
  ]
}

resource "aws_lambda_function" "ingress" {
  function_name = "pr-reviewer-ingress"
  runtime       = "python3.12"
  handler       = "ingress_handler.handler"
  role          = aws_iam_role.ingress.arn

  filename         = data.archive_file.ingress.output_path
  source_code_hash = data.archive_file.ingress.output_base64sha256

  timeout                        = 5
  memory_size                    = 128
  reserved_concurrent_executions = 25
}

resource "aws_lambda_function" "worker" {
  function_name = "pr-reviewer-worker"
  runtime       = "python3.12"
  handler       = "worker_handler.handler"
  role          = aws_iam_role.worker.arn

  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256

  timeout                        = 120
  memory_size                    = 256
  reserved_concurrent_executions = 5
}

resource "aws_lambda_function_url" "ingress" {
  function_name      = aws_lambda_function.ingress.function_name
  authorization_type = "NONE"

  # No cors block: CORS is disabled by omission (HLD §5.2). The only
  # legitimate caller is GitHub's non-browser webhook dispatcher.
}

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
}
