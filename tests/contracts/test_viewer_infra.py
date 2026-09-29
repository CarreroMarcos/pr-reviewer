"""Contract pins for the viewer delivery surface (T051a/T051b).

The replay site's viewer Lambda is the one internet-reachable surface in
the stack, so its Function URL posture is pinned exactly per the HLD §7
Oct-2025 hardening: NONE authorization (public static shell + token-
gated API by design) with BOTH resource-policy statements AWS requires
for a NONE-auth URL — the URL-invocation grant conditioned on
`lambda:FunctionUrlAuthType`, and the companion `lambda:InvokeFunction`
grant conditioned on `lambda:InvokedViaFunctionUrl = true` (without it,
callers routed through the plain invoke action are denied at the URL).

Creator half of the T051a/T051b pair: this file REDs against a missing
terraform/viewer.tf and GREENs only when T051b lands the exact surface.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
VIEWER_TF_PATH = TERRAFORM_DIR / "viewer.tf"


def _viewer_tf() -> str:
    assert VIEWER_TF_PATH.exists(), "terraform/viewer.tf not found — T051b not landed"
    return VIEWER_TF_PATH.read_text(encoding="utf-8")


def _resource_block(source: str, kind: str, name: str) -> str:
    # Terminator is a column-0 closing brace (MULTILINE ^}) — the fmt-
    # guaranteed block boundary. The no-bleed assert catches a lazy match
    # spanning into the next resource (bot review #1 on PR #144).
    m = re.search(
        rf'resource "{re.escape(kind)}" "{re.escape(name)}" \{{(.*?)^}}',
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, f"{kind}.{name} not found"
    block = m.group(1)
    assert "resource " not in block, f"{kind}.{name} block bled into the next resource"
    return block


def test_viewer_lambda_exists():
    """T051a: the viewer Lambda resource exists under its pinned name."""
    block = _resource_block(_viewer_tf(), "aws_lambda_function", "viewer")
    assert re.search(r'function_name\s*=\s*"pr-reviewer-viewer"', block), (
        "viewer Lambda not named pr-reviewer-viewer"
    )


def test_viewer_function_url_none_auth():
    """T051a: the Function URL exists with authorization_type NONE — the
    public static shell + token-gated API design (HLD §7)."""
    block = _resource_block(_viewer_tf(), "aws_lambda_function_url", "viewer")
    assert re.search(r'authorization_type\s*=\s*"NONE"', block), "Function URL auth is not NONE"


def test_viewer_function_url_permission():
    """T051a: the URL-invocation grant — action lambda:InvokeFunctionUrl,
    Principal *, auth-conditioned on FunctionUrlAuthType NONE (the
    Oct-2025 hardening: a NONE URL without this condition is a hole).
    The provider models the condition as the typed argument
    `function_url_auth_type`, which compiles to the resource-policy
    condition StringEquals lambda:FunctionUrlAuthType = NONE."""
    block = _resource_block(_viewer_tf(), "aws_lambda_permission", "viewer_function_url")
    assert re.search(r'action\s*=\s*"lambda:InvokeFunctionUrl"', block)
    assert re.search(r'principal\s*=\s*"\*"', block)
    assert re.search(r'function_url_auth_type\s*=\s*"NONE"', block)


def test_viewer_invoked_via_function_url_permission():
    """T051a: the companion grant — action lambda:InvokeFunction with the
    InvokedViaFunctionUrl = true condition (AWS requires BOTH statements
    for a NONE-auth Function URL); the provider-native argument form
    compiles to StringEquals lambda:InvokedViaFunctionUrl = true."""
    block = _resource_block(
        _viewer_tf(), "aws_lambda_permission", "viewer_invoked_via_function_url"
    )
    assert re.search(r'action\s*=\s*"lambda:InvokeFunction"', block)
    assert re.search(r'principal\s*=\s*"\*"', block)
    assert re.search(r"invoked_via_function_url\s*=\s*true", block)


def test_viewer_deploy_blocked_until_t054_handler():
    """T051b guard (bot review #1, PR #144): the placeholder must be
    mechanically un-deployable — the Lambda's plan-time precondition
    hard-fails any plan/apply while the packaged handler is the stub,
    so the T056 deploy tag cannot precede T054 by convention alone."""
    block = _resource_block(_viewer_tf(), "aws_lambda_function", "viewer")
    assert "precondition" in block, "no stub-deploy precondition on the viewer Lambda"
    assert re.search(r"strcontains\(\s*file\(", block), (
        "precondition does not inspect the packaged handler source"
    )
    assert "NotImplementedError" in block, "precondition does not gate on the placeholder marker"
