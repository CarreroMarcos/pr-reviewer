"""Static replay-viewer pins (T055).

The per-review replay page (static/index.html + review.css + review.js)
is the render boundary for model-controlled archive bytes (HLD §5, §7):
every reasoning excerpt, finding field, and raw archive byte passes
through the JS three-phase span-preserving sanitizer (mirroring
lambda/common/sanitize.py) and reaches the DOM via textContent-backed
nodes only. The replay token lives in sessionStorage and rides fetch
headers — never the URL, never localStorage. No third-party assets.

These are source-level pins (same pattern as
tests/model_evals/test_corpus_registry.py): they read the shipped
sources and assert the security/behavior contract holds. If a pin's
scan breaks (renamed function, restructured file), the pin names what
moved — update the pin and the source together, never silence it.
"""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
HTML_PATH = STATIC_DIR / "index.html"
CSS_PATH = STATIC_DIR / "review.css"
JS_PATH = STATIC_DIR / "review.js"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot read {path.name} relative to tests/contracts — this "
            "contract assumes the repo layout (repo root two levels up)"
        ) from exc


HTML = _read(HTML_PATH)
CSS = _read(CSS_PATH)
JS = _read(JS_PATH)


def test_viewer_flat_files_match_server_allowlist():
    """static/ carries exactly the flat files the viewer Lambda serves:
    index.html plus assets whose names match ^[A-Za-z0-9._-]+$ (the
    _STATIC_RE character class in lambda/viewer_handler.py)."""
    assert HTML_PATH.exists() and CSS_PATH.exists() and JS_PATH.exists()
    for path in sorted(STATIC_DIR.iterdir()):
        assert path.is_file(), f"static/{path.name} is not a flat file"
        assert re.fullmatch(r"[A-Za-z0-9._-]+", path.name), (
            f"static/{path.name} breaks the viewer _STATIC_RE allowlist"
        )
    assert (STATIC_DIR / "index.html").exists(), "viewer serves static/index.html as the shell"


def test_no_third_party_sources():
    """No CDN links, no external URLs in script/link/img sources — the
    page is single-origin by construction (HLD §7)."""
    for name, source in (("index.html", HTML), ("review.css", CSS), ("review.js", JS)):
        assert "https://" not in source and "http://" not in source, (
            f"{name} contains an external URL — third-party sources are forbidden"
        )
    assert re.search(r'<script\s+src="/static/', HTML), "JS must load from /static/"
    assert re.search(r'<link\s+[^>]*href="/static/', HTML), "CSS must load from /static/"
    assert "<script src=" not in HTML.replace('<script src="/static/', ""), (
        "only /static/ script sources are allowed"
    )


def test_sanitizer_three_phases_present_and_ordered():
    """The JS sanitizer mirrors sanitize.py's three phases: fenced-block
    extraction, then inline-span extraction, then prose neutralization,
    then span restoration — in that order inside sanitizeMarkdown."""
    for fn in (
        "extractFencedBlocks",
        "extractInlineSpans",
        "neutralizeProseText",
        "restoreSpans",
        "sanitizeMarkdown",
    ):
        assert re.search(rf"function {fn}\(", JS), f"{fn}() missing from review.js"
    body = re.search(r"function sanitizeMarkdown\(.*?\) \{(.*?)\n\}", JS, re.DOTALL)
    assert body is not None, "sanitizeMarkdown body not found — the scan broke"
    phases = (
        "extractFencedBlocks",
        "extractInlineSpans",
        "neutralizeProseText",
        "restoreSpans",
    )
    positions = [body.group(1).index(call) for call in phases]
    assert positions == sorted(positions), (
        "sanitizer phases out of order — must be fences, inline, neutralize, restore"
    )


def test_archive_key_validation_before_interpolation():
    """archive_s3_key is server-shaped data, never trusted markup: an
    anchored allowlist mirroring the viewer Lambda's _ARCHIVE_RE must
    gate interpolation, with validated run_id/sha components feeding the
    fallback branch."""
    match = re.search(r"var ARCHIVE_KEY_RE = /(.*?)/;", JS)
    assert match is not None, "ARCHIVE_KEY_RE allowlist missing from review.js"
    pattern = match.group(1)
    assert pattern.startswith("^") and pattern.endswith("$"), "key allowlist must be anchored"
    for fragment in (r"runs\/", r"(\d+)", "[0-9a-f]{40}", "[0-9a-f]{32}", r"events\.jsonl"):
        assert fragment in pattern, f"key allowlist lost its {fragment} contract"
    assert "key.match(ARCHIVE_KEY_RE)" in JS, "archive key must be validated before use"
    assert "[0-9a-f]{32}" in JS, "run_id fallback component must be shape-validated"
    assert "[0-9a-f]{40}" in JS, "sha fallback component must be shape-validated"


def test_severity_allowlist_before_class_interpolation():
    """Model-controlled severity text must pass an explicit HIGH/MEDIUM/
    LOW allowlist (default LOW) before reaching className — explicit
    equality, so prototype-chain names can never pass."""
    assert re.search(r"function cleanSeverity\(value\)", JS), "cleanSeverity() missing"
    for level in ("HIGH", "MEDIUM", "LOW"):
        assert f'"{level}"' in JS, f"{level} missing from the severity allowlist"
    assert "cleanSeverity(item.severity)" in JS, "finding render must use cleanSeverity"
    assert 'typeof item.severity === "string" ? item.severity : "LOW"' not in JS, (
        "raw severity interpolation must not remain"
    )


