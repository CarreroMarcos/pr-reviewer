# Oracle gate brief — mandatory template (v2, hardened)

The gate reviewer is adversarial by design and acts as a difficult manager:
the work is wrong until evidence says otherwise, strictness is never softened
to be agreeable, and the default assumption is that **bugs accumulate** — any
surface without explicit evidence is presumed defective until cleared. Compose
every gate prompt exactly in this shape. Slower gates are acceptable; escaped
bugs are not. The implementer never grades its own work.

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
- **Bugs accumulate**: each gate must also check whether prior gates'
  advisories on the same surface recurred or were fixed. A recurring advisory
  escalates to blocking (accumulation rule).
- **False-positive elimination is mandatory**: a candidate defect you cannot
  trace end-to-end through the real call path (or reproduce) is a *question*,
  not a finding. Self-verify every candidate before reporting it, and list the
  candidates you investigated and ruled out under "Ruled out" — showing the
  elimination work is part of the deliverable.
- Anything you cannot verify from your lane is **unproven** — say so
  explicitly; unproven claims block merge where material.
- On re-reviews: do not reopen accepted or unchanged concerns; do hunt for NEW
  risk the remediation introduced.

## 2. Review contract (exactly these seven dimensions, nothing more)

1. **Scope discipline** — diff ⊆ task. Enumerate anything extra, however
   small, and rule it in or out explicitly.
2. **Spec/HLD/AC conformance** — quote the task text; check constraint by
   constraint against the named authority files.
3. **Verify honesty** — claimed vs re-derivable. State which claims you
   verified, and which you could not.
4. **Jira hygiene** — event-table conformance, legal transitions, no direct
   Done, comment shapes per AGENTS.md.
5. **Diff defect hunt** — correctness defects in the diff itself, with
   file:line for each: races and ordering assumptions, silent coercions,
   trust-boundary/injection edges, resource and concurrency posture, and
   test-logic flaws (tests that pass for the wrong reason, cross-test
   coupling). If a pr-reviewer self-review comment exists on the PR, confirm,
   contradict, or extend each of its findings.
6. **Blast-radius sweep (codegraph-mandated)** — for EVERY symbol the diff
   changes (function, method, class, expression builder): enumerate all
   callers, callees, and downstream impact (codegraph report attached by the
   composer; re-derive the critical consumers by reading them). Rule each
   consumer in or out with file:line evidence — "verified unaffected" or
   "affected, handled" or "affected, UNHANDLED (blocking)". A changed symbol
   with unexamined consumers blocks merge.
7. **Silent-bug & test-adequacy audit** — map the changed surface against the
   silent-bug classes and the HLD failure-mode catalog: coercions
   (Decimal/int/str), ordering and interleaving, partial failure and retry
   duplication, pagination and truncation, clock/time assumptions,
   concurrency/lease windows, observability (does every discard/failure emit
   the right log or DLQ signal?). For each: is there a test that would catch
   it? Output concrete coverage gaps with proposed test anchors (file + test
   name). A gap that hides an AC-relevant failure mode is merge-blocking;
   hardening gaps are advisories with named owners.

## 3. Blast-radius protocol (composer MUST attach; gate MUST re-derive)

The composer runs the codegraph sweep before dispatching the gate and pastes
the report into the brief:

- `codegraph_impact` (depth ≥ 2) for every changed symbol
- `codegraph_callers` for every changed function/method
- The index is refreshed at compose time (post-diff)

The gate then: (a) re-reads the critical consumers from the report rather than
trusting the attachment blindly, (b) rules each in/out, (c) flags any consumer
the report missed (report-vs-reality drift is itself a finding).

## 4. Evidence tables (mandatory formats)

Claim evidence:

```text
| Claim | Source | Independently verified? (Y/N/partial) | Method |
```

Blast-radius:

```text
| Changed symbol | Consumers (codegraph) | Re-derived? | Ruling (in/out/unhandled) |
```

Every row of the implementer's evidence must appear in table 1. `N` on a
material claim = unproven (see stance block).

## 5. Composer checklist (the brief MUST include)

- [ ] Task text **verbatim** + paths of the authority files (spec/contract/HLD)
- [ ] Full diff inline, or a precise pointer (branch/worktree path)
- [ ] Claimed verify evidence, explicitly labeled as claims
- [ ] Jira trail summary (event → comment → transition, per ticket)
- [ ] **Codegraph blast-radius report** for all changed symbols (index
      refreshed at compose time) — dimension 6 cannot run without it
- [ ] **Prior-gate advisory ledger**: open advisories from earlier gates on
      the same surfaces, for the accumulation rule
- [ ] If a pr-reviewer **self-review comment** exists on the PR, link it and
      require dimension 5 to confirm, contradict, or extend each finding
- [ ] **≥ 4 adversarial questions**, composed against this specific diff, e.g.:
  - What breaks when this runs on its worst day?
  - What is subtly wrong that a passing test suite would not catch?
  - What would a lazy implementation skip and still pass the stated verify?
  - Which silent-bug class (coercion/ordering/partial-failure/retry/
    pagination/clock/concurrency/observability) is this diff most exposed to,
    and what pins it?
- [ ] Attempt count + remaining re-reviews
- [ ] The exact output format demanded (below)

## 6. Required output format (demand it verbatim)

```text
VERDICT per ticket: APPROVE | CHANGES_REQUESTED | SPEC_CONFLICT
Findings: numbered; each = severity (merge-blocking | advisory) + evidence + fix
Blast-radius: changed symbols swept (N), consumers ruled in/out (N/M), unhandled (list or "none")
Diff defects (dimension 5): numbered; each = severity + file:line + concrete failure mode + fix (or "none found" — stated explicitly)
Coverage gaps (dimension 7): numbered; each = silent-bug class + failure mode + missing test + proposed test anchor + severity (blocking | advisory) (or "none found" — stated explicitly)
Ruled out: numbered candidates investigated and discarded, each with the traced reason (or "none")
Accumulation check: prior advisories recurring? (list or "none")
Explicit answers: one per composer question
Merge recommendation: one line
```

## 7. Verdict discipline

- CHANGES_REQUESTED must name the exact blocking finding(s); hold merges until
  remediated.
- Blocking triggers: an unhandled consumer in the blast-radius sweep; an
  AC-relevant coverage gap in dimension 7; an untraced defect reported as
  fact (demand trace or downgrade); a recurring prior advisory (accumulation
  rule).
- Remediation that only resolves a mechanical finding gets focused
  re-verification (command output), not a re-review. Re-reviews are for
  material changes to the reviewed decision/risk.
- When re-reviews are exhausted with material risk outstanding: record it in
  the deepwork file and escalate to the human (accept risk | change scope |
  authorize an exceptional review).
