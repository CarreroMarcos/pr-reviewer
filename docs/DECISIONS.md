# Decision Log — Autonomous Serverless PR Reviewer

**Status:** Non-normative. This file records **why** the system became what it is: dated rulings, supersedes, gate outcomes, measurements, and rejected alternatives. It never defines current behavior.

**Authority rule (binds all docs in this repo):** on **current behavior**, `docs/HLD.md` wins; on **why/history**, this file wins; on **redrive procedure**, `docs/runbook-redrive.md` wins; on **task scope**, `specs/**` wins. Any disagreement between documents is a Needs-input question (AGENTS.md), never a silent edit.

**Append-only discipline:** entries are never edited in place — a superseded entry is superseded by a new dated entry that names it. Entries exist only for behavior/config/accepted-risk changes and measurements; typo and wording fixes are silent. Every entry ends with a "Current truth" pointer into the HLD so a reader can jump from history to the normative statement.

## Entry format

```text
## YYYY-MM-DD — <Title>

**Context:** <what forced the question>
**Decision:** <what was decided, by whom (ruling/gate)>
**Consequences:** <what it costs or bounds>
**Affected HLD:** §X.Y

Current truth: HLD §X.Y.
```

<!-- Entries land here starting with the §7.2 delta migration. -->
