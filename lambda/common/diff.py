"""GitHub diff fetch + shape validation + budget + lockfile summaries.

HLD §2.3 items 2–4; FR-013; failure modes 16/18/19; Constitution II/III/VI.

Item 2 — endpoints are CONSTRUCTED from the validated envelope identifiers
(`repo_full_name` + `pr_number`); response-embedded or payload-supplied URLs
are never used for navigation (failure mode 18), so the fetch surface has no
URL parameter at all. Every request pins
`X-GitHub-Api-Version: 2026-03-10` (failure mode 19) and an explicit
User-Agent. PR-meta responses are shape-validated (HTTP 200, `head.sha`
40-hex) before the value is returned for the live-head fence; anything else
raises typed `DiffError`.

Item 3 — deterministic pre-model budget: `MAX_FILES = 500`,
`MAX_CHANGED_LINES = 25_000` (additions + deletions),
`MAX_INPUT_BYTES = 800_000` (UTF-8 patch bytes).

Item 4 — lockfiles are excluded from review content and folded into a
deterministic summary (sorted by filename, no timestamps: same input bytes
→ byte-identical summary).

Interpretations (HLD silent — flagged, conservative readings):
  1. Over-budget input is TRUNCATED deterministically (sorted-filename
     order, first-fit within every cap) with `truncated=True`, not refused:
     refusing would drop the whole review for one large PR, while silent
     pass-through would breach the pre-model bound (failure mode 16).
  2. Enforcement order is file-count → changed-lines → input-bytes, checked
     per file in sorted-filename order.
  3. `LOCKFILE_NAMES` is defined here: the HLD gives one example string, no
     canonical list, so the set below (js/python/rust/go/ruby/php ecosystems
     + `requirements.txt`) is the contract until the HLD names one.
  4. `urlopen` takes a single timeout, so `TIMEOUT_SECONDS = 10` covers the
     HLD §2.3 GitHub read budget (10 s); the 2 s connect split is
     unexpressible in stdlib and documented here rather than silently claimed.
  5. `/files` pagination uses constructed `?per_page=&page=` URLs only:
     following `Link`-header URLs would violate failure mode 18. Fetching
     stops at the first short page, at `MAX_FILES` accumulated entries, or
     at the page safety cap.

Stdlib + typing only; no I/O except through the injected `_transport`
seam, so tests never hit the network. Secret values never appear in
`DiffError` text (Constitution III).

Error semantics for the worker's §2.3 item 8 classification: `http_error`
(non-200, `status` attached) and `bad_shape`/`bad_sha` are the fetch-side
signals; retry-vs-complete is decided by the worker, not here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
USER_AGENT = "pr-reviewer/1.0"

MAX_FILES = 500
MAX_CHANGED_LINES = 25_000
MAX_INPUT_BYTES = 800_000

TIMEOUT_SECONDS = 10
FILES_PER_PAGE = 100
MAX_FILE_PAGES = MAX_FILES // FILES_PER_PAGE + 2

# Interpretation 3: HLD names no canonical lockfile list; this set is the
# contract (basenames, matched case-sensitively — GitHub filenames are exact).
LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "requirements.txt",
        "Gemfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
    }
)

EMPTY_LOCKFILE_SUMMARY = "lockfiles: no changes"

# Anchors are \Z, not $ (mirrors common.envelope: $ admits trailing newline).
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")

MAX_REPO_LENGTH = 128
MAX_PR_NUMBER = 10**9


class DiffError(ValueError):
    """Typed diff rejection: `field` names the offending field
    (`repo_full_name`, `pr_number`, `head.sha`, `response`, `files`,
    `pr` for HTTP status), `reason` is a machine-readable code
    (`bad_repo`, `bad_pr_number`, `bad_shape`, `bad_sha`, `http_error`,
    `bad_page`), and `status` carries the HTTP status for `http_error`
    (else `None`). Never carries secret values."""

    def __init__(self, field_name: str, reason: str, status: int | None = None) -> None:
        self.field = field_name
        self.reason = reason
        self.status = status
        super().__init__(f"invalid diff: {field_name}: {reason}")


@dataclass(frozen=True)
class HttpResponse:
    """Minimal transport response: HTTP status, raw body bytes, headers."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


