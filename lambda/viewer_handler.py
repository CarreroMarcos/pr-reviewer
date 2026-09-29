"""Viewer handler — placeholder (T051b infra shell).

T054 lands the real implementation (routing, hmac token check, S3/SSM
reads). This stub exists so the T051b archive_file source resolves and
`terraform validate` passes on the infra-only PR: a Lambda resource
requires a packaging source, and file() is evaluated eagerly at
validate. Nothing deploys until the T056 tag (never-apply law), so the
stub never serves traffic.
"""


def handler(event, context):
    raise NotImplementedError("viewer handler lands with T054")
