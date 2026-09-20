# pr-reviewer — HCP Terraform OIDC bootstrap (spec-002 T103, M2).
#
# One-time, Mars-applied locally. NEVER imported into the HCP-managed stack
# (avoids circularity: the role must exist before any HCP run). State stays
# local and gitignored.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = "us-west-2"
  prefix     = "pr-reviewer"

  # thumbprint_list IS pinned on the resource below (defense in depth;
  # top intermediate CA, chain verified live 2026-09-19). IAM auto-retrieval
  # remains the primary trust mechanism for Amazon-trusted chains — the
  # stored value is consulted only if the cert ever falls outside AWS's
  # trusted-root library. Current HCP dynamic-credentials docs instruct
  # no thumbprint.
}

resource "aws_iam_openid_connect_provider" "hcp" {
  url            = "https://app.terraform.io"
  client_id_list = ["aws.workload.identity"]
  # Defense in depth (self-review round 2): IAM auto-retrieval remains the
  # primary trust mechanism for Amazon-trusted chains; this stored thumbprint
  # (top intermediate CA, chain verified live 2026-09-19) is the fallback AWS
  # uses only if the cert ever falls outside its trusted-root library.
  thumbprint_list = ["e7b8b5a6743ce1b2f17b041de59558a41472d70c"]
}

# Two roles split by run phase (self-review round 4): plan runs get a
# read-only policy, apply runs the full apply policy — a compromised plan
# run can no longer mutate stack resources. sub pins the exact project AND
# workspace (created by M1/M3, spec-002) and the exact run phase per role.
resource "aws_iam_role" "hcp_plan" {
  name = "pr-reviewer-hcp-plan"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.hcp.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "app.terraform.io:aud" = "aws.workload.identity"
          "app.terraform.io:sub" = "organization:mars-net:project:pr-reviewer:workspace:pr-reviewer:run_phase:plan"
        }
      }
    }]
  })
}

resource "aws_iam_role" "hcp_apply" {
  name = "pr-reviewer-hcp-apply"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.hcp.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "app.terraform.io:aud" = "aws.workload.identity"
          "app.terraform.io:sub" = "organization:mars-net:project:pr-reviewer:workspace:pr-reviewer:run_phase:apply"
        }
      }
    }]
  })
}

# Least-privilege apply policy, derived from every resource in terraform/*.tf
# (spec-002 T103): lambda (2 functions + URL + event source mapping), sqs
# (2 queues + redrive-allow attribute), dynamodb (state table), iam (3 roles
# + inline policies, PassRole on the two function roles only), logs (2 groups
# + 2 metric filters), cloudwatch (7 alarms), sns (alerts topic).
resource "aws_iam_role_policy" "hcp_apply_policy" {
  name = "pr-reviewer-hcp-apply"
  role = aws_iam_role.hcp_apply.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaFunctions"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:DeleteFunction",
          "lambda:GetFunction",
          "lambda:GetFunctionCodeSigningConfig",
          "lambda:GetFunctionConfiguration",
          "lambda:GetPolicy",
          "lambda:ListVersionsByFunction",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
        ]
        Resource = [
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-ingress",
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-worker",
        ]
      },
      {
        Sid    = "LambdaUrlAndMapping"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunctionUrlConfig",
          "lambda:DeleteFunctionUrlConfig",
          "lambda:GetFunctionUrlConfig",
          "lambda:UpdateFunctionUrlConfig",
          "lambda:CreateEventSourceMapping",
          "lambda:DeleteEventSourceMapping",
          "lambda:GetEventSourceMapping",
          "lambda:ListEventSourceMappings",
          "lambda:UpdateEventSourceMapping",
        ]
        # URL config + event-source-mapping ARNs are function-scoped only at
        # the function level; mappings carry their own UUID ARNs, so the
        # function ARNs plus a mapping wildcard under this account/region.
        Resource = [
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-ingress",
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-worker",
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:event-source-mapping:*",
        ]
      },
      {
        Sid    = "PassFunctionRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-ingress",
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-worker",
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" }
        }
      },
      {
        Sid    = "Queues"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
          "sqs:SetQueueAttributes",
          "sqs:TagQueue",
          "sqs:UntagQueue",
        ]
        Resource = [
          "arn:${local.partition}:sqs:${local.region}:${local.account_id}:${local.prefix}-work",
          "arn:${local.partition}:sqs:${local.region}:${local.account_id}:${local.prefix}-dlq",
        ]
      },
      {
        Sid    = "StateTable"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeTable",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:ListTagsOfResource",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:UpdateTable",
          "dynamodb:UpdateTimeToLive",
        ]
        Resource = "arn:${local.partition}:dynamodb:${local.region}:${local.account_id}:table/${local.prefix}-state"
      },
      {
        Sid    = "StackRoles"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListAttachedRolePolicies",
          "iam:ListRolePolicies",
          "iam:PutRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
        ]
        Resource = [
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-ingress",
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-worker",
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-operator",
        ]
      },
      {
        Sid    = "LogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:DeleteRetentionPolicy",
          "logs:ListTagsForResource",
          "logs:PutRetentionPolicy",
          "logs:TagLogGroup",
          "logs:UntagLogGroup",
        ]
        Resource = [
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-ingress",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-ingress:*",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-worker",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-worker:*",
        ]
      },
      {
        # DescribeLogGroups is an account-list action: AWS evaluates it
        # against "log-group::log-stream:", so resource-level scoping never
        # matches (denied at the first HCP plan, 2026-09-20). No IAM
        # condition can filter a list response (aws:ResourceTag evaluates
        # the request, not the returned items), so this accepted trade-off
        # grants read-only group-name disclosure account-wide.
        Sid      = "LogGroupsList"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        Sid    = "MetricFilters"
        Effect = "Allow"
        Action = [
          "logs:DeleteMetricFilter",
          "logs:DescribeMetricFilters",
          "logs:PutMetricFilter",
        ]
        # Metric-filter APIs take the log-group name, not an ARN filterable
        # here; scoped to the two groups via the group-name prefix form AWS
        # accepts for these actions.
        Resource = [
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-ingress:*",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-worker:*",
        ]
      },
      {
        Sid    = "Alarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:DeleteAlarms",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:ListTagsForResource",
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource",
        ]
        Resource = "arn:${local.partition}:cloudwatch:${local.region}:${local.account_id}:alarm:${local.prefix}-*"
      },
      {
        Sid    = "AlertsTopic"
        Effect = "Allow"
        Action = [
          "sns:CreateTopic",
          "sns:DeleteTopic",
          "sns:GetTopicAttributes",
          "sns:ListTagsForResource",
          "sns:SetTopicAttributes",
          "sns:TagResource",
          "sns:UntagResource",
        ]
        Resource = "arn:${local.partition}:sns:${local.region}:${local.account_id}:${local.prefix}-alerts"
      },
      {
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      },
    ]
  })
}

