"""SQLite benchmark state ledger, webhook deduplicator, blind claim anonymizer, and scorer."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from bench.cases import CASE_METADATA, load_cases


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS providers (
    provider_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    integration_kind TEXT NOT NULL,
    plan_or_model TEXT NOT NULL,
    free_tier_notes TEXT NOT NULL,
    cross_pr_learning_control TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    case_id TEXT NOT NULL,
    condition TEXT NOT NULL,
    repeat_index INTEGER NOT NULL DEFAULT 1,
    repo_full_name TEXT NOT NULL,
    pr_number INTEGER,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    opened_at TEXT,
    first_response_at TEXT,
    completed_at TEXT,
    terminal_status TEXT NOT NULL,
    raw_payload_path TEXT,
    raw_payload_sha256 TEXT,
    notes TEXT,
    UNIQUE(provider_id, case_id, condition, repeat_index)
);

CREATE TABLE IF NOT EXISTS raw_comments (
    comment_uid TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    source_kind TEXT NOT NULL,
    author_login TEXT NOT NULL,
    created_at TEXT NOT NULL,
    path TEXT,
    line INTEGER,
    body TEXT NOT NULL,
    body_norm_sha256 TEXT NOT NULL,
    html_url TEXT,
    is_duplicate_delivery INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    blind_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    comment_uid TEXT REFERENCES raw_comments(comment_uid),
    anonymized_text TEXT NOT NULL,
    classification TEXT,
     cites_env_evidence INTEGER NOT NULL DEFAULT 0,
    identifies_affected_site INTEGER NOT NULL DEFAULT 0,
    verification_test TEXT,
    grading_rationale TEXT,
    graded_by TEXT
);

CREATE TABLE IF NOT EXISTS deployment_traces (
    trace_id TEXT PRIMARY KEY,
    environment_kind TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    workflow_run_url TEXT,
    stack_name TEXT NOT NULL,
    request_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    notes TEXT
);
"""


BOT_PATTERNS = [
    r"(?i)\bproofline(?:dev|-ai|-bot)?\b",
    r"(?i)\bcoderabbit(?:ai)?\b",
    r"(?i)\bgemini-code-assist(?:\[bot\])?\b",
    r"(?i)\bcopilot(?:-pull-request-reviewer|\[bot\])?\b",
    r"(?i)\bgreptile(?:-apps|\[bot\])?\b",
    r"(?i)\bsourcery(?:-ai|\[bot\])?\b",
    r"(?i)\bcodacy(?:-production|\[bot\])?\b",
    r"(?i)<!--.*?-->",
]


def normalize_comment_body(body: str) -> str:
    collapsed = re.sub(r"\s+", " ", body.strip())
    return collapsed


def anonymize_text(body: str) -> str:
    cleaned = body
    for pat in BOT_PATTERNS:
        cleaned = re.sub(pat, "[REVIEWER]", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"https://github\.com/[^\s)]+", "[GITHUB_LINK]", cleaned)
    cleaned = re.sub(r"https://proofline\.sh/[^\s)]+", "[CONSOLE_LINK]", cleaned)
    return cleaned.strip()


