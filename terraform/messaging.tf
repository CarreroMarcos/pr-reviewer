# serverless-pr-reviewer — SQS messaging (T021, HLD §2.2, §2.5).
#
# Work queue (visibility 1800s, retention 4d, maxReceiveCount 3 -> DLQ) +
# DLQ (14-day retention) + redrive-allow policy on the source queue.
#
# Safety proof (visibility 1800 s >= worker timeout 900 s,
# terraform/compute.tf): an invocation can never outlive the invisibility
# window, so no concurrent duplicate processing is possible; a failed
# message redelivers at ~30 min (previously 5400 s ≈ 90 min). The 1800/900
# and maxReceiveCount values are pinned to the worker mirror
# (`worker_handler._MAX_RECEIVE_COUNT`) by
# tests/contracts/test_terraform_contract.py — change both together.
#
# Ordering (pinned 2026-09-26): this is a STANDARD (non-FIFO) queue and the
# SQS event source mapping delivers batch_size = 1 (terraform/compute.tf)
# — one message per worker invocation, so there is no intra-batch
# reordering — but standard queues give NO cross-message ordering
# guarantee: an older review can be delivered after a newer one (retry
# gap, concurrent polls). Correctness never depends on arrival order:
# establish/generation + the live-head fence + last_seen_sha dedupe
# discard or supersede out-of-order reviews by content, not by arrival
# sequence (HLD §3.3; exercised by tests/state_machine/test_interleaving.py
# and tests/state_machine/test_reconcile.py). batch_size = 1 is pinned by
# tests/contracts/test_terraform_contract.py — change both together.
#
# Interpretation (flagged, not silently decided): the AWS redrive-allow
# policy object carries no principal field — it names the DLQ as an allowed
# redrive source (byQueue). Naming the operator role as the permitted
# StartMessageMoveTask actor is IAM, and lives in terraform/iam.tf (T023,
# HLD §5.1 operator role). This file wires the queue side.

resource "aws_sqs_queue" "work" {
  name                       = "pr-reviewer-work"
  visibility_timeout_seconds = 1800   # = 2 x worker timeout 900 s (invocation cannot outlive the window); was 6 x 900 = 5400 (~90-min retry gaps).
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
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
