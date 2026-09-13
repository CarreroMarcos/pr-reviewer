# serverless-pr-reviewer — DynamoDB state store (T022, HLD §2.4).
#
# Table pr-reviewer-state, partition key pk, PROVISIONED 25 WCU / 25 RCU
# (Always Free binding, HLD §2.4; Constitution I).
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
  read_capacity  = 25
  write_capacity = 25
  hash_key       = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
