#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TASKS_PATHS = {
    "001": Path("specs/001-pr-reviewer/tasks.md"),
    "004": Path("specs/004-multi-agent-review/tasks.md"),
}
FIELD_NAME = "Spec Task ID"
TASK_RE = re.compile(r"^- \[[ xX]\] (T\d+[a-z]?)(?: \[P\])?(?: \[US(\d+)\])? (.+)$")


def tid_num(tid: str) -> int:
    """Numeric part of a T-id; tolerates letter suffixes (T010a -> 10)."""
    m = re.match(r"\d+", tid[1:])
    if not m:
        sys.exit(f"unparsable T-id {tid!r}: expected T<digits>[letter suffix]")
    return int(m.group())


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"missing env {name}")
    return v.rstrip("/") if name == "JIRA_BASE_URL" else v


def truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}


class Jira:
    def __init__(self, base: str, email: str, token: str) -> None:
        self.base = base
        raw = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {raw}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def req(self, method: str, path: str, body=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        if not url.startswith("https://"):
            raise ValueError("refusing non-https Jira URL")
        data = None if body is None else json.dumps(body).encode()
        # S310: same https guard as above; req is only ever opened via urlopen below.
        req = urllib.request.Request(  # noqa: S310
            url, data=data, headers=self.headers, method=method
        )
        try:
            # S310: scheme is enforced above (refuses non-https); Jira base is operator config.
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")
            raise SystemExit(f"Jira {method} {path} -> {e.code}: {err}") from e


def parse_tasks(text: str, scope: str) -> list[dict]:
    parsed = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = TASK_RE.match(line.strip())
        if not m:
            continue
        tid, us, rest = m.group(1), m.group(2), m.group(3).strip()
        if tid in seen:
            sys.exit(f"duplicate {tid}")
        seen.add(tid)
        us_n = int(us) if us else None
        if " \u2014 verify:" in rest:
            summary, verify = rest.split(" \u2014 verify:", 1)
        elif " -- verify:" in rest:
            summary, verify = rest.split(" -- verify:", 1)
        else:
            summary, verify = rest, ""
        parsed.append(
            {
                "id": tid,
                "us": us_n,
                "summary": summary.strip()[:240],
                "verify": verify.strip(),
                "full": rest,
            }
        )
    us2_min = min(
        (tid_num(t["id"]) for t in parsed if t["us"] is not None and t["us"] >= 2),
        default=None,
    )
    out = []
    for t in parsed:
        n = tid_num(t["id"])
        if scope == "mvp":
            if t["us"] is not None and t["us"] != 1:
                continue
            if t["us"] is None and us2_min is not None and n >= us2_min:
                continue
        elif scope.startswith("us") and scope[2:].isdigit():
            if t["us"] != int(scope[2:]):
                continue
        out.append(t)
    if not out:
        sys.exit("no tasks parsed from tasks.md")
    return out


def adf(paragraphs: list[str]) -> dict:
    content = []
    for p in paragraphs:
        p = p.replace("\x00", "").strip()
        if not p:
            continue
        content.append({"type": "paragraph", "content": [{"type": "text", "text": p[:4000]}]})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": "."}]}]
    return {"type": "doc", "version": 1, "content": content}


def main() -> None:
    base = env("JIRA_BASE_URL")
    if not base.startswith("https://"):
        sys.exit("JIRA_BASE_URL must start with https://")
    key = env("JIRA_PROJECT_KEY")
    email = env("JIRA_EMAIL")
    token = env("JIRA_API_TOKEN")
    dry = truthy(os.environ.get("DRY_RUN", "true"))
    spec = (os.environ.get("SPEC") or "001").strip()
    if spec not in TASKS_PATHS:
        sys.exit(f"SPEC must be one of: {', '.join(TASKS_PATHS)}")
    tasks_path = TASKS_PATHS[spec]
    scope = (os.environ.get("SCOPE") or "mvp").strip().lower()
    if scope not in {"mvp", "all", "us2", "us3", "us4", "us5"}:
        sys.exit("SCOPE must be one of: mvp, us2, us3, us4, us5, all")
    if spec == "004" and scope != "all":
        sys.exit("spec 004 has no [USn] scopes; SCOPE must be 'all'")
    if not tasks_path.is_file():
        sys.exit(f"missing {tasks_path}")

    tasks = parse_tasks(tasks_path.read_text(encoding="utf-8"), scope)
    repo = os.environ.get("GITHUB_REPOSITORY", "CarreroMarcos/pr-reviewer")
    sha = os.environ.get("GITHUB_SHA", "main")
    spec_url = f"https://github.com/{repo}/blob/{sha}/{tasks_path}"
    print(f"scope={scope} dry_run={dry} tasks={len(tasks)} project={key}")

    jira = Jira(base, email, token)
    fields = jira.req("GET", "/rest/api/3/field")
    field_id = next((f["id"] for f in fields if f.get("name") == FIELD_NAME), None)
    if not field_id:
        sys.exit(f"custom field {FIELD_NAME!r} not found")

    project = jira.req("GET", f"/rest/api/3/project/{urllib.parse.quote(key)}")
    types = jira.req("GET", "/rest/api/3/issuetype/project", query={"projectId": project["id"]})
    type_name = next(
        (
            n
            for n in ("Story", "Task")
            if any(t.get("name") == n and not t.get("subtask") for t in types)
        ),
        None,
    )
    if not type_name:
        sys.exit(f"no Story/Task type in {key}")

    created = updated = listed = 0
    fid_num = field_id.replace("customfield_", "")
    for t in tasks:
        jql = f'project = "{key}" AND cf[{fid_num}] ~ "{t["id"]}"'
        try:
            search = jira.req(
                "POST",
                "/rest/api/3/search/jql",
                body={"jql": jql, "maxResults": 5, "fields": ["key", "summary"]},
            )
        except SystemExit:
            search = jira.req(
                "GET",
                "/rest/api/3/search",
                query={"jql": jql, "fields": "key,summary", "maxResults": "5"},
            )
        hits = search.get("issues") or []
        if len(hits) > 1:
            sys.exit(f"duplicate Jira rows for {t['id']}: {[i['key'] for i in hits]}")
        labels = ["spec-sync", t["id"].lower()]
        if t["us"] is not None:
            labels.append(f"us{t['us']}")
        if scope == "mvp":
            labels.append("mvp")
        desc = adf(
            [
                f"Spec task: {t['id']}",
                f"Source: {spec_url}",
                t["full"],
                "Do not edit ACs here. Change git spec, then re-sync.",
                "Agents may comment and transition Ready / In progress / "
                "In review only. Never Done. Never create tickets.",
            ]
        )
        payload_fields = {
            "summary": f"{t['id']}: {t['summary']}"[:255],
            "description": desc,
            field_id: t["id"],
            "labels": labels,
        }
        if dry:
            action = "UPDATE" if hits else "CREATE"
            summary = payload_fields["summary"][:90]
            print(f"DRY {action} {t['id']} {summary}")
            listed += 1
            continue
        if not hits:
            created_issue = jira.req(
                "POST",
                "/rest/api/3/issue",
                body={
                    "fields": {
                        **payload_fields,
                        "project": {"key": key},
                        "issuetype": {"name": type_name},
                    }
                },
            )
            print(f"CREATE {t['id']} -> {created_issue.get('key')}")
            created += 1
        else:
            ikey = hits[0]["key"]
            jira.req(
                "PUT",
                f"/rest/api/3/issue/{ikey}",
                body={"fields": payload_fields},
            )
            print(f"UPDATE {t['id']} -> {ikey}")
            updated += 1
        time.sleep(0.35)
    print(f"done created={created} updated={updated} dry_listed={listed}")


if __name__ == "__main__":
    main()
