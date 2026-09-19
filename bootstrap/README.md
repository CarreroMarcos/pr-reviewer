OIDC trust for HCP dynamic creds. Apply once locally by Mars (spec-002 M2), never imported into HCP.
Run here, never from HCP: terraform init && terraform apply (creates pr-reviewer-hcp-plan + pr-reviewer-hcp-apply roles, split by run phase).
State stays local + gitignored; the main stack must exist before any HCP run.
If HCP ever rotates its cert chain (sudden AssumeRoleWithWebIdentity denials): re-derive the top-intermediate thumbprint per the AWS IAM docs procedure (comment in main.tf) and update thumbprint_list.
