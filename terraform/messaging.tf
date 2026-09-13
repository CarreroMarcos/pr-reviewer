# serverless-pr-reviewer — SQS messaging (T021, HLD §2.2, §2.5).
#
# Work queue (visibility 720s, retention 4d, maxReceiveCount 5 -> DLQ) +
# DLQ (14-day retention) + redrive-allow policy on the source queue.
#
# Interpretation (flagged, not silently decided): the AWS redrive-allow
# policy object carries no principal field — it names the DLQ as an allowed
# redrive source (byQueue). Naming the operator role as the permitted
# StartMessageMoveTask actor is IAM, and lives in terraform/iam.tf (T023,
# HLD §5.1 operator role). This file wires the queue side.

resource "aws_sqs_queue" "work" {
  name                       = "pr-reviewer-work"
  visibility_timeout_seconds = 720
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "dlq" {
  name                      = "pr-reviewer-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue_redrive_allow_policy" "work" {
  queue_url = aws_sqs_queue.work.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.dlq.arn]
  })
}
