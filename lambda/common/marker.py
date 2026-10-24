"""Canonical comment marker builder (ARCHITECTURE.md §2.8, §4).

Single source of truth for the worker-injected marker:
`<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->`.
The model never emits it; external tooling must not parse it.

Pure stdlib, no I/O.
"""

MARKER_VERSION = "v1"


def build_marker(repo_full_name: str, pr_number: int) -> str:
    """Build the canonical marker for a repo/PR pair. Precondition: inputs
    are envelope-validated per ARCHITECTURE.md §2.1 — no validation here."""
    return f"<!-- pr-reviewer:canonical:{MARKER_VERSION}:{repo_full_name}#{pr_number} -->"
