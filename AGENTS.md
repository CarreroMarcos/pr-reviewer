# Agent instructions

Personal serverless PR reviewer. Git spec is the contract. Jira is the board. Use Atlassian Rovo MCP for Jira (comment + transition only).

## Commands

```bash
uv sync --frozen --group dev
uv run --frozen pytest -q --tb=short
uv run --frozen ruff check .
uv run --frozen ruff format --check .
pre-commit run --all-files
# only after terraform/ exists:
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
# Real terraform runs (init/plan/destroy) also need live AWS creds. The CLI's
# `aws login` cache (~/.aws/login) is invisible to the terraform AWS provider —
# export resolved session creds into the environment first (verified 2026-09-13):
eval "$(aws configure export-credentials --format env)"
```

Pre-push hook will run the same class of checks. If it fails, fix the code. Do not `--no-verify`.
Never `terraform apply`. Never commit secrets, `.env`, or `terraform.tfstate`.
Never add runtime deps other than boto3. If `pyproject.toml` changes, `uv lock` in the same change.

## Layout (do not invent folders)

HLD owns the tree: `lambda/common/` (shared contract), thin `lambda/ingress_handler.py` + `lambda/worker_handler.py`, `terraform/`, `tests/{unit,state_machine,contracts,model_evals,integration}/`, `prompts/`, `docs/`, `specs/001-pr-reviewer/`. Doc authority: HLD owns architecture (current truth); `docs/DECISIONS.md` owns history/why (append-only); runbooks own procedure; specs own task contracts. Precedence on conflict: HLD wins current behavior, DECISIONS wins why/history; disagreements are Needs-input questions, never silent edits.
Runtime: Python 3.12 stdlib + `boto3` only. No model tools.

## Jira (MCP)

Allowed tools: search, get issue, list transitions, add comment, transition.
Forbidden: create issue, edit summary/description/ACs, delete, Cancelled (human only).
Done is agent-settable only through the Oracle review gate (below) — never directly.
Call mechanics (discover/executeRead wrapping, response shapes, resume after failure): `docs/process/jira-mcp-recipes.md`.

Jira description is a pointer. Source of truth is `specs/001-pr-reviewer/tasks.md` + spec/HLD.

Statuses in this space (names must match; the first column is **To Do**, not Ready):

| Event | Comment? | Transition |
| --- | --- | --- |
| Starting a task | Yes, once | To Do → In Progress |
| PR opened | Yes, with URL | In Progress → In Review |
| Need a decision / AC change | Yes, question only | In Progress → Needs input |
| Human answered, resume | Yes, one line | Needs input → In Progress |
| Oracle gate passed (merged) | Yes, one line: verdict + short sha | In Review → Done |

Comment only at those events. No progress spam, no pasting diffs or secrets, no rewriting the story in a comment.

Every agent-posted Jira comment begins with a Pacific-time stamp — `[YYYY-MM-DD HH:MM PT]` (America/Los_Angeles, DST-aware) — so Mars can see at a glance when the edit/post happened.

Comment shape:

```text
[2026-09-13 23:55 PT] Started T001 on SPR-1/t001-scaffold.
```

```text
PR: https://github.com/CarreroMarcos/pr-reviewer/pull/N
verify: <exact command from tasks.md> → pass
```

```text
Needs input: <one question>. Not changing the spec here.
```

Always `listJiraIssueTransitions` before transitioning. If the name is missing, stop; do not guess.

Next work JQL:

```text
project = SPR AND labels = spec-sync AND status = "To Do" ORDER BY key ASC
```

One ticket in **In Progress** at a time. Start at SPR-1 / T001.

## One ticket, one PR

1. Next To Do story via JQL. Read `tasks.md` for that T-id (not the Jira body).
2. Comment + To Do → In Progress.
3. Branch `SPR-n/t00x-short-slug`. Implement only that task. Run its `verify:`.
4. PR title `SPR-n: T00x …`. Fill the PR template.
5. Comment PR URL + verify result. In Progress → In Review.
6. Stop. The Oracle gate owns In Review (below).

If the AC/HLD is wrong: Needs input, stop. Spec change is a git PR first.

## Oracle review gate (In Review → merge → Done)

After step 6, dispatch an Oracle review with a bounded brief: the single T-id text
from `tasks.md`, the PR diff, real verify evidence + CI status, and the Jira trail.
The gate is a difficult manager: strict, adversarial, and assumes bugs
accumulate until evidence clears every surface. Compose per
`docs/process/gate-brief.md` (v2, mandatory) — the composer attaches a
codegraph blast-radius report (index refreshed at compose time) and the
prior-gate advisory ledger. Oracle reviews exactly seven things — nothing more:

1. Scope discipline — the diff contains only what that T-id requires.
2. Spec/HLD/AC conformance for that task.
3. Verify honesty — claimed evidence matches real output and CI.
4. Jira hygiene — right comment at the right event, legal transitions, no direct Done.
5. Diff defect hunt — correctness bugs in the diff itself (races, silent
   coercions, trust boundaries, concurrency posture, test-logic flaws), each
   with file:line; link any pr-reviewer self-review comment as mandatory input.
6. Blast-radius sweep — codegraph impact/callers for every changed symbol;
   every consumer ruled in/out with file:line evidence; unexamined consumers
   block merge.
7. Silent-bug & test-adequacy audit — failure modes vs tests (coercions,
   ordering, partial failure, retry duplication, pagination, clock,
   concurrency, observability); AC-relevant coverage gaps block merge.

