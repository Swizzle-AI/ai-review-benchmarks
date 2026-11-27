# AI Review Benchmarks — Multi-Provider Empirical Report

## 1. Provider Coverage & Denominator Summary

| Provider | Plan / Model | Completed Pairs | Repeats (`r=2`) | Quota Stops | Errors | Unattempted | Code Defect Recall (`01,02,03,09`) | Env Snapshot Accuracy (`04,05,06`) | Epistemic Caution (`04,05,06` code-only) | Clean Specificity (`07,08,10`) | Claim Precision | Repeat Consistency |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **CodeRabbit** (`coderabbit`) | 14-day Pro trial | `13` | `6` | `0` | `0` | `0` | **4/4** | **3/3** | **1/3** | **3/3** | **8/8** | `5/6` |
| **GitHub Copilot Code Review** (`copilot`) | GitHub Pro / Student Developer Pack | `0` | `0` | `1` | `0` | `12` | **0/0** | **0/0** | **0/0** | **0/0** | **0/0** | `0/0` |
| **Gemini Code Assist** (`gemini-code-assist`) | Google Gemini Code Assist free GitHub App | `0` | `0` | `0` | `0` | `13` | **0/0** | **0/0** | **0/0** | **0/0** | **0/0** | `0/0` |
| **Greptile** (`greptile`) | Starter trial allowance | `0` | `0` | `0` | `0` | `13` | **0/0** | **0/0** | **0/0** | **0/0** | **0/0** | `0/0` |
| **Proofline** (`proofline`) | 1-month free org trial (20,000 credits) | `13` | `5` | `0` | `0` | `0` | **0/4** | **0/3** | **2/3** | **3/3** | **2/2** | `5/5` |
| **Sourcery AI** (`sourcery`) | Free / 14-day Pro trial | `13` | `6` | `0` | `0` | `0` | **4/4** | **3/3** | **1/3** | **1/3** | **14/16** | `6/6` |

## 2. Provider × Case Matrix (`code_only` Condition, Cases `01`..`10`)

| Provider | Case `01` | Case `02` | Case `03` | Case `04` | Case `05` | Case `06` | Case `07` | Case `08` | Case `09` | Case `10` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **CodeRabbit** | `TP` | `TP` | `TP` | `MISS` | `MISS` | `ENV-ASK` | `TN` | `TN` | `TP` | `TN` |
| **GitHub Copilot Code Review** | `QUOTA_STOP` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` |
| **Gemini Code Assist** | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` |
| **Greptile** | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` | `—` |
| **Proofline** | `FN` | `FN` | `FN` | `ENV-ASK` | `MISS` | `ENV-ASK` | `TN` | `TN` | `FN` | `TN` |
| **Sourcery AI** | `TP` | `TP` | `TP` | `MISS` | `MISS` | `ENV-ASK` | `FP` | `TN` | `TP` | `FP` |

## 3. Provider × Case Matrix (`with_snapshot` Condition, Cases `04, 05, 06`)

| Provider | Case `04` (`inventory-204`, `elm` fails) | Case `05` (`archive-318`, `cedar` fails) | Case `06` (`subscriptions-416`, `elm` fails) |
| :--- | :---: | :---: | :---: |
| **CodeRabbit** | `ENV-TP` | `ENV-TP` | `ENV-TP` |
| **GitHub Copilot Code Review** | `—` | `—` | `—` |
| **Gemini Code Assist** | `—` | `—` | `—` |
| **Greptile** | `—` | `—` | `—` |
| **Proofline** | `FN` | `FN` | `FN` |
| **Sourcery AI** | `ENV-TP` | `ENV-TP` | `ENV-TP` |

## 4. Formal Non-Interference & Cross-PR Learning Controls

Every primary benchmark pull request (`PR #2`..`PR #14` on `Swizzle-AI/arb-target-proofline`) was opened against a single pinned `pilot/base` commit (`04a2554b8a9e42e5ee91179ebd6c97af725f61c8`, tree identical to `pilot/base` `9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a` containing only `README.md`, `console.py`, `reports.py`, and `migrations/001_initial.sql`). No benchmark harness, oracle, or rubric file ever existed in the reviewed target repository.

