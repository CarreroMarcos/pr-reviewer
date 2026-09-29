# Viewer: the internet-reachable replay-site surface (HLD §7 Phase 2;
# Function URL posture per the Oct-2025 hardening). T051b lands the
# Lambda + NONE-auth Function URL + BOTH permission statements; T052
# lands the role's read-only policy (S3 runs/static, DynamoDB index,
# SSM token — no kms:Decrypt, §2.6 mechanics per the Mars ruling
# 2026-09-28). The handler source arrives with T054 — the never-apply
# law keeps this surface validate-only until the T056 deploy tag, so
# the archive_file source below intentionally does not resolve until
# then.

resource "aws_iam_role" "viewer" {
  name = "pr-reviewer-viewer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })

  tags = {
    Owner = var.alert_owner
  }
}

# Viewer read policy (T052, HLD §7 Viewer IAM): the replay surface is
# strictly read-only. No kms:Decrypt — the token is a plain SecureString
# on the AWS-managed aws/ssm key and SSM decrypts server-side via
# WithDecryption (HLD §2.6, iam.tf note #6); §7's earlier decrypt clause
# is superseded (Mars ruling 2026-09-28, DECISIONS), and the contract
# tests pin its absence so it cannot silently return.
resource "aws_iam_role_policy" "viewer" {
  name = "pr-reviewer-viewer-read"
  role = aws_iam_role.viewer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.archives.arn}/runs/*",
          "${aws_s3_bucket.archives.arn}/static/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = ["${aws_dynamodb_table.state.arn}/index/pr-runs-index"]
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        # Singular read per HLD §5.1 discipline (iam.tf note #5): the
        # T054 handler reads GetParameter + WithDecryption only. If it
        # ever adopts batched GetParameters/ByPath, the grant grows in
        # the same change.
        Resource = [local.ssm_parameter_arn.replay_token]
      },
    ]
  })
}

data "archive_file" "viewer" {
  type        = "zip"
  output_path = "${path.module}/viewer.zip"

  source {
    filename = "viewer_handler.py"
    content  = file("${path.module}/../lambda/viewer_handler.py")
  }
}

resource "aws_lambda_function" "viewer" {
  function_name = "pr-reviewer-viewer"
  runtime       = "python3.12"
  handler       = "viewer_handler.handler"
  role          = aws_iam_role.viewer.arn

  filename         = data.archive_file.viewer.output_path
  source_code_hash = data.archive_file.viewer.output_base64sha256

  # Static replay + token-gated API reads: sub-second S3/SSM work, no
  # LLM. 256 MB is the SPR-60 baseline sizing; nothing here needs a
  # full vCPU.
  timeout     = 30
  memory_size = 256

  # Mechanical sequencing guard (bot review #1 on PR #144): the T051b
  # placeholder must be un-deployable, not merely un-deployed — any
  # plan/apply hard-fails while the packaged handler is the stub, so
  # the T056 tag cannot precede T054 by convention alone.
  lifecycle {
    precondition {
      condition     = !strcontains(file("${path.module}/../lambda/viewer_handler.py"), "NotImplementedError")
      error_message = "viewer_handler.py is still the T051b placeholder — land T054 before any plan/apply."
    }
  }

  tags = {
    Owner = var.alert_owner
  }
}

resource "aws_lambda_function_url" "viewer" {
  function_name      = aws_lambda_function.viewer.function_name
  authorization_type = "NONE"
}

# Oct-2025 Function URL hardening (HLD §7): a NONE-auth URL needs BOTH
# resource-policy statements — the URL-invocation grant conditioned on
# the URL's auth type, and the companion plain-invoke grant conditioned
# on InvokedViaFunctionUrl. One without the other either blocks URL
# callers or leaves the URL conditionally unguarded. Provider >= 6.x
# models each condition as a typed argument (function_url_auth_type /
# invoked_via_function_url) that compiles to the StringEquals resource
# policy conditions.

resource "aws_lambda_permission" "viewer_function_url" {
  statement_id           = "AllowFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.viewer.arn
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "viewer_invoked_via_function_url" {
  statement_id             = "AllowInvokedViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.viewer.arn
  principal                = "*"
  invoked_via_function_url = true
}
