"""Worker zip prompt-packaging contract (T068).

The 2026-09-27 incident: the worker `archive_file` carried exactly one
`prompts/` file (system_prompt.md) while `_load_fanout_prompts` needs
the five `_FANOUT_PROMPT_FILES` templates — every Phase-0 shadow
failed fast with `ConfigError("fanout_prompt", "missing")`
(`shadow_failed` warning on each review; no LLM cost, single_pass
unaffected). The bundle is built from lambda/ (`**/*.py` only) plus
explicit prompt sources, so a prompt present in the repo but absent
from the zip is silently missing from the deployed artifact. This pin
forces the packaging to include the whole prompts/ directory via
fileset, so repo prompts and zip prompts cannot drift, and the
packaging pattern to actually cover every file the runtime manifest
requires.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")
WORKER_HANDLER = (
    Path(__file__).resolve().parent.parent.parent / "lambda" / "worker_handler.py"
).read_text(encoding="utf-8")


def _worker_archive_block():
    match = re.search(r'data "archive_file" "worker" \{(.*?)\n\}', COMPUTE_TF, re.DOTALL)
    assert match is not None, "worker archive_file block not found — the scan broke"
    return match.group(1)


def test_worker_zip_packages_the_prompts_directory():
    block = _worker_archive_block()
    assert re.search(r'fileset\("\$\{path\.module\}/\.\./prompts", "\*\.md"\)', block), (
        "worker archive_file must package prompts/*.md via fileset — a "
        "prompt in the repo but absent from the zip is a runtime "
        "ConfigError (the T068 incident)"
    )
    assert re.search(r'filename\s*=\s*"prompts/\$\{source\.value\}"', block), (
        "worker archive_file must place prompts at zip-root prompts/"
    )


def test_packaging_pattern_covers_the_runtime_manifest():
    """fileset("*.md") covers single-component .md paths only; if the
    manifest ever requires another shape (subdirectory, non-.md), the
    packaging pattern and this contract must change consciously."""
    manifest_block = re.search(r"_FANOUT_PROMPT_FILES\s*=\s*\{(.*?)\}", WORKER_HANDLER, re.DOTALL)
    assert manifest_block is not None, (
        "_FANOUT_PROMPT_FILES not found in worker_handler.py — the scan broke"
    )
    entries = re.findall(r'"([a-z_]+)":\s*"([^"]+)"', manifest_block.group(1))
    assert len(entries) >= 5, f"unexpected manifest shape: {entries}"
    for _key, filename in entries:
        assert re.fullmatch(r"[A-Za-z0-9_-]+\.md", filename), (
            f"manifest file {filename!r} is not covered by the "
            'fileset("*.md") packaging pattern — revisit compute.tf '
            "and this contract together (T068)"
        )