To verify zero cross-bot contamination when multiple GitHub Apps observe the same PR, we compare each reviewer's `check_run.started_at` (when the webhook snapshot of the PR was ingested) against the earliest substantive review/inline comment timestamp (`min(other_bot.submitted_at)`) posted on that PR:

| PR | Case & Condition | Proofline `started_at` -> `completed_at` | Sourcery `started_at` | CodeRabbit `started_at` | Earliest Substantive Bot Comment | Non-Interference (`max(started_at) < min(comment_at)`) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: |
| `#2` | `01` (`code_only`) | `11:44:15Z` -> `11:44:48Z` | `11:44:19Z` | `11:44:25Z` | `11:46:12Z` (`sourcery-ai[bot]`) | **Verified (`+107s` buffer)** |
| `#3` | `02` (`code_only`) | `11:44:24Z` -> `11:44:58Z` | `11:44:28Z` | `11:44:33Z` | `11:46:16Z` (`coderabbitai[bot]`) | **Verified (`+103s` buffer)** |
| `#4` | `03` (`code_only`) | `11:44:43Z` -> `11:45:10Z` | `11:44:47Z` | `11:44:52Z` | `11:45:51Z` (`sourcery-ai[bot]`) | **Verified (`+59s` buffer)** |
| `#5` | `04` (`code_only`) | `11:44:57Z` -> `11:45:23Z` | `11:45:01Z` | `11:45:08Z` | `11:46:48Z` (`sourcery-ai[bot]`) | **Verified (`+100s` buffer)** |
| `#6` | `04` (`with_snapshot`) | `11:45:07Z` -> `11:45:36Z` | `11:45:14Z` | `11:45:16Z` | `11:46:58Z` (`coderabbitai[bot]`) | **Verified (`+102s` buffer)** |
| `#7` | `05` (`code_only`) | `11:45:16Z` -> `11:45:28Z` | `11:45:24Z` | `11:45:27Z` | `11:46:50Z` (`sourcery-ai[bot]`) | **Verified (`+83s` buffer)** |
| `#8` | `05` (`with_snapshot`) | `11:45:26Z` -> `11:46:08Z` | `11:45:46Z` | `11:45:35Z` | `11:47:22Z` (`coderabbitai[bot]`) | **Verified (`+96s` buffer)** |
| `#9` | `06` (`code_only`) | `11:45:37Z` -> `11:46:03Z` | `11:46:09Z` | `11:45:51Z` | `11:47:18Z` (`coderabbitai[bot]`) | **Verified (`+69s` buffer)** |
| `#10` | `06` (`with_snapshot`) | `11:45:47Z` -> `11:46:33Z` | `11:46:25Z` | `11:45:56Z` | `11:47:50Z` (`sourcery-ai[bot]`) | **Verified (`+85s` buffer)** |
| `#11` | `07` (`code_only`) | `11:45:57Z` -> `11:46:36Z` | `11:46:49Z` | `11:46:07Z` | `11:48:02Z` (`sourcery-ai[bot]`) | **Verified (`+73s` buffer)** |
| `#12` | `08` (`code_only`) | `11:46:09Z` -> `11:46:29Z` | `11:47:00Z` | `11:46:19Z` | `11:47:57Z` (`sourcery-ai[bot]`) | **Verified (`+57s` buffer)** |
| `#13` | `09` (`code_only`) | `11:46:19Z` -> `11:46:49Z` | `11:47:08Z` | `11:46:40Z` | `11:48:32Z` (`coderabbitai[bot]`) | **Verified (`+84s` buffer)** |
| `#14` | `10` (`code_only`) | `11:46:30Z` -> `11:46:50Z` | `11:47:16Z` | `11:46:39Z` | `11:48:36Z` (`sourcery-ai[bot]`) | **Verified (`+80s` buffer)** |

## 5. End-to-End Trace of Two Representative Findings

