# Jira MCP call recipes

Verified call shapes for the Atlassian Rovo MCP inside Code Mode (`execute`
with `tools.jira`). Only the orchestrator talks to Jira. Comment/transition
etiquette lives in AGENTS.md — this file is mechanics only, learned the hard
way in the SPR-7…SPR-14 batch (4 failed shapes before a working trail).

## Constants and rules

- cloudId: `aa4f2eb6-ad98-4c84-9204-ac012f892801` — always pass as a
  **top-level** argument (sibling of `name`/`inputs`), never inside `inputs`.
- Transition ids in this space: `21` In Progress, `31` In Review, `41` Done,
  `2` Needs input. Always `listJiraIssueTransitions` before transitioning;
  if a name is missing, stop — do not guess.

## Primary tools (callable directly as `tools.jira.<name>`)

- `addOrEditJiraIssueComment({ cloudId, issueIdOrKey, commentBody })`
- `transitionJiraIssue({ cloudId, issueIdOrKey, transitionId })` — the id is
  a **string**.
- `getJiraIssue({ cloudId, issueIdOrKey, fields })` — `fields` MUST be an
  array, e.g. `["status"]`.
- `searchJiraIssuesUsingJql({ cloudId, jql })`

## Discovered operations need executeRead wrapping

`listJiraIssueTransitions`, `listJiraIssueComments` and friends are **not**
direct tools — calling them as `tools.jira.listJiraIssueTransitions` fails
with "not a function". Wrap them:

```js
await tools.jira.executeRead({
  name: "listJiraIssueTransitions", cloudId,
  inputs: { issueIdOrKey: "SPR-11" },
});
```

Writes/deletes discovered the same way route through `executeWrite` /
`executeDestructive` by risk tier.

## discover signature

```js
tools.jira.discover({ query: "add a comment and transition an issue" })
```

The key is `query` — `goal` is rejected.

## Response shape (verified)

`executeRead` returns a JSON **string** with the payload nested under
`data`:

```json
{ "data": { "total": 2, "comments": [ ... ] } }
```

Parse defensively. Reading `parsed.comments` off the raw string yields
`undefined` (silently empty lists look like "no comments"); calling array
methods on the raw string throws:

```js
const parse = (r) => (typeof r === "string" ? JSON.parse(r) : r);
const d = parse(await tools.jira.executeRead({ ... })).data ?? {};
const list = d.comments ?? [];
```

## Resume after a failed trail block

An `execute` block that throws midway has usually already landed earlier
steps — comments commit before a later `throw`. After any failure, re-read
the issue (status + last comments) and resume **after** the last landed
step. Never blindly re-run the block: duplicate start comments are Jira
hygiene violations.
