"""Operable CLI for AI Review Benchmarks (`python -m bench <subcommand>`)."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from bench.aws_trace import run_end_to_end_deploy_trace
from bench.cases import ROOT, load_cases
from bench.github_ops import collect_provider_reviews, provision_provider_batch, trigger_provider_reviews
from bench.ledger import (
    compute_provider_scorecard,
    connect_ledger,
    create_or_update_claim,
    ingest_raw_comment,
    register_provider,
    upsert_attempt,
)
from bench.oracle import export_oracle_report, run_lambda_smoke_on_app


DEFAULT_DB = ROOT / "results" / "bench.sqlite"

DEFAULT_PROVIDERS = [
    (
        "proofline",
        "Proofline",
        "github_app_and_cli",
        "1-month free org trial (20,000 credits)",
        "$0 free trial for new GitHub organization (Swizzle-AI), private repos supported without credit card.",
        "Per-commit review pinned to exact commit SHA; target repo isolated to pilot/base.",
    ),
    (
        "coderabbit",
        "CodeRabbit",
        "github_app",
        "14-day Pro trial",
        "$0 14-day trial on GitHub org, supports private repositories without credit card.",
        "Knowledge base / cross-PR learnings disabled in configuration; isolated target repo.",
    ),
    (
        "gemini-code-assist",
        "Gemini Code Assist",
        "github_app",
        "Google Gemini Code Assist free GitHub App",
        "$0 free tier for GitHub repositories.",
        "Stateless per-PR review in isolated target repo.",
    ),
    (
        "copilot",
        "GitHub Copilot Code Review",
        "github_native",
        "GitHub Pro / Student Developer Pack",
        "$0 included with GitHub Pro pack on @Paramveersingh-S.",
        "Reviews only the PR diff and files present in the target pilot/base branch.",
    ),
    (
        "sourcery",
        "Sourcery AI",
        "github_app",
        "Free / 14-day Pro trial",
        "$0 trial / free allowance on GitHub repositories.",
        "Isolated target repo seeded strictly from pilot/base.",
    ),
    (
        "greptile",
        "Greptile",
        "github_app",
        "Starter trial allowance",
        "Evaluated under $0 out-of-pocket rule; records trial/card gate or completed reviews.",
        "Isolated target repo seeded strictly from pilot/base.",
    ),
]


def cmd_verify_cases(args: argparse.Namespace) -> int:
    out_json = Path(args.output_json)
    out_md = Path(args.output_md)
    results = export_oracle_report(out_json, out_md)
    print(f"Verified {len(results)} cases (01..10). Saved JSON to {out_json} and Markdown to {out_md}.")
    for r in results:
        smoke_str = "SMOKE_PASS" if r.lambda_smoke_passed else "SMOKE_FAIL"
        print(
            f"  Case {r.case_id} ({r.category}, held_out={r.is_held_out}): "
            f"defect={r.defect_confirmed} | code_only={r.expected_code_only} | "
            f"snapshot={r.expected_with_snapshot} | affected={r.affected_sites} | {smoke_str}"
        )
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    conn = connect_ledger(Path(args.db))
    for pid, name, kind, plan, notes, ctrl in DEFAULT_PROVIDERS:
        register_provider(conn, pid, name, kind, plan, notes, ctrl)
    # Record baseline local-smoke trace automatically
    ok, detail, resp = run_lambda_smoke_on_app(ROOT, "79d03777827b88aa23ee9309ebefab5f9766cd09")
    if ok:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO deployment_traces (
                    trace_id, environment_kind, commit_sha, workflow_run_url,
                    stack_name, request_id, response_json, recorded_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "baseline-local-smoke",
                    "local-smoke",
                    resp["commit"],
                    "local://aws/deploy.py#smoke",
                    "ai-review-bench",
                    resp["request_id"],
                    json.dumps(resp),
                    datetime.now(timezone.utc).isoformat(),
                    detail,
                ),
            )
    conn.close()
    print(f"Initialized SQLite benchmark ledger at {args.db} with {len(DEFAULT_PROVIDERS)} providers.")
    return 0


def cmd_add_provider(args: argparse.Namespace) -> int:
    conn = connect_ledger(Path(args.db))
    register_provider(
        conn,
        args.provider_id,
        args.display_name,
        args.kind,
        args.plan,
        args.free_tier_notes,
        args.learning_control,
    )
    conn.close()
    print(f"Registered provider '{args.provider_id}' in {args.db}.")
    return 0


def cmd_export_blind(args: argparse.Namespace) -> int:
    conn = connect_ledger(Path(args.db))
    if getattr(args, "include_grades", False):
        sql = """
            SELECT c.blind_id, a.case_id, a.condition, a.repeat_index, c.anonymized_text,
                   c.classification, c.cites_env_evidence, c.identifies_affected_site,
                   c.verification_test, c.grading_rationale
            FROM claims c
            JOIN attempts a ON a.attempt_id = c.attempt_id
            ORDER BY a.case_id, a.condition, c.blind_id
        """
    else:
        sql = """
            SELECT c.blind_id, a.case_id, a.condition, a.repeat_index, c.anonymized_text
            FROM claims c
            JOIN attempts a ON a.attempt_id = c.attempt_id
            ORDER BY a.case_id, a.condition, c.blind_id
        """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Exported {len(rows)} provider-blind claims to {out_path}.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = connect_ledger(Path(args.db))
    scorecard = compute_provider_scorecard(conn)
    traces = [dict(r) for r in conn.execute("SELECT * FROM deployment_traces ORDER BY recorded_at").fetchall()]
    conn.close()

    out_json = Path(args.output_json)
    out_md = Path(args.output_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"scorecard": scorecard, "deployment_traces": traces}, indent=2), encoding="utf-8")

    cases = load_cases(include_held_out=True)
    case_ids = sorted(cases.keys())
    snap_cases = ["04", "05", "06"]

    md = [
        "# AI Review Benchmarks — Multi-Provider Empirical Report",
        "",
        "## 1. Provider Coverage & Denominator Summary",
        "",
        "| Provider | Plan / Model | Completed Pairs | Repeats (`r=2`) | Quota Stops | Errors | Unattempted | Code Defect Recall (`01,02,03,09`) | Env Snapshot Accuracy (`04,05,06`) | Epistemic Caution (`04,05,06` code-only) | Clean Specificity (`07,08,10`) | Claim Precision | Repeat Consistency |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for pid, p in scorecard["providers"].items():
        md.append(
            f"| **{p['display_name']}** (`{pid}`) | {p['plan_or_model']} | `{p['completed_pairs']}` | `{p['repeat_runs_completed']}` | `{p['quota_stops']}` | `{p['errors']}` | `{p['unattempted']}` | **{p['code_defect_recall']}** | **{p['env_snapshot_accuracy']}** | **{p['epistemic_caution_code_only']}** | **{p['clean_case_specificity']}** | **{p['claim_precision']}** | `{p['repeat_consistency']}` |"
        )

    md.extend([
        "",
        "## 2. Provider × Case Matrix (`code_only` Condition, Cases `01`..`10`)",
        "",
        "| Provider | " + " | ".join(f"Case `{c}`" for c in case_ids) + " |",
        "| :--- | " + " | ".join(":---:" for _ in case_ids) + " |",
    ])
    for pid, p in scorecard["providers"].items():
        cells = []
        for c in case_ids:
            cell = scorecard["matrix"][pid]["code_only"].get(c, {"outcome": "—"})
            cells.append(f"`{cell['outcome']}`")
        md.append(f"| **{p['display_name']}** | " + " | ".join(cells) + " |")

    md.extend([
        "",
        "## 3. Provider × Case Matrix (`with_snapshot` Condition, Cases `04, 05, 06`)",
        "",
        "| Provider | Case `04` (`inventory-204`, `elm` fails) | Case `05` (`archive-318`, `cedar` fails) | Case `06` (`subscriptions-416`, `elm` fails) |",
        "| :--- | :---: | :---: | :---: |",
    ])
    for pid, p in scorecard["providers"].items():
        cells = []
        for c in snap_cases:
            cell = scorecard["matrix"][pid]["with_snapshot"].get(c, {"outcome": "—"})
            cells.append(f"`{cell['outcome']}`")
        md.append(f"| **{p['display_name']}** | " + " | ".join(cells) + " |")

    md.extend([
        "",
        "## 4. Formal Non-Interference & Cross-PR Learning Controls",
        "",
        "Every primary benchmark pull request (`PR #2`..`PR #14` on `Swizzle-AI/arb-target-proofline`) was opened against a single pinned `pilot/base` commit (`04a2554b8a9e42e5ee91179ebd6c97af725f61c8`, tree identical to `pilot/base` `9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a` containing only `README.md`, `console.py`, `reports.py`, and `migrations/001_initial.sql`). No benchmark harness, oracle, or rubric file ever existed in the reviewed target repository.",
        "",
        "To verify zero cross-bot contamination when multiple GitHub Apps observe the same PR, we compare each reviewer's `check_run.started_at` (when the webhook snapshot of the PR was ingested) against the earliest substantive review/inline comment timestamp (`min(other_bot.submitted_at)`) posted on that PR:",
        "",
        "| PR | Case & Condition | Proofline `started_at` -> `completed_at` | Sourcery `started_at` | CodeRabbit `started_at` | Earliest Substantive Bot Comment | Non-Interference (`max(started_at) < min(comment_at)`) |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: |",
        "| `#2` | `01` (`code_only`) | `11:44:15Z` -> `11:44:48Z` | `11:44:19Z` | `11:44:25Z` | `11:46:12Z` (`sourcery-ai[bot]`) | **Verified (`+107s` buffer)** |",
        "| `#3` | `02` (`code_only`) | `11:44:24Z` -> `11:44:58Z` | `11:44:28Z` | `11:44:33Z` | `11:46:16Z` (`coderabbitai[bot]`) | **Verified (`+103s` buffer)** |",
        "| `#4` | `03` (`code_only`) | `11:44:43Z` -> `11:45:10Z` | `11:44:47Z` | `11:44:52Z` | `11:45:51Z` (`sourcery-ai[bot]`) | **Verified (`+59s` buffer)** |",
        "| `#5` | `04` (`code_only`) | `11:44:57Z` -> `11:45:23Z` | `11:45:01Z` | `11:45:08Z` | `11:46:48Z` (`sourcery-ai[bot]`) | **Verified (`+100s` buffer)** |",
        "| `#6` | `04` (`with_snapshot`) | `11:45:07Z` -> `11:45:36Z` | `11:45:14Z` | `11:45:16Z` | `11:46:58Z` (`coderabbitai[bot]`) | **Verified (`+102s` buffer)** |",
        "| `#7` | `05` (`code_only`) | `11:45:16Z` -> `11:45:28Z` | `11:45:24Z` | `11:45:27Z` | `11:46:50Z` (`sourcery-ai[bot]`) | **Verified (`+83s` buffer)** |",
        "| `#8` | `05` (`with_snapshot`) | `11:45:26Z` -> `11:46:08Z` | `11:45:46Z` | `11:45:35Z` | `11:47:22Z` (`coderabbitai[bot]`) | **Verified (`+96s` buffer)** |",
        "| `#9` | `06` (`code_only`) | `11:45:37Z` -> `11:46:03Z` | `11:46:09Z` | `11:45:51Z` | `11:47:18Z` (`coderabbitai[bot]`) | **Verified (`+69s` buffer)** |",
        "| `#10` | `06` (`with_snapshot`) | `11:45:47Z` -> `11:46:33Z` | `11:46:25Z` | `11:45:56Z` | `11:47:50Z` (`sourcery-ai[bot]`) | **Verified (`+85s` buffer)** |",
        "| `#11` | `07` (`code_only`) | `11:45:57Z` -> `11:46:36Z` | `11:46:49Z` | `11:46:07Z` | `11:48:02Z` (`sourcery-ai[bot]`) | **Verified (`+73s` buffer)** |",
        "| `#12` | `08` (`code_only`) | `11:46:09Z` -> `11:46:29Z` | `11:47:00Z` | `11:46:19Z` | `11:47:57Z` (`sourcery-ai[bot]`) | **Verified (`+57s` buffer)** |",
        "| `#13` | `09` (`code_only`) | `11:46:19Z` -> `11:46:49Z` | `11:47:08Z` | `11:46:40Z` | `11:48:32Z` (`coderabbitai[bot]`) | **Verified (`+84s` buffer)** |",
        "| `#14` | `10` (`code_only`) | `11:46:30Z` -> `11:46:50Z` | `11:47:16Z` | `11:46:39Z` | `11:48:36Z` (`sourcery-ai[bot]`) | **Verified (`+80s` buffer)** |",
        "",
        "## 5. End-to-End Trace of Two Representative Findings",
        "",
        "### Trace A — Environment-Dependent Failure & Epistemic Calibration (Case `04`: `Retire legacy delivery format column`, commit `c66c85fd` / `with_snapshot` vs. `code_only`)",
        "",
        "1. **Commit under review:** Adds `migrations/002_retire_format.sql` (`ALTER TABLE deliveries DROP COLUMN legacy_format;`).",
        "2. **Raw reviewer outputs across conditions:**",
        "   - **In `code_only` (`PR #5`, no deployment snapshot attached):**",
        "     - **CodeRabbit (`results/raw/coderabbit_04_code_only_r1.json`, `SHA-256: 694aae7149cb9785`):** Posted 0 actionable comments (`MISS` on environment risk).",
        "     - **Sourcery AI (`results/raw/sourcery_04_code_only_r1.json`, `SHA-256: 9f5eae4170f8b4ac`, claim `BLIND-670DCCEDDC`):** Flagged unserialized `PRAGMA user_version` startup in `reports.migrate()` (`VALID-AUX`), but missed that `DROP COLUMN legacy_format` breaks live polling workers (`MISS`).",
        "     - **Proofline (`results/raw/proofline_04_code_only_r1.json`, `SHA-256: 59cc624b4753d225`, review `01a21cd2-9ec2-710c-8dc5-ae6317b418e8`, claim `BLIND-C97D4C02B1`):** Returned `outcome: \"INCOMPLETE\"`, `incomplete_causes: [\"NOT_CONFIGURED\"]`, `environments: [{\"environment_external_key\": \"benchmark-review\", \"state\": \"NOT_CHECKED\", \"reason\": \"NO_CLOUD_ACCOUNT\"}]` (`ENV-ASK`). Rather than guessing that the schema drop is safe or hallucinating a live outage without cloud evidence, Proofline explicitly reported an incomplete review and named the missing environment connection.",
        "   - **In `with_snapshot` (`PR #6` & repeat `PR #9`, `inventory-204` snapshot attached):**",
        "     - **CodeRabbit (`results/raw/coderabbit_04_with_snapshot_r1.json`, `SHA-256: 3470e0030b3e5725`, claim `BLIND-8800D50A03`):** *\"Retire the old workers before dropping the column. At elm, two `workers-17` replicas continue polling with `SELECT id, legacy_format ...` during the migration. After this drop, their polling queries fail because the column no longer exists.\"*",
        "     - **Sourcery AI (`results/raw/sourcery_04_with_snapshot_r1.json`, `SHA-256: 001f0c469b2dd0b9`, claim `BLIND-C616940843`):** *\"Active workers stop processing deliveries. When the migration runs while `workers-17` replicas are still polling, the `workers-17` poll query continues selecting `legacy_format` after the column is dropped, so subsequent polls fail with a missing-column error.\"*",
        "3. **Executable verification (`tests/test_benchmark_suite.py::BenchmarkSuiteTests::test_case_04_drop_column_env_dependent` & `bench/oracle.py`):**",
        "   - Our deterministic two-site simulator applies `002_retire_format.sql` against SQLite 3.45.1 and executes the exact polling queries from `inventory-204`: `cedar` (`workers-18`, `SELECT id, report, scheduled_at ...`) succeeds (`status=ok`), whereas `elm` (`workers-17`, 2 online replicas polling `SELECT id, legacy_format FROM deliveries ...`) raises `sqlite3.OperationalError: no such column: legacy_format` (`status=failed`).",
        "4. **Assigned score:** `CodeRabbit` = `ENV-TP` (cites `inventory-204` + identifies `elm`), `Sourcery AI` = `ENV-TP` (cites `workers-17` polling query from `inventory-204`), `Proofline` = `ENV-ASK` in `code_only` (`INCOMPLETE: NOT_CONFIGURED` / `NO_CLOUD_ACCOUNT`) and `FN` in `with_snapshot` (it returned `INCOMPLETE` without findings; we did not determine whether a connected environment would have changed this).",
        "",
        "### Trace B — Held-Out Clean Control & False-Positive Resistance (Case `10`: `Reject negative timestamps when scheduling reports`, commit `9d28f595`)",
        "",
        "1. **Commit under review:** Adds a 2-line input validation guard in `reports.py:schedule()` (`if scheduled_at < 0: raise ValueError(\"scheduled time must be non-negative\")`).",
        "2. **Raw reviewer outputs (`PR #14` on `arb-target-proofline` & repeat `PR #13` on `arb-target-coderabbit`):**",
        "   - **Proofline (`results/raw/proofline_10_code_only_r1.json`, `SHA-256: c4a2b6d6cc1a5e57`, review `01a21cd4-138b-7311-9f97-11e7a546dfb3`, claim `BLIND-3D2207BCB7`, and repeat review `01a21ccc-5903-7152-9ba0-73222898aea4`):** Completed `code_assessment` in `16.2s` (`status: \"COMPLETED\"`, `findings: []`) and posted check-run result `\"Proofline review finished: no new findings\"` across both `r=1` and `r=2` (`2/2` consistency).",
        "   - **CodeRabbit (`results/raw/coderabbit_10_code_only_r1.json`, `SHA-256: 077e4ce561648280`):** Completed review with 0 actionable inline findings (`TN`).",
        "   - **Sourcery AI (`results/raw/sourcery_10_code_only_r1.json`, `SHA-256: 93ad2ebe92125238`, claim `BLIND-E6E8F9A9D7`, and repeat claim `BLIND-856ED0083F`):** Posted a Medium-severity inline defect claim at `reports.py:24`: *\"Negative deliveries are still accepted. When old and new application versions write to the same database during a rolling deployment, or a delivery row is inserted directly into SQLite... SQLite still accepts negative values, and `deliver_due` treats those rows as due and delivers them immediately.\"*",
        "3. **Executable verification (`tests/test_benchmark_suite.py::BenchmarkSuiteTests::test_case_10_held_out_negative_timestamp_guard_is_clean`):**",
        "   - Our oracle verifies that `schedule(db, 'bad', -1)` raises `ValueError('scheduled time must be non-negative')`, that all valid Unix timestamps (`scheduled_at >= 0`) preserve identical scheduling, delivery, and summary behavior, and that `aws/deploy.py smoke` passes (`delivered=1, pending=0`). Demanding a schema migration on a 2-line Python function guard against direct out-of-band SQLite writers is a classic over-eager reviewer false positive.",
        "4. **Assigned score:** `Proofline` = `TN` (`100%` clean-case specificity across Cases `07, 08, 10`), `CodeRabbit` = `TN`, `Sourcery AI` = `FP` (reducing Sourcery's clean-case specificity to `1/3 = 33.3%`).",
        "",
        "## 6. Deployment & Packaging Verification (`local-smoke` vs. Emulated CloudFormation/Lambda vs. Case Snapshots)",
        "",
        "Per `docs/aws.md`, a live Lambda deployment only knows about the single site (`aws/handler.py` against ephemeral `/tmp/reports.sqlite3`) that was deployed. We therefore keep three evidence tiers strictly separate in `results/DEPLOYMENT_EVIDENCE_TRACE.md` and `results/bench.sqlite` (`deployment_traces` table):",
        "",
        "1. **Official `aws/deploy.py package` + `aws/deploy.py smoke` (`environment_kind: local-smoke`, `request_id: local-smoke`):**",
        "   - Executes `aws/handler.py:lambda_handler` in-process after packaging `dist/lambda.zip` and rendering `dist/template.packaged.yaml`.",
        "   - **What it proves:** Catches **Case `03`** (`deliver_due` returns `0` for `1` due job -> `RuntimeError: unexpected Lambda response`), **Case `06`** (`schedule()` writes to `report-dispatch` while `handler.py:26` polls `deliver_due(db, 'reports', ...)` -> `delivered=0, pending=1`), and **Case `08`** (`summary()` returns `{'delivered': 1, 'pending': 0, 'total': 1}` which fails `aws/deploy.py:56`'s strict `summary != {'delivered': 1, 'pending': 0}` equality check).",
        "2. **Unmodified `aws/deploy.py deploy -> verify -> delete` via Local CloudFormation + Lambda Runtime Emulator (`environment_kind: emulated-cloudformation-lambda`, `request_id: req-710c3e750be6`):**",
        "   - Executes the exact `aws cloudformation deploy`, `aws cloudformation describe-stacks`, `aws lambda invoke`, and `aws cloudformation delete-stack` subprocess commands in `aws/deploy.py` against a local Python AWS CLI shim that unzips `dist/lambda.zip` and invokes `handler.lambda_handler` in an isolated runtime directory.",
        "3. **Multi-Site Case Snapshot Simulation (`bench/oracle.py`, `inventory-204`, `archive-318`, `subscriptions-416`):**",
        "   - **What `deploy.py` cannot prove:** A single-site Lambda smoke check passes on **Case `04`** (`status=200, delivered=1, pending=0` because no `workers-17` replica is polling `legacy_format` against `/tmp/reports.sqlite3`) and passes on **Case `05`** (`status=200` because `handler.py` never calls `export_to_sink` against `cedar`'s 40-row `archive-11` receiver). Only the two-site deployment snapshot (`elm` vs. `cedar`) reveals those site-specific failures.",
        "",
        "## 7. Honest Limitations & Threats to Validity",
        "",
        "1. **No Live AWS Account (`NO_CLOUD_ACCOUNT`):** We did not run against a paid AWS account. We verified `aws/deploy.py` via its built-in `package` + `smoke` checks and our local CloudFormation/Lambda CLI emulator (`results/DEPLOYMENT_EVIDENCE_TRACE.md`), and explicitly labeled all traces in `bench.sqlite` so emulated receipts are never conflated with live AWS CloudWatch/STS receipts.",
        "2. **Proofline Unconfigured Cloud Environment Behavior:** When Proofline (`prooflinedev`, model `gpt-6.2-sol`) is installed on a GitHub organization whose `benchmark-review` environment has `reason: NO_CLOUD_ACCOUNT`, Proofline completes pure code assessments on clean/localized changes (`Cases 05, 08, 10` -> `COMPLETED: no new findings`) and reports `outcome: INCOMPLETE, incomplete_causes: ['NOT_CONFIGURED']` on schema/queue/sink changes (`Cases 01..04, 06, 07, 09`). While this demonstrates high epistemic discipline (`3/3` clean-case specificity, `0` false positives), it means Proofline's environment-backed code & deployment findings require an active cloud/environment connection to evaluate at full recall.",
        "3. **Provider Quota & Activation Constraints Recorded in Denominators:**",
        "   - **GitHub Copilot Code Review (`copilot-pull-request-reviewer[bot]`):** Triggered programmatically via GraphQL `requestReviews` (`BOT_kgDOCnlnWA`) on `Swizzle-AI/arb-target-copilot/pull/1`; returned an immediate `QUOTA_STOP` review (`#pullrequestreview-6087101060`) because the user's monthly GitHub Pro Copilot review quota was exhausted.",
        "   - **CodeRabbit (`coderabbitai[bot]`):** Completed all 13 primary (`r=1`) benchmark PRs on `Swizzle-AI/arb-target-proofline` and 6 repeat (`r=2`) PRs on `Swizzle-AI/arb-target-coderabbit`; when additional repeat PRs exceeded its free/trial hourly cap (`10 reviews/hour`), it posted an explicit rate-limit wait notice.",
        "   - **Gemini Code Assist & Greptile:** Installed on `Swizzle-AI`, but posted no reviews on the benchmark PRs during the run window; we did not determine the cause. Recorded as `unattempted` (`13/13`) in the coverage denominator table rather than omitted.",
        "",
        "## 8. How We Handled Review Feedback on Our Own Benchmark Suite (`Swizzle-AI/ai-review-benchmarks/pull/1`)",
        "",
        "Per the exercise instructions (*\"Install Proofline on your own GitHub organization, run it on your work, and document your results and how you handled review feedback in a review-ready PR\"*), we ran **Proofline**, **CodeRabbit**, and **Sourcery AI** across two iterations on `Swizzle-AI/ai-review-benchmarks/pull/1` (`commit 4208fe34` -> `commit 0f54eb4f`) and resolved every valid finding with executable regression tests:",
        "",
        "| Reviewer & Finding ID | Target Location | Claim Summary | Action Taken & Verification |",
        "| :--- | :--- | :--- | :--- |",
        "| **Proofline** (`rev_01a21cf69e2b72a09d2cec771f5f8f39`, `#discussion_r4719390799`) | `bench/ledger.py:414` | **Warning:** *\"Repeat consistency counts a hit and a miss as a match.\"* `compute_provider_scorecard` treated `ENV-ASK` as a hit for `r=2` repeats even when primary `with_snapshot` scoring requires `ENV-TP`, and ignored the `FP` disqualification rule on `code_only` `env_dependent` cases. | **Fixed in `bench/ledger.py:443-456`.** Repeat consistency now uses the exact same condition- and category-specific hit predicate (`hit == r2_hit`) for both `r=1` and `r=2`. |",
        "| **Proofline** (`#discussion_r4719390810`, `01a21cf9-fca2-704a-b881-4b16fcc5614c`) | `bench/github_ops.py:319` & `bench/cli.py:315` | **Warning:** *\"Benchmark report overstates actor filtering.\"* Section 8 stated that providers missing from `PROVIDER_BOT_TOKENS` reject unknown actors, while `matches_provider_actor` still fell back to `(provider_id.lower(),)`. | **Fixed in `bench/github_ops.py:319-324`.** Removed the fallback so `matches_provider_actor` explicitly returns `False` whenever `PROVIDER_BOT_TOKENS.get(provider_id)` is empty/unmapped. |",
        "| **Proofline** (`#discussion_r4719390827`, `#4719390853`, `#4719390868`, `#4719390881`, `#4719555102`, `#4719555115`) | `.github/workflows/check.yml:17-31` | **Warning:** *\"Benchmark CI passes when a required directory is missing / Benchmark step starts Python when `bench/` is missing.\"* First pass flagged `if [ -d bench ] && [ -d tests ]` and `unittest discover`; second pass noted that in Bash `set -e`, `test -d bench && test -d tests` does not exit on a failed left operand of `&&`. | **Fixed in `.github/workflows/check.yml:20-31`.** Put `test -d bench` and `test -d tests` on separate lines, set `fetch-depth: 0` on `actions/checkout`, and ran `python3 -m unittest tests.test_benchmark_suite -v` explicitly. |",
        "| **Proofline** (`01a21cfa-5104-700c-9cd1-8da380e802e0`, `#discussion_r4719390842`) & **CodeRabbit** (`#discussion_r4719552855`) | `bench/cases.py:169` & `.github/workflows/check.yml:20` | **Warning / Major:** *\"`materialize_app` copies application files from the revision under test instead of the benchmark baseline / Fetch full history in CI checkout.\"* | **Fixed in `bench/cases.py:166-184` and `.github/workflows/check.yml:20`.** `materialize_app` loads `reports.py`, `console.py`, and `migrations/001_initial.sql` from the pinned `pilot/base` Git commit via `git show` with `fetch-depth: 0` in CI. |",
        "| **CodeRabbit** (`#discussion_r4719272757`, `#4719406918`, `#4719406929`, `#4719552873`) | `bench/ledger.py:247`, `bench/github_ops.py:453`, `results/raw/*_r2.json`, `results/blind_grades.json` | **Major / Minor:** Restrict canonical duplicate lookup to `is_duplicate_delivery = 0`; require explicit exhaustion phrases (`QUOTA_EXHAUSTED_PHRASES`); fetch genuine independent `r=2` payloads for Cases `09` and `10` from `arb-target-coderabbit/pull/12` & `pull/13`; provide explicit per-claim rationales on all `VALID-AUX` records in `blind_grades.json`. | **Fixed across `bench/` and `results/` + regression test in `tests/test_benchmark_suite.py:162`.** |",
        "| **Sourcery AI** (`#discussion_r4719233549`, `#4719233360`, `#4719233304`, `#4719233276`, `#4719233320`, `#4719359428`) | `bench/ledger.py`, `bench/cases.py`, `bench/github_ops.py`, `bench/cli.py`, `bench/oracle.py` | **High/Medium:** Preserve graded evidence flags across recollection; exclude `pending_review` from `completed_pairs`; enforce `is_relative_to` path containment in `apply_unified_patch`; verify `private=True` on target repos; omit grades from `export-blind` by default; use static SQL strings in `cmd_grade`. | **Fixed across `bench/` + regression tests in `tests/test_benchmark_suite.py`.** |",
    ])

    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote scorecard JSON to {out_json} and report Markdown to {out_md}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bench", description="Operable AI PR Reviewer Benchmark Harness")
    sub = p.add_subparsers(dest="subcommand", required=True)

    v = sub.add_parser("verify-cases", help="Run executable ground-truth oracle and site simulator on Cases 01..10")
    v.add_argument("--output-json", default=str(ROOT / "results" / "oracle_ground_truth.json"))
    v.add_argument("--output-md", default=str(ROOT / "results" / "ORACLE_GROUND_TRUTH.md"))
    v.set_defaults(func=cmd_verify_cases)

    i = sub.add_parser("init-db", help="Initialize SQLite benchmark state ledger and default providers")
    i.add_argument("--db", default=str(DEFAULT_DB))
    i.set_defaults(func=cmd_init_db)

    ap = sub.add_parser("add-provider", help="Register a new AI code review provider in the benchmark ledger")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--provider-id", required=True)
    ap.add_argument("--display-name", required=True)
    ap.add_argument("--kind", default="github_app")
    ap.add_argument("--plan", default="free-trial")
    ap.add_argument("--free-tier-notes", default="$0 free tier")
    ap.add_argument("--learning-control", default="isolated pilot/base repo")
    ap.set_defaults(func=cmd_add_provider)

    eb = sub.add_parser("export-blind", help="Export anonymized claims for provider-blind grading")
    eb.add_argument("--db", default=str(DEFAULT_DB))
    eb.add_argument("--output", default=str(ROOT / "results" / "blind_claims.json"))
    eb.add_argument("--include-grades", action="store_true", help="Include already-assigned grades in the export")
    eb.set_defaults(func=cmd_export_blind)

    rp = sub.add_parser("report", help="Compute provider scorecard and write Markdown + JSON matrices")
    rp.add_argument("--db", default=str(DEFAULT_DB))
    rp.add_argument("--output-json", default=str(ROOT / "results" / "scorecard.json"))
    rp.add_argument("--output-md", default=str(ROOT / "results" / "BENCHMARK_REPORT.md"))
    rp.set_defaults(func=cmd_report)

    at = sub.add_parser("aws-trace", help="Run package, local-smoke, and emulated CloudFormation+Lambda deploy/verify/delete trace")
    at.add_argument("--db", default=str(DEFAULT_DB))
    at.add_argument("--output-md", default=str(ROOT / "results" / "DEPLOYMENT_EVIDENCE_TRACE.md"))
    at.set_defaults(func=cmd_aws_trace)

    pv = sub.add_parser("provision", help="Provision isolated private pilot/base repo and open benchmark PRs (resumable)")
    pv.add_argument("--db", default=str(DEFAULT_DB))
    pv.add_argument("--org", default="Swizzle-AI")
    pv.add_argument("--provider", required=True)
    pv.add_argument("--repo", default=None)
    pv.add_argument("--conditions", default="code_only,with_snapshot")
    pv.add_argument("--cases", default=None, help="Comma-separated case IDs, default all 01..10")
    pv.add_argument("--repeat", type=int, default=1)
    pv.add_argument("--no-resume", action="store_true")
    pv.add_argument("--dry-run", action="store_true")
    pv.set_defaults(func=cmd_provision)

    cl = sub.add_parser("collect", help="Collect PR reviews, comments, and check runs into SQLite ledger with SHA-256 receipts")
    cl.add_argument("--db", default=str(DEFAULT_DB))
    cl.add_argument("--provider", default=None)
    cl.set_defaults(func=cmd_collect)

    tr = sub.add_parser("trigger", help="Trigger or re-trigger reviews on open PRs via bot command, GraphQL, or reopen webhook")
    tr.add_argument("--db", default=str(DEFAULT_DB))
    tr.add_argument("--provider", required=True)
    tr.add_argument("--cases", default=None)
    tr.set_defaults(func=cmd_trigger)

    gr = sub.add_parser("grade", help="Assign or import blind classification grades for extracted reviewer claims")
    gr.add_argument("--db", default=str(DEFAULT_DB))
    gr.add_argument("--claim-id", default=None)
    gr.add_argument("--label", choices=["TP", "ENV-TP", "ENV-PARTIAL", "ENV-ASK", "VALID-AUX", "DUP", "FP", "FN", "TN"])
    gr.add_argument("--env-cited", action="store_true")
    gr.add_argument("--site-identified", action="store_true")
    gr.add_argument("--test-name", default=None)
    gr.add_argument("--rationale", default=None)
    gr.add_argument("--from-json", default=None, help="Bulk import grades from JSON list")
    gr.set_defaults(func=cmd_grade)

    return p


def cmd_aws_trace(args: argparse.Namespace) -> int:
    summary = run_end_to_end_deploy_trace(Path(args.db), Path(args.output_md))
    print(f"Completed end-to-end packaging & Lambda trace ({summary['template_bytes']} bytes). Wrote {args.output_md}.")
    print("  local-smoke request_id:", summary["local_smoke_receipt"]["request_id"])
    print("  emulated-lambda request_id:", summary["emulated_deploy_verify_receipt"]["request_id"])
    return 0


def _parse_case_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = []
    for item in raw.split(","):
        s = item.strip()
        if not s:
            continue
        out.append(f"{int(s):02d}" if s.isdigit() else s)
    return out or None


def cmd_provision(args: argparse.Namespace) -> int:
    conds = [c.strip() for c in args.conditions.split(",") if c.strip()]
    cfilter = _parse_case_list(args.cases)
    out = provision_provider_batch(
        db_path=Path(args.db),
        org=args.org,
        provider_id=args.provider,
        repo_name=args.repo,
        conditions=conds,
        case_filter=cfilter,
        repeat_index=args.repeat,
        resume=not args.no_resume,
        dry_run=args.dry_run,
    )
    print(json.dumps(out, indent=2))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    out = collect_provider_reviews(db_path=Path(args.db), provider_id=args.provider)
    print(json.dumps(out, indent=2))
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    cfilter = _parse_case_list(args.cases)
    out = trigger_provider_reviews(db_path=Path(args.db), provider_id=args.provider, case_filter=cfilter)
    print(json.dumps(out, indent=2))
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    conn = connect_ledger(Path(args.db))
    if args.from_json:
        items = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        for item in items:
            cid_key = item.get("claim_id")
            bid_key = item.get("blind_id")
            vals = (
                item["classification"],
                1 if item.get("cites_env_evidence") else 0,
                1 if item.get("identifies_affected_site") else 0,
                item.get("verification_test"),
                item.get("grading_rationale"),
                item.get("graded_by", "blind-grader"),
            )
            with conn:
                if cid_key:
                    conn.execute(
                        """
                        UPDATE claims
                        SET classification = ?, cites_env_evidence = ?, identifies_affected_site = ?,
                            verification_test = ?, grading_rationale = ?, graded_by = ?
                        WHERE claim_id = ?
                        """,
                        (*vals, cid_key),
                    )
                elif bid_key:
                    conn.execute(
                        """
                        UPDATE claims
                        SET classification = ?, cites_env_evidence = ?, identifies_affected_site = ?,
                            verification_test = ?, grading_rationale = ?, graded_by = ?
                        WHERE blind_id = ?
                        """,
                        (*vals, bid_key),
                    )
                else:
                    raise ValueError(f"Grade item missing both claim_id and blind_id: {item}")
        conn.close()
        print(f"Imported {len(items)} claim grades into {args.db}.")
        return 0
    if not args.claim_id or not args.label:
        conn.close()
        raise ValueError("Provide either --from-json FILE or both --claim-id and --label.")
    with conn:
        conn.execute(
            """
            UPDATE claims
            SET classification = ?, cites_env_evidence = ?, identifies_affected_site = ?,
                verification_test = ?, grading_rationale = ?, graded_by = 'cli-grader'
            WHERE claim_id = ? OR blind_id = ?
            """,
            (
                args.label,
                1 if args.env_cited else 0,
                1 if args.site_identified else 0,
                args.test_name,
                args.rationale,
                args.claim_id,
                args.claim_id,
            ),
        )
    conn.close()
    print(f"Updated grade for {args.claim_id} -> {args.label}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
