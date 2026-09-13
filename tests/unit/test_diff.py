"""T027: diff fetch contract tests (HLD §2.3 items 2–4; FR-013).

Endpoints constructed from `repo_full_name` + `pr_number` (never payload
URLs), `X-GitHub-Api-Version: 2026-03-10` + explicit User-Agent, response
shape-validation (`head.sha` 40-hex) before the fence, diff budget
`MAX_FILES=500` / `MAX_CHANGED_LINES=25000` / `MAX_INPUT_BYTES=800000`,
deterministic lockfile summaries.

Red-first: this file imports `common.diff`, which does not exist yet, so
collection fails with ImportError until T031 implements it.
"""

import inspect
import json

from common import diff

REPO = "octo-org/hello-world"
PR_NUMBER = 42
HEAD_SHA = "aa" * 20
TOKEN = "dummy-github-token-not-a-credential"  # noqa: S105 (dummy fixture)


def meta_body(sha=HEAD_SHA):
    return json.dumps({"head": {"sha": sha}}).encode("utf-8")


def files_body(entries):
    return json.dumps(entries).encode("utf-8")


def file_entry(name, additions=10, deletions=5, patch="line\n"):
    return {"filename": name, "additions": additions, "deletions": deletions, "patch": patch}


class FakeTransport:
    """URL-routed transport double: records (url, headers), serves canned bodies."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append({"url": url, "headers": dict(headers)})
        status, body = self.routes[url]
        return diff.HttpResponse(status=status, body=body, headers={})

    def headers_for(self, url):
        return next(call["headers"] for call in self.calls if call["url"] == url)


def never_called(url, headers):  # pragma: no cover - fail-closed tripwire
    raise AssertionError(f"network touched during validation: {url}")


# --- budget + header constants -------------------------------------------------


def test_budget_constants():
    assert diff.MAX_FILES == 500
    assert diff.MAX_CHANGED_LINES == 25000
    assert diff.MAX_INPUT_BYTES == 800000


def test_api_version_pin_and_explicit_user_agent():
    assert diff.GITHUB_API_VERSION == "2026-03-10"
    assert isinstance(diff.USER_AGENT, str) and len(diff.USER_AGENT) > 0


# --- endpoint construction (identifiers only, never payload URLs) --------------


def test_pr_url_constructed_from_identifiers():
    assert diff.build_pr_url(REPO, PR_NUMBER) == (
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    )


def test_pr_files_url_constructed_from_identifiers():
    url = diff.build_pr_files_url(REPO, PR_NUMBER)
    assert url.startswith(f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files")


def test_invalid_repo_rejected_before_network():
    try:
        diff.build_pr_url("https://evil.example/x", PR_NUMBER)
    except diff.DiffError as exc:
        assert exc.field == "repo_full_name"
    else:
        raise AssertionError("expected DiffError")
    # fetch entry points validate before touching the transport too
    try:
        diff.fetch_pr_head_sha(
            "https://evil.example/x", PR_NUMBER, github_token=TOKEN, _transport=never_called
        )
    except diff.DiffError as exc:
        assert exc.field == "repo_full_name"
    else:
        raise AssertionError("expected DiffError")


def test_invalid_pr_number_rejected_before_network():
    for bad in (0, -7, "42", True, None):
        try:
            diff.build_pr_url(REPO, bad)
        except diff.DiffError as exc:
            assert exc.field == "pr_number", bad
        else:
            raise AssertionError(f"expected DiffError for {bad!r}")
        try:
            diff.fetch_pr_head_sha(REPO, bad, github_token=TOKEN, _transport=never_called)
        except diff.DiffError as exc:
            assert exc.field == "pr_number", bad
        else:
            raise AssertionError(f"expected DiffError for {bad!r}")


def test_no_payload_url_parameter_on_fetch_surface():
    """HLD failure mode 18: response-embedded/payload URLs are never used for
    navigation, so the fetch surface must not even accept one."""
    for func in (diff.fetch_pr_head_sha, diff.fetch_diff):
        params = set(inspect.signature(func).parameters)
        assert not params & {"url", "diff_url", "comments_url", "payload_url", "href"}, func


# --- request headers -----------------------------------------------------------


def test_api_version_and_user_agent_headers_sent():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (200, meta_body())})
    diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
    sent = transport.headers_for(url)
    assert sent["X-GitHub-Api-Version"] == "2026-03-10"
    assert sent["User-Agent"] == diff.USER_AGENT


def test_authorization_header_uses_token():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (200, meta_body())})
    diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
    assert transport.headers_for(url)["Authorization"] == f"Bearer {TOKEN}"


# --- response shape validation (before the fence) ------------------------------


def test_valid_meta_returns_head_sha():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (200, meta_body(HEAD_SHA))})
    assert (
        diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
        == HEAD_SHA
    )


def test_missing_head_rejected():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (200, json.dumps({}).encode())})
    try:
        diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
    except diff.DiffError as exc:
        assert exc.field == "head.sha"
    else:
        raise AssertionError("expected DiffError")


def test_malformed_sha_rejected():
    for bad_sha in ("ABCDEF" + "ab" * 17, "zz" * 20, "aa" * 19, "aa" * 20 + "aa", 42, None):
        url = diff.build_pr_url(REPO, PR_NUMBER)
        transport = FakeTransport({url: (200, meta_body(bad_sha))})
        try:
            diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
        except diff.DiffError as exc:
            assert exc.field == "head.sha", bad_sha
        else:
            raise AssertionError(f"expected DiffError for {bad_sha!r}")


def test_non_200_raises_http_error():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (404, b"{}")})
    try:
        diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
    except diff.DiffError as exc:
        assert exc.reason == "http_error"
        assert exc.status == 404
    else:
        raise AssertionError("expected DiffError")


def test_non_json_body_rejected():
    url = diff.build_pr_url(REPO, PR_NUMBER)
    transport = FakeTransport({url: (200, b"<html>not json")})
    try:
        diff.fetch_pr_head_sha(REPO, PR_NUMBER, github_token=TOKEN, _transport=transport)
    except diff.DiffError as exc:
        assert exc.reason == "bad_shape"
    else:
        raise AssertionError("expected DiffError")


# --- diff budget (deterministic, pre-model) ------------------------------------


def budget_routes(entries):
    return {
        diff.build_pr_url(REPO, PR_NUMBER): (200, meta_body()),
        diff.build_pr_files_url(REPO, PR_NUMBER): (200, files_body(entries)),
    }


def test_file_count_cap():
    entries = [file_entry(f"src/file{i:04d}.py") for i in range(diff.MAX_FILES + 1)]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert result.truncated is True
    assert len(result.files) == diff.MAX_FILES


def test_changed_lines_cap():
    per_file = 1000
    entries = [file_entry(f"src/file{i}.py", additions=per_file, deletions=0) for i in range(30)]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert result.truncated is True
    assert result.total_additions + result.total_deletions <= diff.MAX_CHANGED_LINES


def test_input_bytes_cap():
    big_patch = "x" * 100_000
    entries = [file_entry(f"src/file{i}.py", patch=big_patch) for i in range(10)]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert result.truncated is True
    assert result.total_bytes <= diff.MAX_INPUT_BYTES


def test_within_budget_not_truncated():
    entries = [file_entry("src/main.py")]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert result.truncated is False
    assert len(result.files) == 1


# --- lockfile summaries (deterministic) ----------------------------------------


def test_lockfile_set_covers_common_lockfiles():
    for name in ("package-lock.json", "poetry.lock", "uv.lock", "requirements.txt"):
        assert name in diff.LOCKFILE_NAMES, name


def test_lockfile_summary_deterministic():
    entries = [
        file_entry("uv.lock", additions=120, deletions=45),
        file_entry("src/main.py"),
        file_entry("package-lock.json", additions=13, deletions=2),
    ]
    first = diff.summarize_lockfiles(diff.parse_files(entries))
    second = diff.summarize_lockfiles(diff.parse_files(list(reversed(entries))))
    assert first == second
    assert "uv.lock" in first and "package-lock.json" in first
    assert "src/main.py" not in first


def test_lockfile_summary_byte_identical_across_calls():
    entries = [file_entry("poetry.lock", additions=7, deletions=1)]
    parsed = diff.parse_files(entries)
    assert diff.summarize_lockfiles(parsed) == diff.summarize_lockfiles(parsed)


def test_lockfile_summary_empty_when_no_lockfiles():
    assert diff.summarize_lockfiles(diff.parse_files([file_entry("src/main.py")])) == (
        "lockfiles: no changes"
    )


def test_fetch_diff_excludes_lockfiles_but_summarizes():
    entries = [file_entry("src/main.py"), file_entry("uv.lock", additions=120, deletions=45)]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert [f.filename for f in result.files] == ["src/main.py"]
    assert "uv.lock" in result.lockfile_summary


# --- fetch_diff end-to-end (head_sha contract for the fence) -------------------


def test_fetch_diff_returns_validated_head_sha():
    entries = [file_entry("src/main.py")]
    result = diff.fetch_diff(
        REPO, PR_NUMBER, github_token=TOKEN, _transport=FakeTransport(budget_routes(entries))
    )
    assert result.head_sha == HEAD_SHA
    assert len(result.head_sha) == 40
    int(result.head_sha, 16)  # lowercase hex parses
