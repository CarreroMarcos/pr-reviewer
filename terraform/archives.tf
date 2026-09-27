# serverless-pr-reviewer — S3 archive bucket (T030, HLD §5 S3 Retention + §8 checklist 1).
#
# Bucket pr-reviewer-archives holds per-run archives under runs/
# (events.jsonl + meta.json per HLD §6; keys built by
# lambda/common/archive.py: runs/{pr}/{sha}/{run_id}/{file}).
# Retention: Expiration (Delete) at 90 days on the runs/ prefix — NO
# Glacier transition (128 KB minimum-billable trap on KB-scale objects,
# HLD §5). Local history past expiry lives in the operator sync
# (HLD §5 Local Machine Archive Sync), not in the cloud lifecycle.

# DELIBERATE (bot r2:17): no versioning — HLD §5 pins delete-only retention
# for cost (no Glacier for the same reason); the operator sync (HLD §5 Local
# Machine Archive Sync) is the recovery path, not object versions.
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

  # Pin the security ordering: public-access blocking must exist before
  # any policy is attached — the policy references only the bucket, so
  # the dependency graph alone does not order these two (bot r1:20).
  depends_on = [aws_s3_bucket_public_access_block.archives]

  # DELIBERATE (bot r2:31): no blanket Deny on s3:PutBucketPolicy /
  # s3:PutBucketAcl / s3:DeleteBucketPolicy — it would also deny this
  # repository's own terraform applies (no principal ARN is pinned here
  # to except). Public-granting policies are already rejected
  # service-side by block_public_policy = true (BPA above); the
  # account-level control plane (SCP) lives outside this repository.
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
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.archives.arn,
          "${aws_s3_bucket.archives.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

# Explicit at-rest encryption pin (bot r1:17): AES256 is the AWS default for
# new buckets since 2023-01 — this makes the choice visible and stable in
# code rather than relying on the service default.
resource "aws_s3_bucket_server_side_encryption_configuration" "archives" {
  bucket = aws_s3_bucket.archives.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archives" {
  bucket = aws_s3_bucket.archives.id

  # runs/ is the contract-pinned key space (lambda/common/archive.py
  # s3_key()); anything outside it is not run-archive data. Phase-2
  # viewer assets (static/, if any) get their own rule when they exist.
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
