OIDC trust for HCP dynamic creds. Apply once locally by Mars (spec-002 M2), never imported into HCP.
Run here, never from HCP: terraform init && terraform apply (creates pr-reviewer-hcp-run role).
State stays local + gitignored; the main stack must exist before any HCP run.
