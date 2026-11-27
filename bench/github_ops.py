"""GitHub repository provisioner and review collector for isolated pilot/base runs.

Supports:
1. Discovering GitHub credentials from `GITHUB_TOKEN` or `git credential fill` (zero extra login if git push works).
2. Creating isolated private target repositories (`Swizzle-AI/arb-target-<provider>`) seeded strictly with `pilot/base`
   (`README.md`, `console.py`, `reports.py`, `migrations/001_initial.sql`).
3. Opening reproducible pull requests for `code_only` (`01..10`), `with_snapshot` (`04..06`), and `repeat` (`r=2`)
   with `--resume` interrupted-run recovery backed by `bench.sqlite`.
4. Collecting PR reviews, inline review comments, issue comments, and check-run summaries into `results/raw/`
   with SHA-256 content hashes and webhook deduplication.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

from bench.cases import ROOT, CaseSpec, apply_unified_patch, load_cases, materialize_app
from bench.ledger import (
    connect_ledger,
    create_or_update_claim,
    ingest_raw_comment,
    make_attempt_id,
    upsert_attempt,
)


PILOT_BASE_SHA = "9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a"


def resolve_github_token() -> str:
    """Resolve a GitHub token from GITHUB_TOKEN, GH_TOKEN, .git/config remote, or non-interactive git credential fill."""
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    git_cfg = ROOT / ".git" / "config"
    if git_cfg.exists():
        import re
        m = re.search(r"https://x-access-token:([^@\s]+)@github\.com", git_cfg.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1).strip()
    env = os.environ.copy()
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", "-c", "credential.interactive=never", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=5,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("No GitHub token found in GITHUB_TOKEN, .git/config, or git credential manager.")


def github_api(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = path if path.startswith("https://") else f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "proofline-ai-review-benchmarks-harness/1.0",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed ({err.code}): {detail}") from err


def ensure_private_repo(org: str, repo_name: str, token: str, description: str) -> dict[str, Any]:
    try:
        repo_info = github_api("GET", f"/repos/{org}/{repo_name}", token)
        if not repo_info.get("private", False):
            raise RuntimeError(f"Refusing to provision benchmark target into non-private repository {org}/{repo_name}.")
        return repo_info
    except RuntimeError as err:
        if "(404)" not in str(err):
            raise
    me = github_api("GET", "/user", token).get("login", "")
    endpoint = "/user/repos" if me.lower() == org.lower() else f"/orgs/{org}/repos"
    return github_api(
        "POST",
        endpoint,
        token,
        {
            "name": repo_name,
            "private": True,
            "description": description,
            "has_issues": True,
            "has_projects": False,
            "has_wiki": False,
            "auto_init": False,
        },
    )


def build_pr_body(spec: CaseSpec, condition: str) -> str:
    if condition == "with_snapshot" and spec.evidence_md:
        return f"{spec.body}\n\n## Deployment Snapshot\n\n{spec.evidence_md}\n"
    return f"{spec.body}\n"


def provision_provider_batch(
    *,
    db_path: Path,
    org: str,
    provider_id: str,
    repo_name: str | None = None,
    conditions: list[str] | None = None,
    case_filter: list[str] | None = None,
    repeat_index: int = 1,
    resume: bool = True,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Create isolated pilot/base repo and open PRs for the requested cases/conditions."""
    conn = connect_ledger(db_path)
    target_repo = repo_name or f"arb-target-{provider_id}"
    full_repo = f"{org}/{target_repo}"
    conds = conditions or ["code_only", "with_snapshot"]
    supported_conditions = {"code_only", "with_snapshot"}
    invalid_conds = [c for c in conds if c not in supported_conditions]
    if invalid_conds:
        conn.close()
        raise ValueError(f"Unsupported condition(s) {invalid_conds}; allowed: {sorted(supported_conditions)}")
    cases = load_cases(include_held_out=True)
    selected_cases = [c for c in sorted(cases.keys()) if (not case_filter or c in case_filter)]

    planned: list[tuple[CaseSpec, str]] = []
    for cid in selected_cases:
        spec = cases[cid]
        for cond in conds:
            if cond == "with_snapshot" and spec.expected_with_snapshot is None:
                continue
            aid = make_attempt_id(provider_id, cid, cond, repeat_index)
            if resume:
                row = conn.execute("SELECT pr_number, terminal_status FROM attempts WHERE attempt_id = ?", (aid,)).fetchone()
                if row and row["pr_number"] is not None and row["terminal_status"] != "unattempted":
                    print(f"[resume] Skipping already-provisioned {aid} (PR #{row['pr_number']}, status={row['terminal_status']})")
                    continue
            planned.append((spec, cond))

    if dry_run:
        conn.close()
        return [
            {
                "attempt_id": make_attempt_id(provider_id, s.case_id, cond, repeat_index),
                "repo": full_repo,
                "case_id": s.case_id,
                "condition": cond,
                "title": s.title,
                "dry_run": True,
            }
            for s, cond in planned
        ]

    token = resolve_github_token()
    ensure_private_repo(
        org,
        target_repo,
        token,
        f"Isolated pilot/base target repository for AI Review Benchmarks ({provider_id})",
    )

    provisioned: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        work.mkdir(parents=True, exist_ok=True)
        remote_url = f"https://x-access-token:{token}@github.com/{full_repo}.git"

        def run_git(*gargs: str) -> str:
            env = os.environ.copy()
            env["GIT_AUTHOR_NAME"] = "Report Service"
            env["GIT_AUTHOR_EMAIL"] = "app@example.invalid"
            env["GIT_COMMITTER_NAME"] = "Report Service"
            env["GIT_COMMITTER_EMAIL"] = "app@example.invalid"
            env["GIT_AUTHOR_DATE"] = "2026-11-01T12:00:00Z"
            env["GIT_COMMITTER_DATE"] = "2026-11-01T12:00:00Z"
            res = subprocess.run(
                ["git", "-C", str(work), *gargs],
                check=True,
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            return res.stdout.strip()

        # Materialize clean pilot/base (ONLY README.md, console.py, reports.py, migrations/001_initial.sql)
        materialize_app(work, case_id=None)
        (work / "README.md").write_text((ROOT / "docs" / "report-service.md").read_text(encoding="utf-8"), encoding="utf-8")
        run_git("init", "-b", "main")
        run_git("remote", "add", "origin", remote_url)
        run_git("add", "README.md", "console.py", "reports.py", "migrations/001_initial.sql")
        run_git("commit", "-m", "Add the scheduled report service and console")
        base_sha = run_git("rev-parse", "HEAD")
        run_git("push", "-u", "origin", "main", "--force")

        for spec, cond in planned:
            branch = f"case-{spec.case_id}-{cond}-r{repeat_index}"
            run_git(" checkout", "-B", branch, base_sha) if False else run_git("checkout", "-B", branch, base_sha)
            # Clean any untracked migration from previous case
            for f in (work / "migrations").glob("002_*.sql"):
                f.unlink()
            apply_unified_patch(work, spec.patch_text)
            if cond == "with_snapshot" and spec.evidence_md:
                snap_file = work / "DEPLOYMENT_SNAPSHOT.md"
                snap_file.write_text(spec.evidence_md + "\n", encoding="utf-8")
            else:
                snap_file = work / "DEPLOYMENT_SNAPSHOT.md"
                if snap_file.exists():
                    snap_file.unlink()
            run_git("add", "-A")
            run_git("commit", "-m", spec.title)
            head_sha = run_git("rev-parse", "HEAD")
            run_git("push", "-u", "origin", branch, "--force")

            pr_title = spec.title if cond == "code_only" else f"{spec.title} [with deployment snapshot]"
            pr_body = build_pr_body(spec, cond)
            existing_prs = github_api("GET", f"/repos/{full_repo}/pulls?state=open&head={org}:{branch}", token)
            if existing_prs:
                pr = existing_prs[0]
            else:
                pr = github_api(
                    "POST",
                    f"/repos/{full_repo}/pulls",
                    token,
                    {
                        "title": pr_title,
                        "head": branch,
                        "base": "main",
                        "body": pr_body,
                    },
                )
            trigger_used = "auto_on_pr_open"
            if provider_id == "copilot" and pr.get("node_id"):
                mutation = """
                mutation($prId: ID!, $botIds: [ID!]) {
                  requestReviews(input: { pullRequestId: $prId, botIds: $botIds }) {
                    pullRequest { id }
                  }
                }
                """
                github_api(
                    "POST",
                    "/graphql",
                    token,
                    {"query": mutation, "variables": {"prId": pr["node_id"], "botIds": ["BOT_kgDOCnlnWA"]}},
                )
                trigger_used = "graphql_requestReviews_BOT_kgDOCnlnWA"
            now_iso = datetime.now(timezone.utc).isoformat()
            aid = upsert_attempt(
                conn,
                provider_id=provider_id,
                case_id=spec.case_id,
                condition=cond,
                repeat_index=repeat_index,
                repo_full_name=full_repo,
                pr_number=pr["number"],
                base_sha=base_sha,
                head_sha=head_sha,
                trigger_kind=trigger_used,
                terminal_status="pending_review",
                opened_at=pr.get("created_at", now_iso),
                notes=pr.get("html_url"),
            )
            provisioned.append({"attempt_id": aid, "pr_number": pr["number"], "html_url": pr.get("html_url")})
            time.sleep(1.0)

    conn.close()
    return provisioned


PROVIDER_TRIGGER_COMMENTS: dict[str, str] = {
    "coderabbit": "@coderabbitai review",
    "gemini-code-assist": "/gemini review",
    "sourcery": "@sourcery-ai review",
    "greptile": "@greptileai",
}

PROVIDER_BOT_TOKENS: dict[str, tuple[str, ...]] = {
    "proofline": ("proofline",),
    "coderabbit": ("coderabbit",),
    "gemini-code-assist": ("gemini-code-assist", "gemini"),
    "copilot": ("copilot-pull-request-reviewer", "copilot"),
    "sourcery": ("sourcery",),
    "greptile": ("greptile",),
}


def matches_provider_actor(provider_id: str, login_or_slug: str) -> bool:
    actor = (login_or_slug or "").lower()
    tokens = PROVIDER_BOT_TOKENS.get(provider_id)
    if not tokens:
        return False
    return any(tok in actor for tok in tokens)


def trigger_provider_reviews(
    *,
    db_path: Path,
    provider_id: str,
    case_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Explicitly trigger or re-trigger reviews on already-open PRs via bot command or GraphQL."""
    token = resolve_github_token()
    conn = connect_ledger(db_path)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM attempts WHERE provider_id = ? AND pr_number IS NOT NULL ORDER BY case_id, condition",
            (provider_id,),
        ).fetchall()
    ]
    triggered: list[dict[str, Any]] = []
    for att in rows:
        if case_filter and att["case_id"] not in case_filter:
            continue
        repo = att["repo_full_name"]
        pr_num = att["pr_number"]
        if provider_id == "copilot":
            pr = github_api("GET", f"/repos/{repo}/pulls/{pr_num}", token)
            mutation = """
            mutation($prId: ID!, $botIds: [ID!]) {
              requestReviews(input: { pullRequestId: $prId, botIds: $botIds }) {
                pullRequest { id }
              }
            }
            """
            github_api("POST", "/graphql", token, {"query": mutation, "variables": {"prId": pr["node_id"], "botIds": ["BOT_kgDOCnlnWA"]}})
            triggered.append({"attempt_id": att["attempt_id"], "pr_number": pr_num, "trigger": "graphql_bot_request"})
        elif provider_id in PROVIDER_TRIGGER_COMMENTS:
            cmd_body = PROVIDER_TRIGGER_COMMENTS[provider_id]
            ic = github_api("POST", f"/repos/{repo}/issues/{pr_num}/comments", token, {"body": cmd_body})
            triggered.append({"attempt_id": att["attempt_id"], "pr_number": pr_num, "trigger": cmd_body, "comment_id": ic.get("id")})
        else:
            # Close and reopen PR to fire pull_request.reopened / synchronize webhook for GitHub Apps like Proofline
            github_api("PATCH", f"/repos/{repo}/pulls/{pr_num}", token, {"state": "closed"})
            try:
                time.sleep(1.0)
            finally:
                github_api("PATCH", f"/repos/{repo}/pulls/{pr_num}", token, {"state": "open"})
            triggered.append({"attempt_id": att["attempt_id"], "pr_number": pr_num, "trigger": "pr_reopen_webhook"})
        time.sleep(1.0)
    conn.close()
    return triggered


def collect_provider_reviews(
    *,
    db_path: Path,
    provider_id: str | None = None,
    ignore_authors: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect reviews, inline comments, issue comments, and check-run summaries for provisioned PRs."""
    token = resolve_github_token()
    conn = connect_ledger(db_path)
    skip_logins = {a.lower() for a in (ignore_authors or {"paramveersingh-s"})}

    query = "SELECT * FROM attempts WHERE pr_number IS NOT NULL"
    params: list[Any] = []
    if provider_id:
        query += " AND provider_id = ?"
        params.append(provider_id)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    collected_summary: list[dict[str, Any]] = []
    raw_dir = ROOT / "results" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for att in rows:
        aid = att["attempt_id"]
        repo = att["repo_full_name"]
        pr_num = att["pr_number"]
        head_sha = att["head_sha"]

        reviews = github_api("GET", f"/repos/{repo}/pulls/{pr_num}/reviews", token)
        review_comments = github_api("GET", f"/repos/{repo}/pulls/{pr_num}/comments", token)
        issue_comments = github_api("GET", f"/repos/{repo}/issues/{pr_num}/comments", token)
        check_runs_resp = github_api("GET", f"/repos/{repo}/commits/{head_sha}/check-runs", token)
        check_runs = check_runs_resp.get("check_runs", []) if isinstance(check_runs_resp, dict) else []

        proofline_cli_result = None
        import shutil as _shutil
        env_pl = os.environ.get("PROOFLINE_BIN", "").strip()
        which_pl = _shutil.which("proofline")
        proofline_exe = Path(env_pl) if env_pl else (Path(which_pl) if which_pl else Path(r"D:\Projects\OSS\proofline-cli\proofline.exe"))
        if att["provider_id"] == "proofline" and proofline_exe.exists():
            pl_proc = subprocess.run(
                [str(proofline_exe), "findings", "--repo", repo, "--pr", str(pr_num), "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            raw_pl = (pl_proc.stdout or "").strip()
            if raw_pl.startswith("{"):
                try:
                    proofline_cli_result = json.loads(raw_pl)
                except json.JSONDecodeError:
                    proofline_cli_result = None

        bundle = {
            "attempt_id": aid,
            "repo_full_name": repo,
            "pr_number": pr_num,
            "base_sha": att["base_sha"],
            "head_sha": head_sha,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "reviews": reviews,
            "review_comments": review_comments,
            "issue_comments": issue_comments,
            "check_runs": check_runs,
            "proofline_cli_result": proofline_cli_result,
        }
        raw_bytes = json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        safe_name = aid.replace(":", "_") + ".json"
        raw_path = raw_dir / safe_name
        raw_path.write_bytes(raw_bytes)

        timestamps: list[str] = []
        new_comments = 0
        quota_hit = False

        QUOTA_EXHAUSTED_PHRASES = (
            "rate limit exceeded",
            "insufficient remaining quota",
            "quota exceeded",
            "trial expired",
            "credits exhausted",
            "no credits remain",
            "upgrade your plan",
        )

        # Ingest top-level PR reviews
        for rev in reviews:
            login = (rev.get("user") or {}).get("login", "unknown")
            if login.lower() in skip_logins or not matches_provider_actor(att["provider_id"], login):
                continue
            body = (rev.get("body") or "").strip()
            ts = rev.get("submitted_at") or datetime.now(timezone.utc).isoformat()
            timestamps.append(ts)
            if not body:
                continue
            if any(q in body.lower() for q in QUOTA_EXHAUSTED_PHRASES):
                quota_hit = True
            cuid = f"review:{aid}:{rev['id']}"
            if ingest_raw_comment(
                conn,
                comment_uid=cuid,
                attempt_id=aid,
                source_kind="pull_request_review",
                author_login=login,
                created_at=ts,
                body=body,
                html_url=rev.get("html_url"),
            ):
                new_comments += 1
                create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=body)

        # Ingest inline diff review comments
        for rc in review_comments:
            login = (rc.get("user") or {}).get("login", "unknown")
            if login.lower() in skip_logins or not matches_provider_actor(att["provider_id"], login):
                continue
            body = (rc.get("body") or "").strip()
            ts = rc.get("created_at") or datetime.now(timezone.utc).isoformat()
            timestamps.append(ts)
            if not body:
                continue
            cuid = f"inline:{aid}:{rc['id']}"
            if ingest_raw_comment(
                conn,
                comment_uid=cuid,
                attempt_id=aid,
                source_kind="inline_review_comment",
                author_login=login,
                created_at=ts,
                body=body,
                path=rc.get("path"),
                line=rc.get("line") or rc.get("original_line"),
                html_url=rc.get("html_url"),
            ):
                new_comments += 1
                create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=body)

        # Ingest PR issue comments
        for ic in issue_comments:
            login = (ic.get("user") or {}).get("login", "unknown")
            if login.lower() in skip_logins or not matches_provider_actor(att["provider_id"], login):
                continue
            body = (ic.get("body") or "").strip()
            ts = ic.get("created_at") or datetime.now(timezone.utc).isoformat()
            timestamps.append(ts)
            if not body:
                continue
            if any(q in body.lower() for q in QUOTA_EXHAUSTED_PHRASES):
                quota_hit = True
            cuid = f"issue:{aid}:{ic['id']}"
            if ingest_raw_comment(
                conn,
                comment_uid=cuid,
                attempt_id=aid,
                source_kind="issue_comment",
                author_login=login,
                created_at=ts,
                body=body,
                html_url=ic.get("html_url"),
            ):
                new_comments += 1
                create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=body)

        # Ingest check-run summaries from non-GitHub-Actions reviewer apps
        for cr in check_runs:
            app_slug = ((cr.get("app") or {}).get("slug") or "").lower()
            if app_slug in ("github-actions", "") or not matches_provider_actor(att["provider_id"], app_slug):
                continue
            ts = cr.get("completed_at") or cr.get("started_at") or datetime.now(timezone.utc).isoformat()
            timestamps.append(ts)
            out = cr.get("output") or {}
            summary_text = "\n\n".join(filter(None, [out.get("title"), out.get("summary"), out.get("text")])).strip()
            if summary_text:
                if any(q in summary_text.lower() for q in QUOTA_EXHAUSTED_PHRASES):
                    quota_hit = True
                cuid = f"check:{aid}:{cr['id']}"
                if ingest_raw_comment(
                    conn,
                    comment_uid=cuid,
                    attempt_id=aid,
                    source_kind=f"check_run:{app_slug}",
                    author_login=app_slug,
                    created_at=ts,
                    body=summary_text,
                    html_url=cr.get("html_url"),
                ):
                    new_comments += 1
                    create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=summary_text)

        # Ingest structured Proofline CLI findings (`proofline.review-result.v1`) when available
        if isinstance(proofline_cli_result, dict) and att["provider_id"] == "proofline":
            rev_obj = proofline_cli_result.get("review") or {}
            ca = rev_obj.get("code_assessment") or {}
            ts = rev_obj.get("completed_at") or rev_obj.get("created_at") or datetime.now(timezone.utc).isoformat()
            timestamps.append(ts)
            for idx, fnd in enumerate(ca.get("findings") or []):
                fbody = json.dumps(fnd, ensure_ascii=False)
                cuid = f"proofline_finding:{aid}:{rev_obj.get('id','run')}:{idx}"
                if ingest_raw_comment(
                    conn,
                    comment_uid=cuid,
                    attempt_id=aid,
                    source_kind="proofline_cli_finding",
                    author_login="proofline",
                    created_at=ts,
                    body=fbody,
                    html_url=proofline_cli_result.get("review_url"),
                ):
                    new_comments += 1
                    create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=fbody)
            inc = rev_obj.get("incomplete_causes") or []
            if inc:
                ibody = f"Proofline CLI outcome={proofline_cli_result.get('outcome')} incomplete_causes={inc} code_assessment_status={ca.get('status')} incompleteness_kind={ca.get('incompleteness_kind')}"
                cuid = f"proofline_status:{aid}:{rev_obj.get('id','run')}"
                if ingest_raw_comment(
                    conn,
                    comment_uid=cuid,
                    attempt_id=aid,
                    source_kind="proofline_cli_status",
                    author_login="proofline",
                    created_at=ts,
                    body=ibody,
                    html_url=proofline_cli_result.get("review_url"),
                ):
                    new_comments += 1
                    create_or_update_claim(conn, claim_id=cuid, attempt_id=aid, comment_uid=cuid, raw_text=ibody)

        total_stored = conn.execute(
            "SELECT count(*) FROM raw_comments WHERE attempt_id = ? AND is_duplicate_delivery = 0",
            (aid,),
        ).fetchone()[0]
        if quota_hit:
            term_status = "quota_stopped"
        elif total_stored > 0:
            term_status = "completed"
        elif timestamps:
            term_status = "completed_quiet"
        else:
            term_status = att["terminal_status"]

        first_ts = min(timestamps) if timestamps else att.get("first_response_at")
        last_ts = max(timestamps) if timestamps else att.get("completed_at")
        upsert_attempt(
            conn,
            provider_id=att["provider_id"],
            case_id=att["case_id"],
            condition=att["condition"],
            repeat_index=att["repeat_index"],
            repo_full_name=repo,
            pr_number=pr_num,
            base_sha=att["base_sha"],
            head_sha=head_sha,
            trigger_kind=att["trigger_kind"],
            terminal_status=term_status,
            opened_at=att.get("opened_at"),
            first_response_at=first_ts,
            completed_at=last_ts,
            raw_payload_path=str(raw_path.relative_to(ROOT)),
            raw_payload_sha256=raw_sha256,
            notes=att.get("notes"),
        )
        collected_summary.append(
            {
                "attempt_id": aid,
                "pr_number": pr_num,
                "terminal_status": term_status,
                "unique_comments": total_stored,
                "new_comments": new_comments,
                "raw_sha256": raw_sha256[:16],
            }
        )

    conn.close()
    return collected_summary
