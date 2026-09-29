# serverless-pr-reviewer — DynamoDB state store (T022, HLD §2.4).
#
# Table pr-reviewer-state, partition key pk, PROVISIONED 20 WCU / 20 RCU
# base + 5/5 GSI (T032, HLD §7 Mars ruling: rebalance preserves the AWS
# Always Free 25/25 envelope — 20 base + 5 `pr-runs-index`).
# Zero headroom is intentional (HLD-pinned split, §7 Mars ruling): a
# future GSI or bump must revisit the envelope, not drift past it. GSI
# capacity throttles independently of the base table — a throttled index
# write fails the GSI alone, and the archive path treats index writes as
# best-effort non-fatal by design (T029: swallowed with an
# `archive_failed` warning; the base run record is unaffected).
# GSI pr-runs-index (pr_number N + started_ts S, INCLUDE the archive
# row attributes) answers "latest run for PR" for the replay site.
#
# TTL (beyond the T022 task text — disclosed carry-forward): delivery items
# (pk = delivery:{guid}) carry a 7-day expiry written by the merged state
# codec (lambda/common/state.py: build_delivery_item, attribute "ttl").
# Without table-side TTL enabled, those expiry markers never fire and
# delivery rows accumulate unbounded. The `ttl` spec block needs NO
# AttributeDefinition — DynamoDB rejects non-key attribute definitions
# at CreateTable (Gate 3 F1).

resource "aws_dynamodb_table" "state" {
  name           = "pr-reviewer-state"
  billing_mode   = "PROVISIONED"
  read_capacity  = 20
  write_capacity = 20
  hash_key       = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "pr_number"
    type = "N"
  }

  attribute {
    name = "started_ts"
    type = "S"
  }

  global_secondary_index {
    name            = "pr-runs-index"
    hash_key        = "pr_number"
    range_key       = "started_ts"
    read_capacity   = 5
    write_capacity  = 5
    projection_type = "INCLUDE"

    non_key_attributes = [
      "run_id",
      "sha",
      "status",
      "pipeline",
      "archive_s3_key",
      "archive_written_at",
      "findings_n",
    ]
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
