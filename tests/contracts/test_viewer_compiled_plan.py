"""T052: compiled-plan pin for the viewer Function URL hardening.

Closes the Gate-38 deferred finding on PR #144 (bot-concurred, owner
T052): the source-text pins in tests/contracts/test_viewer_infra.py
prove the .tf uses the provider-native typed arguments
(`function_url_auth_type` / `invoked_via_function_url`), but a source
pin cannot catch provider-version drift where the spelling passes and
the compiled posture changes. This module renders the real plan and
asserts the resolved values — including the compiled viewer policy
document, where the Mars-ruled absence of `kms:Decrypt` is checked
against what terraform will actually register (DECISIONS 2026-09-28).

Evidence-class test (mars-law: terraform and full pytest run in
separate shells). It skips — never fails — outside its evidence
environment:

* no terraform binary, or no AWS creds for the plan's data sources
  (`eval "$(aws configure export-credentials --format env)"` first);
* the viewer handler is still the T051b stub — the Lambda's plan-time
  precondition (intentionally) hard-fails every plan until T054 lands
  the real handler, so this pin arms itself at T054.

Evidence run:

    eval "$(aws configure export-credentials --format env)"
    uv run --frozen pytest tests/contracts/test_viewer_compiled_plan.py -v
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
HANDLER = TERRAFORM_DIR.parent / "lambda" / "viewer_handler.py"


def _skip_reason() -> str | None:
    if shutil.which("terraform") is None:
        return "terraform binary not on PATH"
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        return "AWS creds not exported — terraform plan reads data sources"
    if HANDLER.exists() and "NotImplementedError" in HANDLER.read_text(encoding="utf-8"):
        return (
            "viewer_handler.py is still the T051b stub — the plan-time "
            "precondition blocks every plan; this pin arms at T054"
        )
    return None


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")

# Full path satisfies S607; every argument is repo-constructed (no
# untrusted input), so the S603 bandit check is suppressed at the call
# sites below.
TERRAFORM_BIN = shutil.which("terraform")


def _compiled_plan() -> dict:
    """Full-stack `terraform plan` rendered through `show -json`."""
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = os.path.join(tmp, "plan.bin")
        subprocess.run(  # noqa: S603
            [
                TERRAFORM_BIN,
                f"-chdir={TERRAFORM_DIR}",
                "init",
                "-backend=false",
                "-input=false",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        subprocess.run(  # noqa: S603
            [
                TERRAFORM_BIN,
                f"-chdir={TERRAFORM_DIR}",
                "plan",
                "-refresh=false",
                "-input=false",
                "-lock=false",
                "-no-color",
                f"-out={plan_path}",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
        shown = subprocess.run(  # noqa: S603
            [TERRAFORM_BIN, f"-chdir={TERRAFORM_DIR}", "show", "-json", plan_path],
            check=True,
            capture_output=True,
            timeout=300,
        )
    return json.loads(shown.stdout)


def _walk_resources(module: dict, found: dict) -> None:
    for resource in module.get("resources", []):
        found[resource["address"]] = resource.get("values", {})
    for child in module.get("child_modules", []):
        _walk_resources(child, found)


@pytest.fixture(scope="module")
def compiled_resources() -> dict[str, dict]:
    plan = _compiled_plan()
    found: dict[str, dict] = {}
    _walk_resources(plan.get("planned_values", {}).get("root_module", {}), found)
    return found


def test_viewer_function_url_permissions_compile(compiled_resources):
    """The typed permission arguments resolve to the intended hardening
    posture in the compiled plan (provider-version drift catcher)."""
    url_grant = compiled_resources.get("aws_lambda_permission.viewer_function_url")
    assert url_grant is not None, "compiled plan lacks the URL-invocation grant"
    assert url_grant.get("action") == "lambda:InvokeFunctionUrl"
    assert url_grant.get("principal") == "*"
    assert url_grant.get("function_url_auth_type") == "NONE"

    invoke_grant = compiled_resources.get("aws_lambda_permission.viewer_invoked_via_function_url")
    assert invoke_grant is not None, "compiled plan lacks the InvokedViaFunctionUrl companion grant"
    assert invoke_grant.get("action") == "lambda:InvokeFunction"
    assert invoke_grant.get("principal") == "*"
    assert invoke_grant.get("invoked_via_function_url") is True


def test_viewer_role_policy_compiles_without_kms_decrypt(compiled_resources):
    """The jsonencode'd policy document resolves with exactly the ruled
    read surface — and the superseded kms:Decrypt clause is absent from
    what terraform will actually register, not just from the source."""
    policy_resource = compiled_resources.get("aws_iam_role_policy.viewer")
    assert policy_resource is not None, "compiled plan lacks aws_iam_role_policy.viewer"
    raw = policy_resource.get("policy")
    assert isinstance(raw, str), "policy did not resolve to a JSON document"
    document = json.loads(raw)
    actions = {
        action
        for statement in document["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert {"s3:GetObject", "dynamodb:Query", "ssm:GetParameter"} <= actions, (
        f"compiled policy missing ruled read actions: {sorted(actions)}"
    )
    assert "kms:Decrypt" not in actions, (
        "superseded kms:Decrypt clause present in the compiled policy"
    )