Transport = Callable[[str, Mapping[str, str]], HttpResponse]


@dataclass(frozen=True)
class DiffFile:
    """One parsed `/files` entry: line counts plus patch text."""

    filename: str
    additions: int
    deletions: int
    patch: str


@dataclass(frozen=True)
class DiffResult:
    """Budgeted review content: `head_sha` is the shape-validated PR head
    for the live-head fence; `files` holds kept non-lockfile content;
    totals describe the kept (model-facing) bytes/lines; `truncated` marks
    deterministically dropped remainder; `lockfile_summary` is deterministic."""

    head_sha: str
    files: tuple[DiffFile, ...]
    total_additions: int
    total_deletions: int
    total_bytes: int
    truncated: bool
    lockfile_summary: str


def _check_repo(repo_full_name: Any) -> str:
    if (
        not isinstance(repo_full_name, str)
        or len(repo_full_name) > MAX_REPO_LENGTH
        or not _REPO_RE.match(repo_full_name)
    ):
        raise DiffError("repo_full_name", "bad_repo")
    return repo_full_name


def _check_pr_number(pr_number: Any) -> int:
    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or not 1 <= pr_number <= MAX_PR_NUMBER
    ):
        raise DiffError("pr_number", "bad_pr_number")
    return pr_number


def build_pr_url(repo_full_name: str, pr_number: int) -> str:
    """Construct the PR-meta endpoint from validated identifiers only."""
    repo = _check_repo(repo_full_name)
    number = _check_pr_number(pr_number)
    return f"{GITHUB_API_BASE}/repos/{repo}/pulls/{number}"


def build_pr_files_url(
    repo_full_name: str, pr_number: int, *, page: int = 1, per_page: int = FILES_PER_PAGE
) -> str:
    """Construct the PR-files endpoint from validated identifiers only."""
    repo = _check_repo(repo_full_name)
    number = _check_pr_number(pr_number)
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise DiffError("page", "bad_page")
    if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1:
        raise DiffError("per_page", "bad_page")
    return f"{GITHUB_API_BASE}/repos/{repo}/pulls/{number}/files?per_page={per_page}&page={page}"


def _request_headers(github_token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {github_token}",
    }


def _default_transport(url: str, headers: Mapping[str, str]) -> HttpResponse:
    request = Request(url, headers=dict(headers))  # noqa: S310 (https allow-listed base)
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return HttpResponse(
            status=response.status,
            body=response.read(),
            headers=dict(response.headers.items()),
        )


