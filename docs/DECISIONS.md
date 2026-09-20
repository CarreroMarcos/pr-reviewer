# Decision Log — Autonomous Serverless PR Reviewer

**Status:** Non-normative. This file records **why** the system became what it is: dated rulings, supersedes, gate outcomes, measurements, and rejected alternatives. It never defines current behavior — entries are non-binding on behavior, and the "Current truth" pointer is their only bridge to normative content.

**Authority rule (binds all docs in this repo):** on **current behavior**, `docs/HLD.md` wins; on **why/history**, this file wins; on **redrive procedure**, `docs/runbook-redrive.md` wins; on **task scope**, `specs/**` wins. Any disagreement between documents is a Needs-input question (AGENTS.md), never a silent edit.

**Append-only discipline:** entries are never edited in place — a superseded entry is superseded by a new dated entry that names it. Entries exist only for behavior/config/accepted-risk changes and measurements; typo and wording fixes are silent. Every entry ends with a "Current truth" pointer into the HLD so a reader can jump from history to the normative statement. HLD § numbering is frozen — sections are added, never renamed or renumbered (specs and this log cite §X.Y) — so pointers stay stable; an entry written against an older HLD notes the version current at entry time when the section's content has since moved.

## Entry format

```text
## YYYY-MM-DD — <Title>

**Context:** <what forced the question>
**Decision:** <what was decided, by whom (ruling/gate)>
**Consequences:** <what it costs or bounds>
**Affected HLD:** §X.Y

Current truth: HLD §X.Y.
```

<!-- Entries land here: first the Configuration Baseline delta paragraphs migrate out of docs/HLD.md (its "Configuration Baseline" section), then inline ruling/measurement asides (HLD restructure PR-2/PR-3, approved 2026-09-20). -->