False-positive elimination: an untraced defect is a question, not a finding —
candidates must be traced end-to-end or listed under "Ruled out". Accumulation
rule: a prior gate's advisory recurring on the same surface escalates to
blocking. Even if gates take longer, right-first-time is the goal.

Verdicts and actions:

- **APPROVE** → squash-merge when CI is green; comment `Oracle: APPROVE. Merged <short sha>.`; In Review → Done; start the next ticket via JQL.
- **CHANGES_REQUESTED** → fix on the same branch, push, re-dispatch the gate. Do not merge.
- **Spec/HLD conflict** → Needs input, stop. Human decides; spec change is a git PR first.

Never merge without Oracle APPROVE + green CI. Done is set only through the gate.

## Delivery loop policies

- **P1 — evidence-only closure:** a verify-only ticket needing zero real delta
  closes with an evidence comment (verbatim verify command + result + policy
  name) and **no PR**. Never manufacture a diff to justify a PR.
- **P2 — pair PRs:** a test-first creator task whose `verify:` demands FAIL
  ships in the same PR as its implementation task. Title/comments carry both
  ticket IDs; each ticket keeps its own start comment + In Progress; both get
  the PR comment; both Done at merge.
- **Gate briefs:** compose every Oracle gate from the mandatory template in
  `docs/process/gate-brief.md` (v2: seven dimensions, codegraph blast-radius
  sweep, false-positive elimination, silent-bug & test-adequacy audit,
  accumulation rule) — adversarial stance, evidence tables, per-ticket
  verdicts, attempt N of 3.
- **P3 — deepwork regime:** every loop run (phase or multi-ticket batch)
  follows the deepwork skill's workflow — spec-first, thin vertical slices,
  phase gates, qa ledger — with this file's loop laws layered on top as our
  expansion. Activate the skill at phase start; slow-but-right beats fast-but-leaky.
- **Batch orchestration:** running several tickets at once — phases, parallel
  fixer lanes, batch state file, hang recovery — follows
  `docs/process/batch-loop.md`.
- **PR body edits:** `gh pr edit` fails on this repo (Projects-classic
  GraphQL). Use `gh api repos/CarreroMarcos/pr-reviewer/pulls/N -X PATCH -f body=...`.
- **Self-review recheck (Mars law, 2026-09-19; retry protocol 2026-09-20):**
  after every push to an open PR, wait ~2 minutes (`sleep 120`) and re-fetch
  the pr-reviewer's canonical comment (marker `pr-reviewer:canonical`) — it
  re-reviews on `synchronize` and updates the comment in place. Disposition
  changed findings before continuing; the loop is stable when only accepted
  residuals remain. If the canonical is unchanged or errored at 120s, wait
  another 45s and re-fetch once; still absent/errored → note it and proceed
  (merge still requires green CI, never a bot verdict).
- **tf-* tags are the HCP apply trigger (Mars, 2026-09-19):** agents may
  push `tf-*` tags only with Mars's explicit approval — ask when >=90%
  confident the tagged commit should be applied, and wait for his yes.
  Tag exactly one commit (`git tag tf-<reason> <sha>`, push that tag
  only); never `git push --tags`. The push itself starts the HCP run and
  the apply happens **automatically** — there is no manual UI approval
  step (Mars, 2026-09-26).
- **HCP workspace must define `operator_principal_arn` (Mars, 2026-09-26):**
  set it to the live operator principal
  (`arn:aws:iam::395799817120:user/terraform-admin`). Left empty, terraform
  tries to rewrite `aws_iam_role.operator`'s trust policy (root+MFA
  fallback) and the run dies on a 403 — `pr-reviewer-hcp-apply` has no
  `iam:UpdateAssumeRolePolicy` and we deliberately keep it that way (least
  privilege; parity via the variable, not a broader pipeline grant).
- **Worker-log diagnosis (2026-09-20):** worker logs are lowercase
  structured JSON — a CloudWatch `--filter-pattern ERROR` matches nothing.
  Filter by field: `--filter-pattern '{ $.error_class =
  "assemble_approval_verdict" }'`, or pull the window and grep locally —
  `aws --region us-west-2 logs filter-log-events --log-group-name
  /aws/lambda/pr-reviewer-worker --start-time <epoch-ms>`, then grep
  `error_class` / `status` (`retry_queued` redelivers, `discarded_error`
  is terminal).
- **Clean shell after cred export (2026-09-20):** exported AWS session creds
  (`aws configure export-credentials`) make 8 fake-AWS integration tests
  error — run `capture.py`/terraform and full pytest in separate shells.
  Mechanics: `docs/process/pr-protocol.md`.
- **Self-review failure class (2026-09-20):** `assemble_approval_verdict`
  on our own PRs is a deterministic non-retryable bot self-review failure;
  remedy is exactly one empty-commit retrigger (squash-merge collapses it);
  if it recurs after that one retrigger, note it and proceed — CI gates the
  merge, not the bot.
- **Queue retry purgatory (2026-09-20):** queue visibility timeout 5400s ⇒
  a timed-out delivery redelivers up to ~90 min later and PATCHes
  already-merged PRs harmlessly. An absent canonical at 165s usually means
  in-flight/retry, not an outage.
- **Living document (Mars, 2026-09-20):** short actionable gotchas discovered
  during work graduate into this file — one bullet, dated, attributed.
  Session notes live in the git-ignored deepwork progress file
  (`.slim/deepwork/`); durable rules land here. Write things down when
  needed so you don't forget. Superseded bullets are deleted or condensed
  in the same change that supersedes them.