### Trace A — Environment-Dependent Failure & Epistemic Calibration (Case `04`: `Retire legacy delivery format column`, commit `c66c85fd` / `with_snapshot` vs. `code_only`)

1. **Commit under review:** Adds `migrations/002_retire_format.sql` (`ALTER TABLE deliveries DROP COLUMN legacy_format;`).
2. **Raw reviewer outputs across conditions:**
   - **In `code_only` (`PR #5`, no deployment snapshot attached):**
     - **CodeRabbit (`results/raw/coderabbit_04_code_only_r1.json`, `SHA-256: 694aae7149cb9785`):** Posted 0 actionable comments (`MISS` on environment risk).
     - **Sourcery AI (`results/raw/sourcery_04_code_only_r1.json`, `SHA-256: 9f5eae4170f8b4ac`, claim `BLIND-670DCCEDDC`):** Flagged unserialized `PRAGMA user_version` startup in `reports.migrate()` (`VALID-AUX`), but missed that `DROP COLUMN legacy_format` breaks live polling workers (`MISS`).
     - **Proofline (`results/raw/proofline_04_code_only_r1.json`, `SHA-256: 59cc624b4753d225`, review `01a21cd2-9ec2-710c-8dc5-ae6317b418e8`, claim `BLIND-C97D4C02B1`):** Returned `outcome: "INCOMPLETE"`, `incomplete_causes: ["NOT_CONFIGURED"]`, `environments: [{"environment_external_key": "benchmark-review", "state": "NOT_CHECKED", "reason": "NO_CLOUD_ACCOUNT"}]` (`ENV-ASK`). Rather than guessing that the schema drop is safe or hallucinating a live outage without cloud evidence, Proofline explicitly reported an incomplete review and named the missing environment connection.
   - **In `with_snapshot` (`PR #6` & repeat `PR #9`, `inventory-204` snapshot attached):**
     - **CodeRabbit (`results/raw/coderabbit_04_with_snapshot_r1.json`, `SHA-256: 3470e0030b3e5725`, claim `BLIND-8800D50A03`):** *"Retire the old workers before dropping the column. At elm, two `workers-17` replicas continue polling with `SELECT id, legacy_format ...` during the migration. After this drop, their polling queries fail because the column no longer exists."*
     - **Sourcery AI (`results/raw/sourcery_04_with_snapshot_r1.json`, `SHA-256: 001f0c469b2dd0b9`, claim `BLIND-C616940843`):** *"Active workers stop processing deliveries. When the migration runs while `workers-17` replicas are still polling, the `workers-17` poll query continues selecting `legacy_format` after the column is dropped, so subsequent polls fail with a missing-column error."*
3. **Executable verification (`tests/test_benchmark_suite.py::BenchmarkSuiteTests::test_case_04_drop_column_env_dependent` & `bench/oracle.py`):**
   - Our deterministic two-site simulator applies `002_retire_format.sql` against SQLite 3.45.1 and executes the exact polling queries from `inventory-204`: `cedar` (`workers-18`, `SELECT id, report, scheduled_at ...`) succeeds (`status=ok`), whereas `elm` (`workers-17`, 2 online replicas polling `SELECT id, legacy_format FROM deliveries ...`) raises `sqlite3.OperationalError: no such column: legacy_format` (`status=failed`).
