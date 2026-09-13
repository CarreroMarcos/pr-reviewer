# serverless-pr-reviewer — CloudWatch log groups (T024, HLD §4.3).
#
# One log group per Lambda function under the AWS /aws/lambda/<function-name>
# convention, each with 7-day retention (HLD §4.3; cost budget §4.1).

resource "aws_cloudwatch_log_group" "ingress" {
  name              = "/aws/lambda/pr-reviewer-ingress"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/pr-reviewer-worker"
  retention_in_days = 7
}
