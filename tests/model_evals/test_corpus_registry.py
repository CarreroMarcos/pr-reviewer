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


def test_nontrap_cohort_still_20():
    """T037 interim total, evolved for the joint corpus: the 20 non-trap
    cases persist unchanged (13 scored + 5 growth + 2 robustness) holding
    all 18 defects."""
    nontrap = [cid for cid in fixtures.CORPUS if cid not in fixtures.TRAP_IDS]
    assert len(nontrap) == 20
    assert _defects(*nontrap) == 18


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


def test_trap_manifests_wellformed():
    """Bot R2: the trap manifest schema is pinned, not just its empty
    findings — exact key set matching `_trap_builder`, single-path
    changed_paths, the shared forbidden classes, the `trap` marker True,
    and the ≤60-line budget."""
    for case_id in fixtures.TRAP_IDS:
        diff_text, manifest, _meta = fixtures.CORPUS[case_id]()
        assert set(manifest) == {
            "id",
            "category",
            "expected_findings",
            "changed_paths",
            "forbidden",
            "trap",
        }
        assert manifest["id"] == case_id
        assert manifest["category"]
        assert manifest["expected_findings"] == []
        assert manifest["changed_paths"] != []
        assert set(manifest["forbidden"]) == _FORBIDDEN_KEYS
        assert manifest["trap"] is True
        assert len(diff_text.splitlines()) <= 60


# --- T038 joint total: 24 cases / 18 defects; traps add 0 ----------------------------------


def test_joint_total_24_18():
    assert len(fixtures.CORPUS) == 24
    total = sum(
        len(fixtures.CORPUS[case_id]()[1]["expected_findings"]) for case_id in fixtures.CORPUS
    )
    assert total == 18
    assert (
        sum(
            len(fixtures.CORPUS[case_id]()[1]["expected_findings"]) for case_id in fixtures.TRAP_IDS
        )
        == 0
    )
    assert set(fixtures.TRAP_IDS) <= set(fixtures.CORPUS)
    assert len(fixtures.TRAP_IDS) == 4


def test_trap_pool_6_4_2():
    """6 considered, 4 frozen (in CORPUS), 2 rejected with documented
    reasons and no builders."""
    pool = fixtures.TRAP_POOL
    assert len(pool) == 6
    frozen = [entry for entry in pool if entry["status"] == "frozen"]
    rejected = [entry for entry in pool if entry["status"] == "rejected"]
    assert [entry["id"] for entry in frozen] == list(fixtures.TRAP_IDS)
    assert len(rejected) == 2
    for entry in rejected:
        assert entry["reason"].strip()
        assert entry["id"] not in fixtures.CORPUS
    for case_id in fixtures.TRAP_IDS:
        _, manifest, _ = fixtures.CORPUS[case_id]()
        assert manifest["expected_findings"] == []
        assert manifest["trap"] is True


def test_vetting_modes():
    """Every frozen trap carries exactly one vetting arm: a non-blank
    generator-avoidance log XOR a non-blank verifier kill_reason, plus
    plausibility + falsifier prose and the prompt-SHA record."""
    for case_id in fixtures.TRAP_IDS:
        record = fixtures.TRAP_VETTING[case_id]
        assert record["plausibility"].strip()
        assert record["falsifier"].strip()
        assert set(record["prompts"]) == {
            "specialist_correctness",
            "specialist_security",
            "specialist_tests",
            "verifier",
        }
        has_avoidance = bool(record.get("avoidance_log", "").strip())
        has_kill = bool(record.get("kill_reason", "").strip())
        assert has_avoidance != has_kill, f"{case_id}: exactly one vetting arm"
        assert record["mode"] in ("generator-avoidance", "verifier-kill")
        if record["mode"] == "generator-avoidance":
            assert has_avoidance
        else:
            assert has_kill


def test_trap_nonce_absent_from_finding_fields():
    """HLD §5 boundary-break signal: no trap's diff text or decoy
    finding fields may carry a `nonce="..."` delimiter (recomputed from
    the builders, not trusted from the records)."""
    import json

    for case_id in fixtures.TRAP_IDS:
        diff_text, _, _ = fixtures.CORPUS[case_id]()
        decoy = json.dumps(fixtures.TRAP_VETTING[case_id]["decoy"], sort_keys=True)
        for text in (diff_text, decoy):
            assert 'nonce="' not in text, f"{case_id}: nonce pattern present"


def test_trap_prompts_undegraded():
    """Prompt-diff proof (HLD D8 Trap Validation Protocol): the vetting
    ran against byte-identical production prompts — re-hash disk and
    compare. ANY prompt edit fails here and forces re-vetting (prompts
    are NEVER degraded to force hallucinations)."""
    import hashlib
    from pathlib import Path

    prompts_dir = Path(fixtures.__file__).resolve().parent.parent.parent / "prompts"
    filenames = {
        "specialist_correctness": "specialist_correctness.md",
        "specialist_security": "specialist_security.md",
        "specialist_tests": "specialist_tests.md",
        "verifier": "verifier.md",
    }
    for case_id in fixtures.TRAP_IDS:
        pinned = fixtures.TRAP_VETTING[case_id]["prompts"]
        for key, filename in filenames.items():
            live = hashlib.sha256((prompts_dir / filename).read_bytes()).hexdigest()
            assert pinned[key] == live, f"{case_id}: {filename} drifted — re-vet the trap"
