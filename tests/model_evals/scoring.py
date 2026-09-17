"""Q2 offline scorer (docs/open-questions.md §2). Pure functions only.

Parses a model output's `## Findings` section, matches parsed findings
against manifest ground truth, and computes per-case + aggregate metrics.
No I/O, no network — the drift guard in `test_model_evals.py` proves
`results/baseline.json` is exactly reproducible from pinned outputs plus
manifests via this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LINE_WINDOW = 2
FINDINGS_SENTINEL = "No significant issues found."

_FINDINGS_HEAD_RE = re.compile(r"^##\s+Findings\s*$", re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^##\s+\S.*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_SEVERITY_RE = re.compile(r"\[\s*(HIGH|MEDIUM|LOW)\s*\]", re.IGNORECASE)
_LOCATION_RE = re.compile(r"`([^`\s]+?)\s*:\s*(\d+)`")


@dataclass(frozen=True)
class ParsedFinding:
    """One parsed `path:LINE` finding with its severity token (if any)."""

    path: str
    line: int
    severity: str | None


@dataclass(frozen=True)
class CaseMetrics:
    """Per-case Q2 metrics; JSON-serializable via `to_dict()`."""

    case_id: str
    hits: int
    expected_total: int
    missed: int
    fabricated: int
    unlabeled: int
    unparsable: int
    findings_count: int
    low_count: int
    nit_rate: float
    severity_agreement: str
    comment_length: int
    recall: float
    precision: float

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "hits": self.hits,
            "expected_total": self.expected_total,
            "missed": self.missed,
            "fabricated": self.fabricated,
            "unlabeled": self.unlabeled,
            "unparsable": self.unparsable,
            "findings_count": self.findings_count,
            "low_count": self.low_count,
            "nit_rate": round(self.nit_rate, 4),
            "severity_agreement": self.severity_agreement,
            "comment_length": self.comment_length,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
        }


def normalize_path(path: str) -> str:
    """Normalize a finding path for comparison (strip + leading `./`)."""
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def findings_section(output: str) -> str:
    """Extract the `## Findings` section body (up to the next section)."""
    head = _FINDINGS_HEAD_RE.search(output)
    if head is None:
        return ""
    rest = output[head.end() :]
    following = _NEXT_SECTION_RE.search(rest)
    return rest[: following.start()] if following else rest


def parse_findings(output: str) -> tuple[list[ParsedFinding], int]:
    """Parse the Findings section; return (findings, unparsable count).

    Bullet lines without a parseable backticked `path:LINE` location count
    as `unparsable`, as does any other non-blank prose line that is not the
    empty-findings sentinel (wrapped-bullet continuations included — keep
    finding bullets on one line).
    """
    findings: list[ParsedFinding] = []
    unparsable = 0
    for line in findings_section(output).splitlines():
        if not line.strip() or FINDINGS_SENTINEL in line:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is None:
            unparsable += 1
            continue
        body = bullet.group(1)
        location = _LOCATION_RE.search(body)
        if location is None:
            unparsable += 1
            continue
        severity = _SEVERITY_RE.search(body)
        findings.append(
            ParsedFinding(
                path=normalize_path(location.group(1)),
                line=int(location.group(2)),
                severity=severity.group(1).upper() if severity else None,
            )
        )
    return findings, unparsable


def score_output(case_id: str, output: str, manifest: dict) -> CaseMetrics:
    """Score one pinned output against its manifest ground truth.

    Expected→found match: same normalized path AND |Δline| ≤ LINE_WINDOW.
    Unmatched found findings split into `fabricated` (path outside the
    diff's changed paths) vs `unlabeled` (in-diff but not a labeled line).
    """
    expected = manifest.get("expected_findings", [])
    changed = {normalize_path(path) for path in manifest.get("changed_paths", [])}
    found, unparsable = parse_findings(output)
    used = [False] * len(found)
    hits = 0
    agreed = 0
    for item in expected:
        want_path = normalize_path(item["path"])
        want_line = int(item["line"])
        matched = None
        for index, candidate in enumerate(found):
            if used[index] or candidate.path != want_path:
                continue
            if abs(candidate.line - want_line) <= LINE_WINDOW:
                matched = index
                break
        if matched is None:
            continue
        used[matched] = True
        hits += 1
        if found[matched].severity == str(item.get("severity", "")).upper():
            agreed += 1
    fabricated = 0
    unlabeled = 0
    for index, candidate in enumerate(found):
        if used[index]:
            continue
        if candidate.path in changed:
            unlabeled += 1
        else:
            fabricated += 1
    missed = len(expected) - hits
    low_count = sum(1 for item in found if item.severity == "LOW")
    total = len(found)
    recall = hits / len(expected) if expected else 1.0
    if total:
        precision = hits / total
        nit_rate = low_count / total
    else:
        precision = 1.0 if not expected else 0.0
        nit_rate = 0.0
    return CaseMetrics(
        case_id=case_id,
        hits=hits,
        expected_total=len(expected),
        missed=missed,
        fabricated=fabricated,
        unlabeled=unlabeled,
        unparsable=unparsable,
        findings_count=total,
        low_count=low_count,
        nit_rate=nit_rate,
        severity_agreement=f"{agreed}/{hits}",
        comment_length=len(output),
        recall=recall,
        precision=precision,
    )


def aggregate(metrics: list[CaseMetrics]) -> dict:
    """Aggregate seeded-case metrics (recall/precision over the corpus)."""
    hits = sum(m.hits for m in metrics)
    expected = sum(m.expected_total for m in metrics)
    findings = sum(m.findings_count for m in metrics)
    low = sum(m.low_count for m in metrics)
    lengths = [m.comment_length for m in metrics]
    return {
        "cases": len(metrics),
        "hits": hits,
        "expected": expected,
        "recall": round(hits / expected, 4) if expected else 1.0,
        "precision": round(hits / findings, 4) if findings else 1.0,
        "fabricated": sum(m.fabricated for m in metrics),
        "unlabeled": sum(m.unlabeled for m in metrics),
        "unparsable": sum(m.unparsable for m in metrics),
        "nit_rate": round(low / findings, 4) if findings else 0.0,
        "avg_comment_length": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
    }
