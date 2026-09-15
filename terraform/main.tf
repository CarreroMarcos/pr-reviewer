# serverless-pr-reviewer — Terraform provider scaffold (T004).
#
# Scope: provider + version pins only. No resources yet — compute,
# messaging, state, IAM, and observability arrive in T020–T024.
# Local state: no backend block (single-operator local backend per
# HLD §7.1). Guardrails live in .gitignore (.terraform/, *.tfstate).

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