def connect_ledger(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.executescript(SCHEMA_SQL)
    return conn


def make_attempt_id(provider_id: str, case_id: str, condition: str, repeat_index: int = 1) -> str:
    return f"{provider_id}:{case_id}:{condition}:r{repeat_index}"


def register_provider(
    conn: sqlite3.Connection,
    provider_id: str,
    display_name: str,
    integration_kind: str,
    plan_or_model: str,
    free_tier_notes: str,
    cross_pr_learning_control: str,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO providers (
                provider_id, display_name, integration_kind, plan_or_model,
                free_tier_notes, cross_pr_learning_control
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                display_name=excluded.display_name,
                integration_kind=excluded.integration_kind,
                plan_or_model=excluded.plan_or_model,
                free_tier_notes=excluded.free_tier_notes,
                cross_pr_learning_control=excluded.cross_pr_learning_control
            """,
            (
                provider_id,
                display_name,
                integration_kind,
                plan_or_model,
                free_tier_notes,
                cross_pr_learning_control,
            ),
        )


def upsert_attempt(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    case_id: str,
    condition: str,
    repeat_index: int,
    repo_full_name: str,
    pr_number: int | None,
    base_sha: str,
    head_sha: str,
    trigger_kind: str,
    terminal_status: str,
    opened_at: str | None = None,
    first_response_at: str | None = None,
    completed_at: str | None = None,
    raw_payload_path: str | None = None,
    raw_payload_sha256: str | None = None,
    notes: str | None = None,
) -> str:
    attempt_id = make_attempt_id(provider_id, case_id, condition, repeat_index)
    with conn:
        conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, provider_id, case_id, condition, repeat_index,
                repo_full_name, pr_number, base_sha, head_sha, trigger_kind,
                opened_at, first_response_at, completed_at, terminal_status,
                raw_payload_path, raw_payload_sha256, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, case_id, condition, repeat_index) DO UPDATE SET
                repo_full_name=excluded.repo_full_name,
                pr_number=COALESCE(excluded.pr_number, attempts.pr_number),
                base_sha=excluded.base_sha,
                head_sha=excluded.head_sha,
                trigger_kind=excluded.trigger_kind,
                opened_at=COALESCE(excluded.opened_at, attempts.opened_at),
                first_response_at=COALESCE(excluded.first_response_at, attempts.first_response_at),
                completed_at=COALESCE(excluded.completed_at, attempts.completed_at),
                terminal_status=excluded.terminal_status,
                raw_payload_path=COALESCE(excluded.raw_payload_path, attempts.raw_payload_path),
                raw_payload_sha256=COALESCE(excluded.raw_payload_sha256, attempts.raw_payload_sha256),
                notes=COALESCE(excluded.notes, attempts.notes)
            """,
            (
                attempt_id,
                provider_id,
                case_id,
                condition,
                repeat_index,
                repo_full_name,
                pr_number,
                base_sha,
                head_sha,
                trigger_kind,
                opened_at,
                first_response_at,
                completed_at,
                terminal_status,
                raw_payload_path,
                raw_payload_sha256,
                notes,
            ),
        )
    return attempt_id


def ingest_raw_comment(
    conn: sqlite3.Connection,
    *,
    comment_uid: str,
    attempt_id: str,
    source_kind: str,
    author_login: str,
    created_at: str,
    body: str,
    path: str | None = None,
    line: int | None = None,
    html_url: str | None = None,
) -> bool:
    """Insert a comment and mark webhook/retry duplicates with identical normalized text on the same attempt."""
    norm = normalize_comment_body(body)
    norm_sha = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT comment_uid FROM raw_comments WHERE attempt_id = ? AND body_norm_sha256 = ? AND comment_uid != ? AND is_duplicate_delivery = 0",
        (attempt_id, norm_sha, comment_uid),
    ).fetchone()
    is_dup = 1 if existing is not None else 0
    with conn:
        conn.execute(
            """
            INSERT INTO raw_comments (
                comment_uid, attempt_id, source_kind, author_login, created_at,
                path, line, body, body_norm_sha256, html_url, is_duplicate_delivery
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(comment_uid) DO UPDATE SET
                body=excluded.body,
                body_norm_sha256=excluded.body_norm_sha256,
                is_duplicate_delivery=excluded.is_duplicate_delivery
            """,
            (
                comment_uid,
                attempt_id,
                source_kind,
                author_login,
                created_at,
                path,
                line,
                body,
                norm_sha,
                html_url,
                is_dup,
            ),
        )
    return is_dup == 0


def create_or_update_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    attempt_id: str,
    comment_uid: str | None,
    raw_text: str,
    classification: str | None = None,
    cites_env_evidence: bool = False,
    identifies_affected_site: bool = False,
    verification_test: str | None = None,
    grading_rationale: str | None = None,
    graded_by: str | None = None,
) -> str:
    digest = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:10].upper()
    blind_id = f"BLIND-{digest}"
    anon = anonymize_text(raw_text)
    with conn:
        conn.execute(
            """
            INSERT INTO claims (
                claim_id, blind_id, attempt_id, comment_uid, anonymized_text,
                classification, cites_env_evidence, identifies_affected_site,
                verification_test, grading_rationale, graded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                anonymized_text=excluded.anonymized_text,
                classification=COALESCE(excluded.classification, claims.classification),
                cites_env_evidence=CASE WHEN excluded.cites_env_evidence = 1 THEN 1 ELSE claims.cites_env_evidence END,
                identifies_affected_site=CASE WHEN excluded.identifies_affected_site = 1 THEN 1 ELSE claims.identifies_affected_site END,
                verification_test=COALESCE(excluded.verification_test, claims.verification_test),
                grading_rationale=COALESCE(excluded.grading_rationale, claims.grading_rationale),
                graded_by=COALESCE(excluded.graded_by, claims.graded_by)
            """,
            (
                claim_id,
                blind_id,
                attempt_id,
                comment_uid,
                anon,
                classification,
                1 if cites_env_evidence else 0,
                1 if identifies_affected_site else 0,
                verification_test,
                grading_rationale,
                graded_by,
            ),
        )
    return blind_id


