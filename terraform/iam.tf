# serverless-pr-reviewer — IAM roles (T023, HLD §5.1).
#
# Exactly three roles, each with its inline policy:
#   ingress  — logs; ssm:GetParameter on the webhook-secret ARN only;
#              sqs:SendMessage (work queue); dynamodb:GetItem + PutItem on the
#              state table restricted by dynamodb:LeadingKeys = ["delivery:*"]
#              (ingress can never touch review:* records).
#   worker   — logs; sqs:ReceiveMessage/DeleteMessage/GetQueueAttributes/
#              ChangeMessageVisibility (work queue — the §2.2 Retry-After path);
#              ssm:GetParameters on four explicit ARNs (github-token,
#              glm-api-key, glm-model, glm-endpoint — no wildcard; the webhook
#              secret is ingress-only per §2.6); dynamodb:GetItem/PutItem/
#              UpdateItem/DeleteItem (state table; DeleteItem is the
#              mutex-release op — T067); s3:PutObject on the archives
#              bucket (archive contract writes — T069).
#   operator — sqs:StartMessageMoveTask/ReceiveMessage/DeleteMessage/
#              GetQueueAttributes (DLQ) + sqs:SendMessage (work queue);
#              structurally named (byQueue) in the source queue's
#              redrive-allow policy (messaging.tf; HLD §5.1, failure mode 21).
#
# Interpretations (flagged, not silently decided):
# 1. Trust-policy SourceArns are literal function ARNs built from data
#    sources, not resource references: compute.tf already references these
#    roles, so a resource reference back to the functions would be a
#    Terraform dependency cycle.
# 2. The AWS redrive-allow policy object carries no principal field, so the
#    "naming" of the operator role is structural (byQueue + DLQ ARN in
#    messaging.tf) plus the StartMessageMoveTask grant here.
# 3. HLD §5.1 names an SSO principal for the operator trust; SPR-60
#    (Mars decision 2026-09-14) pins it to a named IAM user via
#    var.operator_principal_arn (falling back to account root with MFA if
#    empty). The pre-existing MFA condition (aws:MultiFactorAuthPresent)
#    composes cleanly with the principal and stays as tightening.
# 4. LeadingKeys is a multivalued condition key, so it requires the
#    ForAllValues modifier: bare "StringLike" never matches and every
#    GetItem/PutItem is denied ("no identity-based policy allows …" —
#    surfaced live by the T035 acceptance run on the scratch stack). The
#    ingress only ever issues single-key GetItem/PutItem on delivery:*,
#    so ForAllValues over that one key is exact; it does not issue
#    Scan/Batch calls, where an empty key set would evaluate true.
# 5. Ingress SSM is the singular ssm:GetParameter per HLD §5.1 (the worker
#    uses the plural batched GetParameters, mirroring config.py). If ingress
#    ever adopts the batched accessor, its grant needs the plural action too
#    (future T030 concern — both handlers are still stubs).
# 6. No kms:Decrypt grants: SecureStrings use the AWS-managed aws/ssm key and
#    SSM decrypts server-side via WithDecryption (HLD §2.6).
# 7. The worker role trusts lambda.amazonaws.com scoped to aws:SourceAccount,
#    not the function ARN: CreateEventSourceMapping validates assumability
#    for the QUEUE event source, so a function-ARN SourceArn condition fails
#    it (seen live applying this stack — T035). SourceAccount keeps the
#    confused-deputy guard; the ingress role keeps its function-ARN condition
#    (no mapping; CreateFunction validated it fine).

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  ssm_parameter_arn = {
    github_token   = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/github-token"
    webhook_secret = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/webhook-secret"
    glm_api_key    = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/glm-api-key"
    glm_model      = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/glm-model"
    glm_endpoint   = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/glm-endpoint"
    replay_token   = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/pr-reviewer/replay-token"
  }

  ingress_function_arn = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:pr-reviewer-ingress"
  worker_function_arn  = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:pr-reviewer-worker"
}

resource "aws_iam_role" "ingress" {
  name = "pr-reviewer-ingress"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceArn" = local.ingress_function_arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ingress" {
  name = "pr-reviewer-ingress"
  role = aws_iam_role.ingress.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.ingress.arn,
          "${aws_cloudwatch_log_group.ingress.arn}:*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.ssm_parameter_arn.webhook_secret]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.work.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
        ]
        Resource = [aws_dynamodb_table.state.arn]
        Condition = {
          # LeadingKeys is a multivalued condition key, so the ForAllValues
          # modifier is mandatory — bare StringLike never matches and every
          # data call is denied (see interpretation 4).
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["delivery:*"] }
        }
      },
    ]
  })
}

resource "aws_iam_role" "worker" {
  name = "pr-reviewer-worker"

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
}

resource "aws_iam_role_policy" "worker" {
  name = "pr-reviewer-worker"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.worker.arn,
          "${aws_cloudwatch_log_group.worker.arn}:*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = [aws_sqs_queue.work.arn]
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameters"]
        Resource = [
          local.ssm_parameter_arn.github_token,
          local.ssm_parameter_arn.glm_api_key,
          local.ssm_parameter_arn.glm_model,
          local.ssm_parameter_arn.glm_endpoint,
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = [aws_dynamodb_table.state.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
        ]
        Resource = ["${aws_s3_bucket.archives.arn}/runs/*"]
      },
    ]
  })
}

resource "aws_iam_role" "operator" {
  name = "pr-reviewer-operator"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = var.operator_principal_arn != "" ? var.operator_principal_arn : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
      }
      Action = "sts:AssumeRole"
      Condition = {
        Bool = { "aws:MultiFactorAuthPresent" = "true" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "operator" {
  name = "pr-reviewer-operator"
  role = aws_iam_role.operator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:StartMessageMoveTask",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [aws_sqs_queue.dlq.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.work.arn]
      },
    ]
  })
}