# Read-only policy for PLAN-phase runs (self-review round 4): every read
# path the apply policy needs for refresh/plan, no mutations, no PassRole.
resource "aws_iam_role_policy" "hcp_plan_policy" {
  name = "pr-reviewer-hcp-plan"
  role = aws_iam_role.hcp_plan.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaRead"
        Effect = "Allow"
        Action = [
          "lambda:GetFunction",
          "lambda:GetFunctionCodeSigningConfig",
          "lambda:GetFunctionConfiguration",
          "lambda:GetFunctionUrlConfig",
          "lambda:GetPolicy",
          "lambda:ListEventSourceMappings",
          "lambda:ListVersionsByFunction",
        ]
        Resource = [
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-ingress",
          "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.prefix}-worker",
        ]
      },
      {
        # Mappings carry UUID ARNs (opaque, minted at creation), so neither
        # the refresh path nor IAM can scope by function — mapping-level
        # wildcard is the tightest addressable form (read-only).
        Sid      = "EsMappingRead"
        Effect   = "Allow"
        Action   = ["lambda:GetEventSourceMapping"]
        Resource = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:event-source-mapping:*"
      },
      {
        Sid    = "QueueRead"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
        ]
        Resource = [
          "arn:${local.partition}:sqs:${local.region}:${local.account_id}:${local.prefix}-work",
          "arn:${local.partition}:sqs:${local.region}:${local.account_id}:${local.prefix}-dlq",
        ]
      },
      {
        Sid    = "StateTableRead"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeTable",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:ListTagsOfResource",
        ]
        Resource = "arn:${local.partition}:dynamodb:${local.region}:${local.account_id}:table/${local.prefix}-state"
      },
      {
        Sid    = "StackRolesRead"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListAttachedRolePolicies",
          "iam:ListRolePolicies",
        ]
        Resource = [
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-ingress",
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-worker",
          "arn:${local.partition}:iam::${local.account_id}:role/${local.prefix}-operator",
        ]
      },
      {
        Sid    = "LogsRead"
        Effect = "Allow"
        Action = [
          "logs:DescribeMetricFilters",
          "logs:ListTagsForResource",
        ]
        Resource = [
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-ingress",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-ingress:*",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-worker",
          "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-worker:*",
        ]
      },
      {
        # DescribeLogGroups is an account-list action (see LogGroupsList on
        # the apply policy) — resource-level scoping never matches.
        Sid      = "LogGroupsListRead"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        Sid    = "AlarmsRead"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:ListTagsForResource",
        ]
        Resource = "arn:${local.partition}:cloudwatch:${local.region}:${local.account_id}:alarm:${local.prefix}-*"
      },
      {
        Sid    = "AlertsTopicRead"
        Effect = "Allow"
        Action = [
          "sns:GetTopicAttributes",
          "sns:ListTagsForResource",
        ]
        Resource = "arn:${local.partition}:sns:${local.region}:${local.account_id}:${local.prefix}-alerts"
      },
      {
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      },
    ]
  })
}
