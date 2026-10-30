# serverless-pr-reviewer — DynamoDB state store (T022, HLD §2.4).
#
# Table pr-reviewer-state, partition key pk, PROVISIONED 12 WCU / 12 RCU
# base + 3/3 GSI (15/15 total = 60% of the AWS Always Free 25/25 envelope,
# staying below the 85% / 21.25-unit monthly Free Tier alert email threshold
# while leaving 10 RCU / 10 WCU account-wide buffer). GSI
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
  read_capacity  = 12
  write_capacity = 12
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
    read_capacity   = 3
    write_capacity  = 3
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
