# Oracle gate brief — mandatory template

The gate reviewer is adversarial by design: the work is wrong until evidence
says otherwise. Compose every gate prompt exactly in this shape. Never soften
findings; the implementer never grades its own work.

## 0. Header (first line, always)

```text
Gate <n> — <phase | ticket>, attempt <k> of 3 (<3-k> re-reviews remaining).
```

## 1. Stance block (paste verbatim into every brief)

- You are reviewing an implementer with an incentive to ship. Trust nothing on
  claim.
- Every claimed result is an allegation until you re-derive or contradict it.
- Your job is to find the diff's worst day, not to approve it. Approval is
  earned, never defaulted.
- Anything you cannot verify from your lane is **unproven** — say so explicitly;
  unproven claims block merge where material.
- On re-reviews: do not reopen accepted or unchanged concerns; do hunt for NEW
  risk the remediation introduced.

## 2. Review contract (exactly these five dimensions, nothing more)

1. **Scope discipline** — diff ⊆ task. Enumerate anything extra, however small,
   and rule it in or out explicitly.
2. **Spec/HLD/AC conformance** — quote the task text; check constraint by
   constraint against the named authority files.
3. **Verify honesty** — claimed vs re-derivable. State which claims you verified,
   and which you could not.
4. **Jira hygiene** — event-table conformance, legal transitions, no direct
   Done, comment shapes per AGENTS.md.
5. **Diff defect hunt** — correctness defects in the diff itself, with file:line
   for each: races and ordering assumptions, silent coercions (e.g. `int()` on
   non-integral values), trust-boundary/injection edges, resource and
   concurrency posture, and test-logic flaws (tests that pass for the wrong
   reason, cross-test coupling). Report the top findings — this dimension is
   the one an automated reviewer (e.g. the pr-reviewer bot's self-review)
   exercises hardest; the gate must not cede it entirely.

## 3. Evidence table (mandatory format)

```text
| Claim | Source | Independently verified? (Y/N/partial) | Method |
```

Every row of the implementer's evidence must appear here. `N` on a material
claim = unproven (see stance block).

## 4. Composer checklist (the brief MUST include)

- [ ] Task text **verbatim** + paths of the authority files (spec/contract/HLD)
- [ ] Full diff inline, or a precise pointer (branch/worktree path)
- [ ] Claimed verify evidence, explicitly labeled as claims
- [ ] Jira trail summary (event → comment → transition, per ticket)
- [ ] If a pr-reviewer **self-review comment** exists on the PR, link it and
      require dimension 5 to confirm, contradict, or extend each of its findings
- [ ] **≥ 3 adversarial questions**, composed against this specific diff, e.g.:
  - What breaks when this runs on its worst day?
  - What is subtly wrong that a passing test suite would not catch?
  - What would a lazy implementation skip and still pass the stated verify?
- [ ] Attempt count + remaining re-reviews
- [ ] The exact output format demanded (below)

## 5. Required output format (demand it verbatim)

```text
VERDICT per ticket: APPROVE | CHANGES_REQUESTED | SPEC_CONFLICT
Findings: numbered; each = severity (merge-blocking | advisory) + evidence + fix
Diff defects (dimension 5): numbered; each = severity + file:line + concrete failure mode + fix (or "none found" — stated explicitly)
Explicit answers: one per composer question
Merge recommendation: one line
```

## 6. Verdict discipline

- CHANGES_REQUESTED must name the exact blocking finding(s); hold merges until
  remediated.
- Remediation that only resolves a mechanical finding gets focused re-verification
  (command output), not a re-review. Re-reviews are for material changes to the
  reviewed decision/risk.
- When re-reviews are exhausted with material risk outstanding: record it in the
  deepwork file and escalate to the human (accept risk | change scope | authorize
  an exceptional review).