def test_sanitizer_neutralization_semantics():
    """Span stash uses NUL framing (fail-closed collision domain, like
    sanitize.py's \\x00CODE_SPAN_N\\x00); prose neutralization defuses
    images before links plus bare <http> autolinks; non-strings and
    NUL-bearing input raise instead of rendering."""
    assert "CODE_SPAN_" in JS and '"\\x00"' in JS, "NUL-framed stash placeholders missing"
    assert "[Image: $1]" in JS, "image neutralization missing"
    assert re.search(r"replace\(linkRe", JS), "link neutralization missing"
    assert "autolinkRe" in JS, "angle-bracket autolink neutralization missing"
    assert "bad_type" in JS and "nul_byte" in JS, "fail-closed input guards missing"


def test_no_attack_syntax_literals_in_shipped_js():
    """Real attack-syntax probes live HERE, in the test — never in the
    shipped JS (comments included). The JS keeps only inert mechanism
    markers (CODE_SPAN_, NUL framing); every markup-unsafe sink is
    absent, so sanitized content can only reach the DOM through
    textContent-backed nodes."""
    shipped = JS + HTML + CSS
    for probe in (
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<svg onload=alert(1)>",
        "javascript:alert(1)",
    ):
        assert probe not in shipped, f"attack-syntax literal ships in static/: {probe}"
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in JS, f"markup-unsafe sink {sink} present in review.js"
    assert "textContent" in JS, "model-controlled leaves must render via textContent"


def test_specialty_prototype_safe_and_stage_null_guarded():
    """planReplay's known-node set must be null-prototype (a __proto__ /
    constructor specialty can never pass a prototype read) and
    applyStage must null-check its node — unknown stages skip, never
    throw inside the replay tick."""
    assert "Object.create(null)" in JS, "known-node set must be null-prototype"
    assert re.search(r"if \(node === null\)", JS), "applyStage must null-guard its node"
    assert "known[ev.specialty]" in JS, "specialty filter must gate stage planning"


def test_boot_autoload_shares_form_validation():
    """The query-param auto-load must pass the same PR/SHA validator as
    the login form — one shared gate, so a crafted link cannot smuggle
    path characters into the /api request."""
    assert re.search(r"function prError\(pr\)", JS), "shared PR validator missing"
    assert re.search(r"function shaError\(sha\)", JS), "shared SHA validator missing"
    assert "loadReview(params.pr, params.sha)" not in JS, "unvalidated auto-load must not remain"
    assert re.search(r"prError\(params\.pr\)", JS), "auto-load must validate pr"
    assert re.search(r"shaError\(autoSha\)", JS), "auto-load must validate sha"


def test_replay_button_bound_once_to_current_steps():
    """The Replay button wires once against module-level currentSteps
    (assigned per successful load) — never a per-load stale closure."""
    assert "var currentSteps = []" in JS, "module-level currentSteps missing"
    assert "currentSteps = steps" in JS, "successful load must refresh currentSteps"
    assert "runReplay(currentSteps)" in JS, "Replay button must run currentSteps"


def test_finding_line_number_guarded():
    """Finding locations coerce non-numeric line coordinates to an em
    dash — model-controlled line text never renders as a bare value."""
    assert 'typeof item.line_start === "number"' in JS, "line_start type guard missing"
    assert '(lineNo === null ? "—"' in JS, "non-numeric lines must degrade to a dash"


def test_token_hygiene_sessionstorage_only():
    """Token in sessionStorage ONLY: set/get/remove present, localStorage
    absent, Bearer attached via fetch headers — never the URL."""
    assert "localStorage" not in JS, "localStorage is forbidden for the replay token"
    for op in ("sessionStorage.getItem", "sessionStorage.setItem", "sessionStorage.removeItem"):
        assert op in JS, f"{op} missing — token lifecycle must stay in sessionStorage"
    assert "Authorization" in JS and "Bearer" in JS, "Bearer header missing"
    assert re.search(r"fetch\(.*headers", JS, re.DOTALL), (
        "fetch must carry headers (the Bearer token)"
    )
    assert "token=" not in JS and "?token" not in JS, "token must never appear in a URL"


def test_reasoning_excerpt_guarded_render():
    """reasoning_excerpt renders WHEN PRESENT: a type-and-nonempty guard
    precedes any card construction — absent means no field, not a
    placeholder."""
    assert "reasoning_excerpt" in JS, "reasoning_excerpt handling missing"
    assert re.search(r'typeof .*reasoning_excerpt.*=== *"string"', JS), (
        "reasoning_excerpt presence guard missing"
    )
    assert 'ev.reasoning_excerpt !== ""' in JS, "empty-excerpt guard missing"


def test_api_contract_shapes():
    """The shell speaks the T053/T054 viewer contract: latest-run lookup
    plus the two archive objects, all on same-origin relative paths.
    Asserts bind to the actual construction and field-usage sites, not
    bare substrings a comment could satisfy."""
    assert re.search(r'"/api/runs/" \+ pr \+ "/latest"', JS), (
        "latest-run fetch must build /api/runs/{pr}/latest"
    )
    assert re.search(r'"/runs/" \+ pr \+ "/"', JS), "archive fetch must build /runs/{pr}/…"
    assert re.search(r'\+ "/events\.jsonl"', JS), "events.jsonl must be a built path suffix"
    assert re.search(r'\+ "/meta\.json"', JS), "meta.json must be a built path suffix"
    for field in ("latest.run_id", "latest.archive_s3_key", "latest.sha"):
        assert field in JS, f"latest-run field usage {field} missing"
    assert "key.match(ARCHIVE_KEY_RE)" in JS, "archive key must pass the allowlist"
