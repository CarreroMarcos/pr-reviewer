"""T052: registered-state pin for the viewer Function URL hardening.

Closes the Gate-38 deferred finding on PR #144 (bot-concurred, owner
T052): the source-text pins in tests/contracts/test_viewer_infra.py
prove the .tf uses the provider-native typed arguments
(`function_url_auth_type` / `invoked_via_function_url`), but a source
pin cannot catch provider-version drift where the spelling passes and
the compiled posture changes. This module asserts the RESOLVED values
as registered in the HCP workspace state — including the viewer policy
document, where the Mars-ruled absence of `kms:Decrypt` is checked
against what the workspace has actually registered (DECISIONS
2026-09-28).

Mechanism note (PR #149 evidence-run discovery): this workspace runs
in HCP REMOTE execution — every plan executes in HCP against the
VCS-tracked configuration and saved plans are banned — so "compile
the local tree and assert the plan" is structurally unavailable. The
registered state is the honest compiled surface; see
`_registered_resources` below.

Evidence-class test (mars-law: terraform and full pytest run in
separate shells). It skips — never fails — outside its evidence
environment:

* no terraform binary, or no AWS creds (the evidence-intent signal —
  `eval "$(aws configure export-credentials --format env)"` first);
* the viewer handler is still the T051b stub — the Lambda's plan-time
  precondition (intentionally) hard-fails every plan until T054 lands
  the real handler, so this pin arms itself at T054;
* a pinned resource is not yet registered (T052's policy ships at the
  next tf-tag apply) — the specific test skips with that reason.

Evidence run:

    terraform -chdir=terraform init -backend=false   # once per checkout
    eval "$(aws configure export-credentials --format env)"
    uv run --frozen pytest tests/contracts/test_viewer_compiled_plan.py -v

This module never runs `init` itself: it only consumes the existing
provider install, so no evidence run writes into the working tree
(bot F1, PR #147).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
HANDLER = TERRAFORM_DIR.parent / "lambda" / "viewer_handler.py"

# Full path satisfies S607; every argument is repo-constructed (no
# untrusted input), so the S603 bandit check is suppressed at the call
# sites below.
TERRAFORM_BIN = shutil.which("terraform")


def _skip_reason() -> str | None:
    if TERRAFORM_BIN is None:
        return "terraform binary not on PATH"
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        return "AWS creds not exported — terraform plan reads data sources"
    if HANDLER.exists() and "NotImplementedError" in HANDLER.read_text(encoding="utf-8"):
        return (
            "viewer_handler.py is still the T051b stub — the plan-time "
            "precondition blocks every plan; this pin arms at T054"
        )
    if not (TERRAFORM_DIR / ".terraform" / "providers").exists():
        return (
            "provider not installed — run "
            "`terraform -chdir=terraform init -backend=false` once, then re-run"
        )
    return None


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


def _registered_resources() -> dict[str, dict]:
    """address -> registered values from the workspace state.

    Evidence-surface law (PR #149 discovery): the pr-reviewer HCP
    workspace runs in REMOTE execution mode — every `terraform plan`
    executes in HCP against the VCS-tracked configuration, and saved
    plans are banned outright ("Saved plans not allowed for workspaces
    with a VCS connection"). Local planning of a working tree is
    therefore impossible, and the only provider-resolved compiled
    surface available is the REGISTERED STATE, read via
    `terraform show -json`. It proves what AWS enforces from this
    workspace today; config-side truth stays with the source-level
    pins (tests/contracts/test_viewer_infra.py +
    test_terraform_multi_agent.py), and the state lags config until the
    next tf-tag apply — undeployed resources skip with an explicit
    reason rather than fake evidence.
    """
    proc = subprocess.run(  # noqa: S603
        [TERRAFORM_BIN, f"-chdir={TERRAFORM_DIR}", "show", "-json"],
        check=True,
        capture_output=True,
        timeout=300,
    )
    state = json.loads(proc.stdout)

    def walk(module: dict, found: dict) -> None:
        for resource in module.get("resources", []):
            found[resource["address"]] = resource.get("values", {})
        for child in module.get("child_modules", []):
            walk(child, found)

    found: dict[str, dict] = {}
    walk(state.get("values", {}).get("root_module", {}), found)
    return found


@pytest.fixture(scope="module")
def registered() -> dict[str, dict]:
    return _registered_resources()


def test_viewer_function_url_permissions_compile(registered):
    """The typed permission arguments resolve to the intended hardening
    posture as REGISTERED (provider-version drift catcher)."""
    grant = registered.get("aws_lambda_permission.viewer_function_url")
    if grant is None:
        pytest.skip(
            "viewer URL grant not yet registered in workspace state — "
            "evidence run deferred to the next tf-tag apply"
        )
    assert grant.get("action") == "lambda:InvokeFunctionUrl"
    assert grant.get("principal") == "*"
    assert grant.get("function_url_auth_type") == "NONE"

    invoke_grant = registered.get("aws_lambda_permission.viewer_invoked_via_function_url")
    assert invoke_grant is not None, "state lacks the InvokedViaFunctionUrl companion grant"
    assert invoke_grant.get("action") == "lambda:InvokeFunction"
    assert invoke_grant.get("principal") == "*"
    assert invoke_grant.get("invoked_via_function_url") is True


def test_viewer_role_policy_compiles_without_kms_decrypt(registered):
    """The registered policy document carries exactly the ruled read
    surface — and the superseded `kms:Decrypt` clause is absent from
    what the workspace has actually registered, not just from source."""
    policy_resource = registered.get("aws_iam_role_policy.viewer")
    if policy_resource is None:
        pytest.skip(
            "viewer policy not yet registered in workspace state (T052 IAM "
            "ships at the next tf-tag apply) — evidence run deferred"
        )
    raw = policy_resource.get("policy")
    assert isinstance(raw, str), "registered policy did not resolve to a JSON document"
    document = json.loads(raw)
    actions = {
        action
        for statement in document["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert {"s3:GetObject", "dynamodb:Query", "ssm:GetParameter"} <= actions, (
        f"registered policy missing ruled read actions: {sorted(actions)}"
    )
    assert "kms:Decrypt" not in actions, (
        "superseded kms:Decrypt clause present in the registered policy"
    )
