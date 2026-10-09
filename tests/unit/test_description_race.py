"""Description-vs-diff race guard (hardening b).

Drives the production `_make_review` review port: the diff transport serves
PR meta, the LLM double returns a review containing a description-judging
finding, and a post-LLM meta re-fetch decides whether that finding survives.

Pins:
- meta unchanged between fetch and re-fetch → model text passes through
  byte-identical (description finding intact);
- meta changed mid-review → description-referencing finding bullets are
  dropped, code findings survive, other sections untouched;
- re-fetch failure → best-effort degrade: text untouched, review publishes;
- the pure filter drops only `## Findings` bullets referencing the
  description/title; an emptied section carries the empty-findings sentinel.
"""

import json

from common.config import ConfigProvider
from common.diff import HttpResponse
from common.envelope import Envelope
from worker_handler import _Credentials, _drop_stale_description_findings, _make_review

HOST = "llm.example.test"
ENDPOINT = f"https://{HOST}/v1/chat/completions"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"  # noqa: S105 (fake fixture)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
SHA = "bb" * 20
TITLE_A = "Fix the login helper"
BODY_A = "Small cleanup with tests."
TITLE_B = "Fix the login helper (updated)"
BODY_B = "Small cleanup with tests. Now also touches auth."

DESC_FINDING = (
    '- [MEDIUM] `src/main.py:9` — The PR description says "one file" '
    "yet the diff touches two. Fix: update the description."
)
CODE_FINDING = "- [LOW] `src/main.py:12` — Missing bound. Fix: add a check."

REVIEW_WITH_BOTH = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n" + DESC_FINDING + "\n" + CODE_FINDING + "\n\n"
    "## Risk Notes\nNone.\n"
)
REVIEW_DESC_ONLY = (
    "## Summary\nAdds input validation.\n\n" "## Findings\n" + DESC_FINDING + "\n\n"
    "## Risk Notes\nNone.\n"
)


def _ssm_values():
    return {
        "/pr-reviewer/github-token": GITHUB_TOKEN_VALUE,
        "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
        "/pr-reviewer/glm-api-key": GLM_API_KEY_VALUE,
        "/pr-reviewer/glm-model": "glm-5.3-flash",
        "/pr-reviewer/glm-endpoint": ENDPOINT,
    }


class _FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class _StatefulDiffTransport:
    """PR-meta URL: first hit serves meta A; later hits serve meta B (the
    post-LLM re-fetch), raise when `fail_refetch`, or repeat meta A when
    `changed` is False. `/files` serves one patch."""

    def __init__(self, events, *, changed=True, fail_refetch=False):
        self._events = events
        self._changed = changed
        self._fail_refetch = fail_refetch
        self._meta_hits = 0

    def __call__(self, url, headers):
        self._events.append("diff")
        if "/files" in url:
            payload = [
                {
                    "filename": "src/main.py",
                    "additions": 5,
                    "deletions": 2,
                    "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                }
            ]
            return HttpResponse(status=200, body=json.dumps(payload).encode(), headers={})
        self._meta_hits += 1
        if self._meta_hits > 1 and self._fail_refetch:
            raise TimeoutError("meta refetch down")
        if self._meta_hits == 1 or not self._changed:
            title, body = TITLE_A, BODY_A
        else:
            title, body = TITLE_B, BODY_B
        return HttpResponse(
            status=200,
            body=json.dumps({"head": {"sha": SHA}, "title": title, "body": body}).encode(),
            headers={},
        )


class _FakeSocket:
    def settimeout(self, seconds):
        pass


class _FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body


class _BodyRecordingConnection:
    def __init__(self, events, body):
        self._events = events
        self._body = body
        self.sock = _FakeSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self._events.append("llm")

    def getresponse(self):
        return _FakeResponse(
            json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": self._body}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
        )

    def close(self):
        pass


def _run(*, review_body, changed=True, fail_refetch=False):
    events: list = []
    ssm = _FakeSSM(_ssm_values())
    provider = ConfigProvider(
        ssm.get_parameters, clock=lambda: 1_750_000_000, allowed_endpoint_hosts=(HOST,)
    )
    creds = _Credentials(provider)

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(events, review_body)

    review = _make_review(
        envelope=Envelope(
            envelope_version="v1",
            event_type="pull_request",
            action="opened",
            repo_full_name=REPO,
            pr_number=PR_NUMBER,
            head_sha=SHA,
            base_sha="00" * 20,
            sender="octo-user",
            delivery_guid="11111111-1111-4111-8111-111111111111",
        ),
        creds=creds,
        usage={"tokens": 0},
        diff_transport=_StatefulDiffTransport(
            events, changed=changed, fail_refetch=fail_refetch
        ),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        allowed_hosts=provider.allowed_hosts,
        clock=lambda: 1_750_000_000,
        github_transport=None,
    )
    return review(SHA, 0)


# Pure filter


def test_filter_drops_only_description_findings():
    text, dropped = _drop_stale_description_findings(REVIEW_WITH_BOTH)
    assert dropped == 1
    assert DESC_FINDING not in text
    assert CODE_FINDING in text
    assert text.startswith("## Summary\nAdds input validation.")


def test_filter_emptied_section_gets_sentinel():
    text, dropped = _drop_stale_description_findings(REVIEW_DESC_ONLY)
    assert dropped == 1
    assert "No significant issues found." in text
    assert "## Findings\nNo significant issues found." in text


def test_filter_leaves_other_sections_untouched():
    body = (
        "## Summary\nThe PR description is clear.\n\n"
        "## Findings\n" + CODE_FINDING + "\n\n"
        "## Risk Notes\nThe description covers the risk.\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body


def test_filter_ignores_bare_verb_without_description():
    body = "## Findings\n" + CODE_FINDING.replace("Missing bound", "Describes the bound") + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body


# End-to-end through _make_review


def test_meta_unchanged_passes_through():
    content = _run(review_body=REVIEW_WITH_BOTH, changed=False)
    assert DESC_FINDING in content
    assert CODE_FINDING in content


def test_meta_changed_drops_description_finding():
    content = _run(review_body=REVIEW_WITH_BOTH, changed=True)
    assert DESC_FINDING not in content
    assert CODE_FINDING in content
    assert "## Summary" in content


def test_meta_changed_all_dropped_yields_sentinel():
    content = _run(review_body=REVIEW_DESC_ONLY, changed=True)
    assert DESC_FINDING not in content
    assert "No significant issues found." in content


def test_refetch_failure_degrades_to_unfiltered(caplog):
    with caplog.at_level("WARNING", logger="worker_handler"):
        content = _run(review_body=REVIEW_WITH_BOTH, fail_refetch=True)
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    assert [r for r in caplog.records if r.getMessage() == "meta_refetch_unavailable"]
