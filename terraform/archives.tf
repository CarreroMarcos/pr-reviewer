# serverless-pr-reviewer — S3 archive bucket (T030, HLD §5 S3 Retention + §8 checklist 1).
#
# Bucket pr-reviewer-archives holds per-run archives under runs/
# (events.jsonl + meta.json per HLD §6; keys built by
# lambda/common/archive.py: runs/{pr}/{sha}/{run_id}/{file}).
# Retention: Expiration (Delete) at 90 days on the runs/ prefix — NO
# Glacier transition (128 KB minimum-billable trap on KB-scale objects,
# HLD §5). Local history past expiry lives in the operator sync
# (HLD §5 Local Machine Archive Sync), not in the cloud lifecycle.

resource "aws_s3_bucket" "archives" {
  bucket = "pr-reviewer-archives"
}

resource "aws_s3_bucket_public_access_block" "archives" {
  bucket = aws_s3_bucket.archives.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "archives" {
  bucket = aws_s3_bucket.archives.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyPublicACLs"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "s3:PutObject",
          "s3:PutObjectAcl",
        ]
        Resource = "${aws_s3_bucket.archives.arn}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl" = [
              "public-read",
              "public-read-write",
              "authenticated-read",
            ]
          }
        }
      },
    ]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "archives" {
  bucket = aws_s3_bucket.archives.id

  rule {
    id     = "expire-runs-after-90-days"
    status = "Enabled"

    filter {
      prefix = "runs/"
    }

    expiration {
      days = 90
    }
  }
}
