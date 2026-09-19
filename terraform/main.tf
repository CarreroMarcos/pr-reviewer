# serverless-pr-reviewer — Terraform provider scaffold (T004).
#
# Scope: provider + version pins + HCP Terraform backend. No resources yet —
# compute, messaging, state, IAM, and observability arrive in T020–T024.
# State is hosted in HCP Terraform (org mars-net, workspace pr-reviewer,
# CLI-driven) per spec-002; the cloud block takes effect at the M3 init,
# which prompts the one-time local-state copy — keep terraform.tfstate
# and its .backup until acceptance passes (spec-002 M3 / AC 3).
# Guardrails live in .gitignore (.terraform/, *.tfstate).

terraform {
  required_version = ">= 1.5.0"

  cloud {
    organization = "mars-net"
    workspaces {
      name = "pr-reviewer"
    }
  }

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