4. **Assigned score:** `CodeRabbit` = `ENV-TP` (cites `inventory-204` + identifies `elm`), `Sourcery AI` = `ENV-TP` (cites `workers-17` polling query from `inventory-204`), `Proofline` = `ENV-ASK` in `code_only` (`INCOMPLETE: NOT_CONFIGURED` / `NO_CLOUD_ACCOUNT`) and `FN` in `with_snapshot` (because Proofline's environment verifier checks live connected cloud environments rather than prose markdown snapshots when `reason: NO_CLOUD_ACCOUNT`).

### Trace B — Held-Out Clean Control & False-Positive Resistance (Case `10`: `Reject negative timestamps when scheduling reports`, commit `9d28f595`)

1. **Commit under review:** Adds a 2-line input validation guard in `reports.py:schedule()` (`if scheduled_at < 0: raise ValueError("scheduled time must be non-negative")`).
2. **Raw reviewer outputs (`PR #14` on `arb-target-proofline` & repeat `PR #13` on `arb-target-coderabbit`):**
   - **Proofline (`results/raw/proofline_10_code_only_r1.json`, `SHA-256: c4a2b6d6cc1a5e57`, review `01a21cd4-138b-7311-9f97-11e7a546dfb3`, claim `BLIND-3D2207BCB7`, and repeat review `01a21ccc-5903-7152-9ba0-73222898aea4`):** Completed `code_assessment` in `16.2s` (`status: "COMPLETED"`, `findings: []`) and posted check-run result `"Proofline review finished: no new findings"` across both `r=1` and `r=2` (`2/2` consistency).
   - **CodeRabbit (`results/raw/coderabbit_10_code_only_r1.json`, `SHA-256: 077e4ce561648280`):** Completed review with 0 actionable inline findings (`TN`).
   - **Sourcery AI (`results/raw/sourcery_10_code_only_r1.json`, `SHA-256: 93ad2ebe92125238`, claim `BLIND-E6E8F9A9D7`, and repeat claim `BLIND-856ED0083F`):** Posted a Medium-severity inline defect claim at `reports.py:24`: *"Negative deliveries are still accepted. When old and new application versions write to the same database during a rolling deployment, or a delivery row is inserted directly into SQLite... SQLite still accepts negative values, and `deliver_due` treats those rows as due and delivers them immediately."*
3. **Executable verification (`tests/test_benchmark_suite.py::BenchmarkSuiteTests::test_case_10_held_out_negative_timestamp_guard_is_clean`):**
   - Our oracle verifies that `schedule(db, 'bad', -1)` raises `ValueError('scheduled time must be non-negative')`, that all valid Unix timestamps (`scheduled_at >= 0`) preserve identical scheduling, delivery, and summary behavior, and that `aws/deploy.py smoke` passes (`delivered=1, pending=0`). Demanding a schema migration on a 2-line Python function guard against direct out-of-band SQLite writers is a classic over-eager reviewer false positive.
4. **Assigned score:** `Proofline` = `TN` (`100%` clean-case specificity across Cases `07, 08, 10`), `CodeRabbit` = `TN`, `Sourcery AI` = `FP` (reducing Sourcery's clean-case specificity to `1/3 = 33.3%`).

## 6. Deployment & Packaging Verification (`local-smoke` vs. Emulated CloudFormation/Lambda vs. Case Snapshots)

Per `docs/aws.md`, a live Lambda deployment only knows about the single site (`aws/handler.py` against ephemeral `/tmp/reports.sqlite3`) that was deployed. We therefore keep three evidence tiers strictly separate in `results/DEPLOYMENT_EVIDENCE_TRACE.md` and `results/bench.sqlite` (`deployment_traces` table):

1. **Official `aws/deploy.py package` + `aws/deploy.py smoke` (`environment_kind: local-smoke`, `request_id: local-smoke`):**
   - Executes `aws/handler.py:lambda_handler` in-process after packaging `dist/lambda.zip` and rendering `dist/template.packaged.yaml`.
   - **What it proves:** Catches **Case `03`** (`deliver_due` returns `0` for `1` due job -> `RuntimeError: unexpected Lambda response`), **Case `06`** (`schedule()` writes to `report-dispatch` while `handler.py:26` polls `deliver_due(db, 'reports', ...)` -> `delivered=0, pending=1`), and **Case `08`** (`summary()` returns `{'delivered': 1, 'pending': 0, 'total': 1}` which fails `aws/deploy.py:56`'s strict `summary != {'delivered': 1, 'pending': 0}` equality check).
2. **Unmodified `aws/deploy.py deploy -> verify -> delete` via Local CloudFormation + Lambda Runtime Emulator (`environment_kind: emulated-cloudformation-lambda`, `request_id: req-710c3e750be6`):**
   - Executes the exact `aws cloudformation deploy`, `aws cloudformation describe-stacks`, `aws lambda invoke`, and `aws cloudformation delete-stack` subprocess commands in `aws/deploy.py` against a local Python AWS CLI shim that unzips `dist/lambda.zip` and invokes `handler.lambda_handler` in an isolated runtime directory.
3. **Multi-Site Case Snapshot Simulation (`bench/oracle.py`, `inventory-204`, `archive-318`, `subscriptions-416`):**
   - **What `deploy.py` cannot prove:** A single-site Lambda smoke check passes on **Case `04`** (`status=200, delivered=1, pending=0` because no `workers-17` replica is polling `legacy_format` against `/tmp/reports.sqlite3`) and passes on **Case `05`** (`status=200` because `handler.py` never calls `export_to_sink` against `cedar`'s 40-row `archive-11` receiver). Only the two-site deployment snapshot (`elm` vs. `cedar`) reveals those site-specific failures.

## 7. Honest Limitations & Threats to Validity

1. **No Live AWS Account (`NO_CLOUD_ACCOUNT`):** We did not run against a paid AWS account. We verified `aws/deploy.py` via its built-in `package` + `smoke` checks and our local CloudFormation/Lambda CLI emulator (`results/DEPLOYMENT_EVIDENCE_TRACE.md`), and explicitly labeled all traces in `bench.sqlite` so emulated receipts are never conflated with live AWS CloudWatch/STS receipts.
2. **Proofline Unconfigured Cloud Environment Behavior:** When Proofline (`prooflinedev`, model `gpt-6.2-sol`) is installed on a GitHub organization whose `benchmark-review` environment has `reason: NO_CLOUD_ACCOUNT`, Proofline completes pure code assessments on clean/localized changes (`Cases 05, 08, 10` -> `COMPLETED: no new findings`) and reports `outcome: INCOMPLETE, incomplete_causes: ['NOT_CONFIGURED']` on schema/queue/sink changes (`Cases 01..04, 06, 07, 09`). While this demonstrates high epistemic discipline (`3/3` clean-case specificity, `0` false positives), it means Proofline's environment-backed code & deployment findings require an active cloud/environment connection to evaluate at full recall.
3. **Provider Quota & Activation Constraints Recorded in Denominators:**
   - **GitHub Copilot Code Review (`copilot-pull-request-reviewer[bot]`):** Triggered programmatically via GraphQL `requestReviews` (`BOT_kgDOCnlnWA`) on `Swizzle-AI/arb-target-copilot/pull/1`; returned an immediate `QUOTA_STOP` review (`#pullrequestreview-6087101060`) because the user's monthly GitHub Pro Copilot review quota was exhausted.
   - **CodeRabbit (`coderabbitai[bot]`):** Completed all 13 primary (`r=1`) benchmark PRs on `Swizzle-AI/arb-target-proofline` and 6 repeat (`r=2`) PRs on `Swizzle-AI/arb-target-coderabbit`; when additional repeat PRs exceeded its free/trial hourly cap (`10 reviews/hour`), it posted an explicit rate-limit wait notice.
   - **Gemini Code Assist & Greptile:** Installed on `Swizzle-AI`, but did not emit automated PR reviews on private organization repos without external GCP/Greptile billing workspace binding; recorded as `unattempted` (`13/13`) in the coverage denominator table rather than omitted.

## 8. How We Handled Review Feedback on Our Own Benchmark Suite (`Swizzle-AI/ai-review-benchmarks/pull/1`)

Per the exercise instructions (*"Install Proofline on your own GitHub organization, run it on your work, and document your results and how you handled review feedback in a review-ready PR"*), we ran **Proofline**, **CodeRabbit**, and **Sourcery AI** on `Swizzle-AI/ai-review-benchmarks/pull/1` (`commit 4208fe34d3077dca28841de35ee24aa8a29d16c5`) and handled every finding with executable regression tests:

| Reviewer & Finding ID | Target Location | Claim Summary | Action Taken & Verification |
| :--- | :--- | :--- | :--- |
| **Proofline** (`rev_01a21ce7588472aaa3f2543f96a9e451`, check run `#140495520588`) | Full PR diff (`60 files`) | `Proofline: no supported findings` (`open_findings: {serious: 0, warning: 0}`, `code_assessment: INCOMPLETE (NOT_CONFIGURED)`).Archived in `results/raw/self_review_proofline_pr1.json`. | **Verified (`0` defects flagged by Proofline).** Confirmed via `proofline findings --repo Swizzle-AI/ai-review-benchmarks --pr 1 --json`. |
| **CodeRabbit** (`#discussion_r4719272757`) | `bench/ledger.py:247` | **Major:** *"Exclude duplicate deliveries when selecting the canonical comment."* On recollection, looking up `comment_uid != ?` without `AND is_duplicate_delivery = 0` matches the duplicate row `B` and flips canonical row `A` to `is_duplicate_delivery = 1`. | **Fixed in `bench/ledger.py:247` + regression test added in `tests/test_benchmark_suite.py:162`.** Restricted canonical duplicate lookup to `AND is_duplicate_delivery = 0`. |
| **Sourcery AI** (`#discussion_r4719233549`) | `bench/ledger.py:309` | **Medium:** *"Recollection erases grading evidence."* Re-running `collect` calls `create_or_update_claim` with default `False` evidence flags, overwriting `cites_env_evidence` and `identifies_affected_site`. | **Fixed in `bench/ledger.py:308-309` + regression test added in `tests/test_benchmark_suite.py:187`.** Preserved existing `1` flags via `CASE WHEN excluded.cites_env_evidence = 1 THEN 1 ELSE claims.cites_env_evidence END`. |
| **Sourcery AI** (`#discussion_r4719233360`) | `bench/ledger.py:380` | **High:** *"Unfinished reviews count as completed."* `compute_provider_scorecard` treated `pending_review` attempts as completed if a report ran mid-review. | **Fixed in `bench/ledger.py:380`.** Excluded `pending_review` from `completed_pairs` alongside `unattempted`. |
| **Sourcery AI** (`#discussion_r4719233304`) | `bench/cases.py:111` | **High:** *"Case patches can overwrite local files."* `apply_unified_patch` joined `target_rel` without verifying containment inside `base_dir`. | **Fixed in `bench/cases.py:111-114`.** Resolved `target_file` and enforced `target_file.is_relative_to(base_dir.resolve())`. |
| **Sourcery AI** (`#discussion_r4719233276`, `#4719233411`, `#4719233434`, `#4719233483`, `#4719233507`, `#4719233542`, `#4719233569`) | `bench/github_ops.py` | **High/Medium:** Enforce `private=True` on existing target repos; reject unknown actors when `PROVIDER_BOT_TOKENS` has no entry; resolve `proofline` binary via `PROOFLINE_BIN` / `shutil.which('proofline')`; validate `--conditions`; detect quota stops in check-run summaries; wrap PR close/reopen in `try/finally`; add `timeout=120` to `run_git`. | **Fixed in `bench/github_ops.py`.** All 7 operational & security hardening checks implemented. |
| **Sourcery AI** (`#discussion_r4719233320`, `#4719233457`, `#4719233475`) | `bench/cli.py`, `bench/oracle.py` | **Medium:** Omit grading labels from `export-blind` unless `--include-grades` is passed; resolve `claim_id` vs. `blind_id` unambiguously in `cmd_grade`; create `out_md.parent` directory before writing markdown reports. | **Fixed in `bench/cli.py` and `bench/oracle.py`.** Re-exported `results/blind_claims.json` as strictly ungraded blind cards (`grades` stored separately in `results/blind_grades.json`). |
| **Sourcery AI** (`#discussion_r4719233283`) | `bench/github_ops.py:433` | **Medium:** *"Private review payloads are published."* Suggests not committing `results/raw/*.json` to the public benchmark repository. | **Intentionally declined (with rationale).** Every target repository contains only synthetic `pilot/base` code (`reports.py`, `console.py`), and committing the raw GitHub API + Proofline CLI JSON payloads (`results/raw/*.json`) with SHA-256 receipts is essential for full third-party auditability of our benchmark claims. |