def _check_head_sha(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise DiffError("response", "bad_shape")
    head = payload.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise DiffError("head.sha", "bad_sha")
    return sha


def fetch_pr_head_sha(
    repo_full_name: str,
    pr_number: int,
    *,
    github_token: str,
    _transport: Transport | None = None,
) -> str:
    """GET the PR-meta endpoint and return the shape-validated `head.sha`.

    Raises `DiffError`: `bad_repo`/`bad_pr_number` before any I/O,
    `http_error` (`status` attached) on non-200, `bad_shape` on unparseable
    bodies, `bad_sha` when `head.sha` is not 40-hex.
    """
    url = build_pr_url(repo_full_name, pr_number)
    transport = _transport if _transport is not None else _default_transport
    response = transport(url, _request_headers(github_token))
    if response.status != 200:
        raise DiffError("pr", "http_error", status=response.status)
    try:
        payload = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise DiffError("response", "bad_shape") from None
    return _check_head_sha(payload)


def parse_files(entries: Any) -> tuple[DiffFile, ...]:
    """Parse a `/files` JSON list into `DiffFile`s; fail closed on shape."""
    if not isinstance(entries, list):
        raise DiffError("files", "bad_shape")
    parsed: list[DiffFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DiffError("files", "bad_shape")
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename:
            raise DiffError("files", "bad_shape")
        additions = entry.get("additions", 0)
        deletions = entry.get("deletions", 0)
        patch = entry.get("patch", "")
        if (
            not isinstance(additions, int)
            or isinstance(additions, bool)
            or additions < 0
            or not isinstance(deletions, int)
            or isinstance(deletions, bool)
            or deletions < 0
            or not isinstance(patch, str)
        ):
            raise DiffError("files", "bad_shape")
        parsed.append(
            DiffFile(filename=filename, additions=additions, deletions=deletions, patch=patch)
        )
    return tuple(parsed)


def _is_lockfile(filename: str) -> bool:
    return filename.rsplit("/", 1)[-1] in LOCKFILE_NAMES


def summarize_lockfiles(files: Sequence[DiffFile]) -> str:
    """Deterministic dependency-change summary: sorted by filename, no
    timestamps — identical input yields byte-identical text."""
    locked = sorted((f for f in files if _is_lockfile(f.filename)), key=lambda f: f.filename)
    if not locked:
        return EMPTY_LOCKFILE_SUMMARY
    total_add = sum(f.additions for f in locked)
    total_del = sum(f.deletions for f in locked)
    parts = [f"{f.filename}: +{f.additions}/-{f.deletions} lines" for f in locked]
    return f"lockfiles: {len(locked)} files (+{total_add}/-{total_del} lines): " + "; ".join(parts)


def apply_budget(files: Sequence[DiffFile]) -> tuple[tuple[DiffFile, ...], bool, int, int, int]:
    """Enforce the item-3 budget in sorted-filename order; returns
    (kept, truncated, kept_additions, kept_deletions, kept_bytes)."""
    kept: list[DiffFile] = []
    kept_add = kept_del = kept_bytes = 0
    truncated = False
    for candidate in sorted(files, key=lambda f: f.filename):
        patch_bytes = len(candidate.patch.encode("utf-8"))
        if (
            len(kept) >= MAX_FILES
            or kept_add + kept_del + candidate.additions + candidate.deletions > MAX_CHANGED_LINES
            or kept_bytes + patch_bytes > MAX_INPUT_BYTES
        ):
            truncated = True
            continue
        kept.append(candidate)
        kept_add += candidate.additions
        kept_del += candidate.deletions
        kept_bytes += patch_bytes
    return tuple(kept), truncated, kept_add, kept_del, kept_bytes


def fetch_diff(
    repo_full_name: str,
    pr_number: int,
    *,
    github_token: str,
    _transport: Transport | None = None,
) -> DiffResult:
    """Fetch PR meta (validated `head_sha` for the fence) + files, drop
    lockfiles into the deterministic summary, and budget the review content.

    Raises `DiffError` with the same field/reason contract as
    `fetch_pr_head_sha` (plus `bad_shape` for malformed `/files` bodies).
    """
    transport = _transport if _transport is not None else _default_transport
    head_sha = fetch_pr_head_sha(
        repo_full_name, pr_number, github_token=github_token, _transport=transport
    )
    headers = _request_headers(github_token)
    raw: list[DiffFile] = []
    page = 0
    while len(raw) < MAX_FILES and page < MAX_FILE_PAGES:
        page += 1
        url = build_pr_files_url(repo_full_name, pr_number, page=page)
        response = transport(url, headers)
        if response.status != 200:
            raise DiffError("files", "http_error", status=response.status)
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise DiffError("files", "bad_shape") from None
        batch = parse_files(payload)
        raw.extend(batch)
        if len(batch) < FILES_PER_PAGE:
            break
    lockfile_summary = summarize_lockfiles(raw)
    review = [f for f in raw if not _is_lockfile(f.filename)]
    kept, truncated, kept_add, kept_del, kept_bytes = apply_budget(review)
    dropped_overflow = len(review) > len(kept)
    return DiffResult(
        head_sha=head_sha,
        files=kept,
        total_additions=kept_add,
        total_deletions=kept_del,
        total_bytes=kept_bytes,
        truncated=truncated or dropped_overflow,
        lockfile_summary=lockfile_summary,
    )