def compute_provider_scorecard(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute full coverage denominators, primary recall/specificity/precision, and repeat consistency."""
    providers = [dict(r) for r in conn.execute("SELECT * FROM providers ORDER BY provider_id").fetchall()]
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY provider_id, case_id, condition, repeat_index").fetchall()]
    claims = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY claim_id").fetchall()]

    claims_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for cl in claims:
        claims_by_attempt.setdefault(cl["attempt_id"], []).append(cl)

    att_lookup: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for a in attempts:
        att_lookup[(a["provider_id"], a["case_id"], a["condition"], a["repeat_index"])] = a

    all_cases = sorted(CASE_METADATA.keys())
    matrix: dict[str, dict[str, Any]] = {}
    summary_by_provider: dict[str, Any] = {}

    for p in providers:
        pid = p["provider_id"]
        matrix[pid] = {"code_only": {}, "with_snapshot": {}, "repeat_r2": {}}
        completed_pairs = 0
        quota_stops = 0
        errors = 0
        unattempted = 0

        code_defect_hits = 0
        code_defect_total = 0
        env_snap_hits = 0
        env_snap_total = 0
        env_ask_hits = 0
        env_ask_total = 0
        clean_tn_hits = 0
        clean_total = 0

        valid_claims = 0
        fp_claims = 0
        dup_claims = 0

        repeat_matches = 0
        repeat_pairs = 0

        for cid in all_cases:
            is_held_out, category, exp_code, exp_snap = CASE_METADATA[cid]
            for cond in ("code_only", "with_snapshot"):
                if cond == "with_snapshot" and exp_snap is None:
                    continue
                att = att_lookup.get((pid, cid, cond, 1))
                if att is None or att["terminal_status"] in ("unattempted", "pending_review"):
                    matrix[pid][cond][cid] = {"status": att["terminal_status"] if att else "unattempted", "outcome": "—"}
                    unattempted += 1
                    continue
                st = att["terminal_status"]
                if st == "quota_stopped":
                    quota_stops += 1
                    matrix[pid][cond][cid] = {"status": "quota_stopped", "outcome": "QUOTA_STOP"}
                    continue
                if st == "error":
                    errors += 1
                    matrix[pid][cond][cid] = {"status": "error", "outcome": "ERROR"}
                    continue

                completed_pairs += 1
                acl = claims_by_attempt.get(att["attempt_id"], [])
                labels = [c["classification"] for c in acl if c.get("classification")]
                for lbl in labels:
                    if lbl in ("TP", "ENV-TP", "ENV-PARTIAL", "ENV-ASK", "VALID-AUX"):
                        valid_claims += 1
                    elif lbl == "FP":
                        fp_claims += 1
                    elif lbl == "DUP":
                        dup_claims += 1

                if cond == "code_only":
                    if category == "code_defect":
                        code_defect_total += 1
                        hit = "TP" in labels
                        if hit:
                            code_defect_hits += 1
                        outcome = "TP" if hit else ("FP+FN" if "FP" in labels else "FN")
                    elif category == "env_dependent":
                        env_ask_total += 1
                        hit = ("ENV-ASK" in labels or "ENV-TP" in labels) and ("FP" not in labels)
                        if hit:
                            env_ask_hits += 1
                        outcome = "ENV-ASK" if hit else ("FP" if "FP" in labels else "MISS")
                    else:  # clean
                        clean_total += 1
                        hit = "FP" not in labels
                        if hit:
                            clean_tn_hits += 1
                        outcome = "TN" if hit else "FP"
                else:  # with_snapshot
                    env_snap_total += 1
                    hit = "ENV-TP" in labels
                    if hit:
                        env_snap_hits += 1
                    outcome = "ENV-TP" if hit else ("ENV-PARTIAL" if "ENV-PARTIAL" in labels else "FN")

                matrix[pid][cond][cid] = {
                    "status": st,
                    "outcome": outcome,
                    "pr_number": att.get("pr_number"),
                    "claims": labels,
                }

                # Check r=2 repeatability if present
                att_r2 = att_lookup.get((pid, cid, cond, 2))
                if att_r2 and att_r2["terminal_status"] in ("completed", "completed_quiet"):
                    repeat_pairs += 1
                    acl2 = claims_by_attempt.get(att_r2["attempt_id"], [])
                    labels2 = [c["classification"] for c in acl2 if c.get("classification")]
                    r1_good = any(x in labels for x in ("TP", "ENV-TP", "ENV-ASK")) or (category == "clean" and "FP" not in labels)
                    r2_good = any(x in labels2 for x in ("TP", "ENV-TP", "ENV-ASK")) or (category == "clean" and "FP" not in labels2)
                    if r1_good == r2_good:
                        repeat_matches += 1
                    matrix[pid]["repeat_r2"][f"{cid}:{cond}"] = {
                        "r1_claims": labels,
                        "r2_claims": labels2,
                        "consistent": r1_good == r2_good,
                    }

        non_dup_claims = valid_claims + fp_claims
        summary_by_provider[pid] = {
            "provider_id": pid,
            "display_name": p["display_name"],
            "plan_or_model": p["plan_or_model"],
            "completed_pairs": completed_pairs,
            "quota_stops": quota_stops,
            "errors": errors,
            "unattempted": unattempted,
            "repeat_runs_completed": repeat_pairs,
            "code_defect_recall": f"{code_defect_hits}/{code_defect_total}",
            "code_defect_recall_rate": round(code_defect_hits / code_defect_total, 3) if code_defect_total else None,
            "env_snapshot_accuracy": f"{env_snap_hits}/{env_snap_total}",
            "env_snapshot_accuracy_rate": round(env_snap_hits / env_snap_total, 3) if env_snap_total else None,
            "epistemic_caution_code_only": f"{env_ask_hits}/{env_ask_total}",
            "epistemic_caution_rate": round(env_ask_hits / env_ask_total, 3) if env_ask_total else None,
            "clean_case_specificity": f"{clean_tn_hits}/{clean_total}",
            "clean_case_specificity_rate": round(clean_tn_hits / clean_total, 3) if clean_total else None,
            "claim_precision": f"{valid_claims}/{non_dup_claims}",
            "claim_precision_rate": round(valid_claims / non_dup_claims, 3) if non_dup_claims else None,
            "duplicate_claims": dup_claims,
            "repeat_consistency": f"{repeat_matches}/{repeat_pairs}" if repeat_pairs else "0/0",
        }

    return {
        "providers": summary_by_provider,
        "matrix": matrix,
    }
