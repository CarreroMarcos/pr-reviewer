"""Prompt-bundle presence contract (T064).

`_load_fanout_prompts` resolves every `_FANOUT_PROMPT_FILES` entry
against the repo `prompts/` directory at runtime; a missing or renamed
file is a permanent `ConfigError` → worker alert on the first review
after the bad merge. This row catches that at PR time: every manifest
entry must exist in the repo with non-empty, decodable UTF-8 content.
(The zip-packaging half — repo prompts reaching worker.zip — is the
T068 contract in test_terraform_prompt_packaging.py.)
"""

from pathlib import Path

from worker_handler import _FANOUT_PROMPT_FILES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_fanout_prompt_manifest_files_exist_with_content():
    assert _FANOUT_PROMPT_FILES, "prompt manifest unexpectedly empty"
    for key, filename in _FANOUT_PROMPT_FILES.items():
        path = REPO_ROOT / "prompts" / filename
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AssertionError(f"prompt for {key!r} unreadable: {path}") from exc
        assert content, f"prompt for {key!r} is empty: {path}"
