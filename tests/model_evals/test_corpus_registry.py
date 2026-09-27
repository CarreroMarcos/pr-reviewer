"""T037/T038: corpus registry + defect accounting (HLD D8 Corpus pin).

The on-disk CORPUS registry grows 15 → 20 (T037: +3 tests-gap, +2
subtle-true) → 24 (T038: +4 verifier traps); ground-truth defects go
13 → 18 with the +5 (one planted defect each) while traps contribute 0.
The scored vocabulary stays frozen: "13 seeded" always means the
pre-growth scored set (HLD D8) — new classes register in CORPUS for the
capture/harness surface but never join SEED_SCORING_IDS until a capture
run pins their outputs (T039/T045).

T038 rows (joint total, trap pool, vetting records, prompt-identity)
are appended below the T037 interim rows.
"""

import fixtures

# Literal transcription of the 12 pre-growth seed specs (independent of
# the implementation list they must equal — a rename breaks loudly here).
_SEED_TWELVE = frozenset(
    {
        "path_traversal",
        "hardcoded_secret",
        "missing_authz",
        "check_then_act",
        "mutable_default",
        "silent_except",
        "off_by_one",
        "resource_leak",
        "command_injection",
        "shared_state_no_lock",
        "xss_safe",
        "ring_modulo",
    }
)

_TESTS_GAP = ("tautological_assertion", "skipped_security_test", "clock_dependent_test")
_SUBTLE_TRUE = ("split_lock_race", "naive_aware_datetime")

_FORBIDDEN_KEYS = frozenset(
    {"verdicts", "mentions", "images", "http_links", "credentials", "canary"}
)


def _manifest(case_id):
    return fixtures.CORPUS[case_id]()[1]


def _defects(*case_ids):
    return sum(len(_manifest(case_id)["expected_findings"]) for case_id in case_ids)


# --- T037 interim: 20 cases / 18 defects --------------------------------------------------------


def test_registry_interim_total_20():
    assert len(fixtures.CORPUS) == 20


def test_seeded_vocabulary_frozen_13():
    """HLD D8 vocabulary pin: "13 seeded" = the scored set, frozen at the
    pre-growth 13 (representative + 12) no matter how CORPUS grows."""
    assert len(fixtures.SEED_SCORING_IDS) == 13
    assert set(fixtures.SEED_SCORING_IDS) == _SEED_TWELVE | {"representative"}


def test_new_classes_registered():
    assert fixtures.TESTS_GAP_IDS == _TESTS_GAP
    assert fixtures.SUBTLE_TRUE_IDS == _SUBTLE_TRUE
    new_five = set(_TESTS_GAP) | set(_SUBTLE_TRUE)
    assert new_five <= set(fixtures.CORPUS)
    assert new_five.isdisjoint(fixtures.SEED_SCORING_IDS)
    assert new_five.isdisjoint(fixtures.ROBUSTNESS_IDS)
    assert set(fixtures.ROBUSTNESS_IDS) == {"injection", "large"}


def test_defect_accounting_interim_18():
    """13 pre-growth defects + exactly one planted defect per new case;
    robustness contributes 0."""
    assert _defects(*fixtures.SEED_SCORING_IDS) == 13
    assert _defects(*_TESTS_GAP, *_SUBTLE_TRUE) == 5
    assert _defects(*fixtures.ROBUSTNESS_IDS) == 0
    assert _defects(*fixtures.CORPUS) == 18


def test_new_manifests_wellformed():
    for case_id in (*_TESTS_GAP, *_SUBTLE_TRUE):
        diff_text, manifest, _meta = fixtures.CORPUS[case_id]()
        assert manifest["id"] == case_id
        assert manifest["category"]
        findings = manifest["expected_findings"]
        assert len(findings) == 1
        finding = findings[0]
        assert {"path", "line", "severity", "hint"} <= set(finding)
        assert manifest["changed_paths"] == [finding["path"]]
        assert set(manifest["forbidden"]) == _FORBIDDEN_KEYS
        assert len(diff_text.splitlines()) <= 60
