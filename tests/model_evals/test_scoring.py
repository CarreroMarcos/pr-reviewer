"""Q2 scorer unit tests (offline — tiny fake outputs, never the LLM)."""

from scoring import aggregate, parse_findings, score_output

_MANIFEST = {
    "expected_findings": [{"path": "src/app/db.py", "line": 14, "severity": "HIGH"}],
    "changed_paths": ["src/app/db.py"],
}


def _output(*finding_lines: str) -> str:
    return "\n".join(
        [
            "## Summary",
            "Fake change.",
            "",
            "## Findings",
            *finding_lines,
            "",
            "## Risk Notes",
            "None.",
        ]
    )


def test_exact_hit_with_severity_agreement() -> None:
    findings, unparsable = parse_findings(_output("- [HIGH] `src/app/db.py:14` — SQLi. Fix: x."))
    assert unparsable == 0
    metrics = score_output("c", _output("- [HIGH] `src/app/db.py:14` — SQLi. Fix: x."), _MANIFEST)
    assert (metrics.hits, metrics.missed, metrics.fabricated) == (1, 0, 0)
    assert metrics.severity_agreement == "1/1"
    assert (metrics.recall, metrics.precision) == (1.0, 1.0)


def test_line_window_hit_and_severity_mismatch() -> None:
    metrics = score_output("c", _output("- [MEDIUM] `src/app/db.py:16` — SQLi? Fix: x."), _MANIFEST)
    assert (metrics.hits, metrics.missed) == (1, 0)  # |16-14| <= 2
    assert metrics.severity_agreement == "0/1"


def test_miss_and_unlabeled_and_fabricated() -> None:
    output = _output(
        "- [LOW] `src/app/db.py:40` — Nit on an unrelated line. Fix: x.",
        "- [HIGH] `src/other/evil.py:1` — Off-diff finding. Fix: x.",
    )
    metrics = score_output("c", output, _MANIFEST)
    assert (metrics.hits, metrics.missed) == (0, 1)
    assert metrics.unlabeled == 1  # in-diff, not a labeled line
    assert metrics.fabricated == 1  # path outside changed paths
    assert metrics.precision == 0.0


def test_unparsable_junk_counted_not_matched() -> None:
    output = _output(
        "- [HIGH] `src/app/db.py:14` — SQLi. Fix: x.",
        "- just a vibe, no location here",
        "stray prose without a bullet",
    )
    findings, unparsable = parse_findings(output)
    assert len(findings) == 1
    assert unparsable == 2
    metrics = score_output("c", output, _MANIFEST)
    assert metrics.hits == 1 and metrics.unparsable == 2


def test_empty_findings_sentinel_scores_zero_precision() -> None:
    metrics = score_output("c", _output("No significant issues found."), _MANIFEST)
    assert (metrics.hits, metrics.missed, metrics.findings_count) == (0, 1, 0)
    assert metrics.precision == 0.0
    assert aggregate([metrics])["recall"] == 0.0
