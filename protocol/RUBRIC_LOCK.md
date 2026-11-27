# Pre-Registered Experimental Protocol & Scoring Rubric (`RUBRIC_LOCK`)

**Protocol Version:** `1.0.0-frozen`  
**Locked Before Provider Runs:** Yes (committed prior to opening target review pull requests)  
**Target Baseline Commit (`pilot/base`):** `9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a`  
**Starter Main Commit (`main`):** `79d03777827b88aa23ee9309ebefab5f9766cd09`

---

## 1. Research Questions

Per `docs/experiment.md`, an engineer reading our results must be able to answer two questions from the recorded artifacts alone:
1. **Equivalence & Isolation:** *Did these tools review the exact same change with the exact same information, without contamination from answer keys, test harnesses, or other tools' comments?*
2. **Operability & Reproducibility:** *Could another engineer add a new reviewer tomorrow, recover from an interrupted run, and trace every numerical score back to the exact original review comment and an executable verification test?*

---

## 2. Unit of Observation (`Attempt`)

Every single review attempt is stored as an immutable row in our SQLite ledger (`bench.sqlite`) and exported to canonical JSONL (`results/attempts.jsonl`) with the following primary fields:

- `attempt_id`: Deterministic UUIDv5 / slug `<provider>:<case_id>:<condition>:r<repeat_index>`
- `provider`: Canonical reviewer identifier (`proofline`, `coderabbit`, `gemini-code-assist`, `copilot`, `greptile`, `sourcery`, `codacy`, etc.)
- `provider_model_or_plan`: Visible model/tier string reported by the tool or check run (e.g., `free-trial-1mo`, `pro-trial-14d`, `copilot-pro`), or `"not_disclosed"`
- `case_id`: `"01"`..`"08"` (starter cases) or `"09"`..`"10"` (held-out probe cases authored after locking this rubric)
- `condition`:
  - `"code_only"`: Only the `pilot/base` application files (`README.md`, `console.py`, `reports.py`, `migrations/001_initial.sql`) plus the case patch and PR title/body.
  - `"with_snapshot"`: Applicable to Cases `04`, `05`, `06`. Supplies the exact deployment snapshot from `cases/<id>/evidence.md` uniformly in the PR body under a `## Deployment Snapshot` section across every compared provider.
- `repeat_index`: `1` for the primary evaluation batch; `2+` for intentional repeatability runs. Webhook redeliveries or bot edits within the same trigger window are deduplicated into the same `repeat_index`.
- `repo_full_name` & `pr_number`: The isolated private repository (`Swizzle-AI/<target-repo>`) and pull request number.
- `base_sha` & `head_sha`: Exact Git commit hashes reviewed.
- `trigger_kind`: `"auto_on_pr_open"`, `"manual_command"` (e.g. `@coderabbitai review`), or `"cli_or_api"`.
- `opened_at`, `first_response_at`, `completed_at`: ISO-8601 UTC timestamps.
- `terminal_status`:
  - `"completed"`: Reviewer finished and posted a review, inline finding(s), check-run summary, or explicit clean approval.
  - `"completed_quiet"`: Reviewer check run succeeded / completed with zero defect claims.
  - `"quota_stopped"`: Reviewer refused or truncated due to free-tier credit/trial exhaustion.
  - `"error"`: Reviewer crashed or timed out within the `15-minute` review window.
  - `"unattempted"`: Not run for this cell (always shown explicitly in the coverage denominator table).
- `raw_payload_sha256`: SHA-256 hash of the archived raw GitHub API JSON response (`results/raw/<attempt_id>.json`).

---

## 3. Contamination & Input Isolation Rules

1. **App-Only Target Repositories (`pilot/base`):**
   - Reviewers are **never** installed on the benchmark harness repository for case evaluation.
   - Each provider reviews an isolated private repository seeded strictly from `pilot/base` (`9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a`), which contains only `README.md`, `console.py`, `reports.py`, and `migrations/001_initial.sql`.
   - `cases/`, `aws/`, `docs/experiment.md`, `protocol/`, `bench/`, and `tests/` are **100% absent** from target review repositories.
2. **One Provider Per Target Repository (Zero Cross-Bot Comment Leakage):**
   - Each provider is installed on its own private target repository (or isolated PR branch where no other bot has commented) so Provider B cannot read Provider A's review thread on the PR.
3. **Cross-PR Memory / Learning Controls:**
   - Where a provider exposes settings for cross-PR learning, chat memory, or org-wide indexing, we disable them and record what was controllable vs. opaque in `results/provider_controls.json`.
4. **Held-Out Original Cases (`09` & `10`):**
   - Because `prooflinedev/ai-review-benchmarks` is a public repository that an LLM or provider index could have seen, we evaluate two original held-out changes (`Case 09` defect and `Case 10` clean control) authored against `pilot/base` using the exact same rules.

---

## 4. Pre-Registered Claim Classification & Scoring Rubric

Before inspecting any reviewer output, we define the ground-truth classification of every atomic claim extracted from a review (`bench/blind.py` anonymizes claims by stripping bot handles, avatars, and signature footers before human/secondary verification):

### 4.1 Atomic Claim Categories
- **`TP` (True Positive — Verified Code Defect):**
  - Identifies the primary root-cause bug in a defective case (`01`, `02`, `03`, `09`) with a correct causal explanation that matches our executable reproduction test.
- **`ENV-TP` (Environment-Grounded True Positive — `with_snapshot` on `04`, `05`, `06`):**
  - Correctly identifies the production failure **and** names the affected site (`elm` in Cases `04` & `06`; `cedar` in Case `05`), the unaffected site, and cites the snapshot evidence (`inventory-204`, `archive-318`, `subscriptions-416`).
- **`ENV-PARTIAL` (Environment Finding Without Exact Site/Evidence Citation):**
  - Flags the migration/batch/queue risk in `with_snapshot` mode but fails to distinguish which site (`elm` vs. `cedar`) breaks or fails to cite the snapshot record.
- **`ENV-ASK` (Appropriate Epistemic Caution — `code_only` on `04`, `05`, `06`):**
  - In `code_only` mode (where no snapshot is provided), accurately warns that safety cannot be determined from code alone and specifies the missing deployment fact needed (active worker queries polling `legacy_format` in `04`, downstream archive receiver batch limit in `05`, or active worker queue subscriptions in `06`).
- **`VALID-AUX` (Valid Auxiliary Observation):**
  - A factually true, non-hallucinated secondary observation (verified by test or SQLite behavior) that is not the primary seeded defect. Tracked separately so it neither inflates primary defect recall nor penalizes precision as a false positive.
- **`DUP` (Duplicate Claim):**
  - A repeated statement of the same defect within the same review attempt. Counted once for recall; tracked as redundancy (`duplicate_rate`).
- **`FP` (False Positive / Unsupported or Incorrect Warning):**
  - Claims a bug that our executable tests or SQLite `3.35+` semantics disprove (for example: claiming SQLite `3.35+` does not support `ALTER TABLE ... DROP COLUMN` in `04`, claiming SQLite partial indexes are invalid in `07`, or claiming `count(delivered_at)` returns `NULL` in `08`).
- **`FN` (False Negative / Missed Defect):**
  - Reviewer completed on a defective case (`01`, `02`, `03`, `09`, or `04–06` `with_snapshot`) without identifying the defect.
- **`TN` (True Negative / Correct Quiet or Approving Review on Clean Change):**
  - Reviewer completes on a clean change (`07`, `08` on `pilot/base`, `10`) with zero false positives (`FP = 0`).

### 4.2 Case-Level Ground-Truth Oracle Contract

| Case | Condition | Expected Ideal Outcome | Executable Verification Test (`bench verify-cases`) |
| :--- | :--- | :--- | :--- |
| **`01`** | `code_only` | **`TP`**: Scalar `scheduled_at < ?` cursor drops tied-timestamp rows across page boundaries when `ORDER BY scheduled_at DESC, id DESC`. | `test_case_01_tie_timestamp_pagination_drops_rows` |
| **`02`** | `code_only` | **`TP`**: Replacing `csv.writer` with `",".join(map(str, row))` breaks RFC-4180 escaping when `report` contains `,`, `"`, or `\n`. | `test_case_02_manual_csv_join_corrupts_commas_and_quotes` |
| **`03`** | `code_only` | **`TP`**: `range(0, len(jobs) - 20 + 1, 20)` drops all remainder jobs (`len(jobs) % 20`), delivering `0` jobs when `1 <= len(jobs) < 20` (breaking `deploy.py smoke`). | `test_case_03_batch_range_truncates_remainder_and_fails_smoke` |
| **`04`** | `code_only` | **`ENV-ASK`**: Dropping `legacy_format` is valid in SQLite 3.35+, safe in repo code, but requires checking live polling workers. | `test_case_04_drop_column_sqlite_valid_but_breaks_elm_workers17` |
| **`04`** | `with_snapshot` | **`ENV-TP`**: Safe on `cedar` (`workers-18`), breaks `elm` (`2` replicas of `workers-17` running `SELECT id, legacy_format ...` in `inventory-204`). | `test_case_04_drop_column_sqlite_valid_but_breaks_elm_workers17` |
| **`05`** | `code_only` | **`ENV-ASK`**: `EXPORT_BATCH_SIZE = 50` requires checking downstream archive sink request size limits. | `test_case_05_batch_50_succeeds_elm_rejected_on_cedar` |
| **`05`** | `with_snapshot` | **`ENV-TP`**: Succeeds on `elm` (`max_rows_per_request: 100`), rejected in full on `cedar` (`archive-11`, `max_rows_per_request: 40` in `archive-318`). | `test_case_05_batch_50_succeeds_elm_rejected_on_cedar` |
| **`06`** | `code_only` | **`ENV-ASK`**: Changing `DELIVERY_QUEUE = "report-dispatch"` requires checking worker queue subscriptions (and deploy harness `--queue` args). | `test_case_06_new_queue_strands_elm_jobs_and_fails_harness_queue` |
| **`06`** | `with_snapshot` | **`ENV-TP`**: `cedar` subscribes to `["reports", "report-dispatch"]` (delivers `8/8`), `elm` subscribes only to `["reports"]` (`8` due jobs stranded in `subscriptions-416`). | `test_case_06_new_queue_strands_elm_jobs_and_fails_harness_queue` |
| **`07`** | `code_only` | **`TN`**: Partial covering index `(queue, scheduled_at, id) WHERE delivered_at IS NULL` is valid and eliminates temp B-tree sort in `deliver_due`. | `test_case_07_partial_covering_index_matches_query_plan` |
| **`08`** | `code_only` | **`TN`** (or **`ENV-ASK`** on strict schema callers): Safe & additive on `pilot/base`; note that `main`'s `aws/deploy.py:56` checks exact dict equality `summary == {"delivered": 1, "pending": 0}`. | `test_case_08_summary_total_additive_on_app_strict_in_deploy_verify` |
| **`09`** *(Held-Out)* | `code_only` | **`TP`**: Retry loop re-Slices `islice(rows, EXPORT_BATCH_SIZE)` from exhausted iterator instead of retrying the failed `batch`, silently dropping 25 rows on transient error. | `test_case_09_held_out_retry_reslices_iterator_and_drops_batch` |
| **`10`** *(Held-Out)* | `code_only` | **`TN`**: Guard `if scheduled_at < 0: raise ValueError(...)` in `schedule()` is clean, behavior-preserving for valid epoch timestamps, and prevents negative times. | `test_case_10_held_out_non_negative_schedule_guard_is_clean` |

### 4.3 Primary & Secondary Aggregates
For every provider $p$, reported alongside **explicit raw denominators** ($n / N$ on shared completed cases):
1. **Defect Recall (`code_only` bugs `01, 02, 03, 09`):** $\frac{\text{Cases with } \ge 1 \text{ TP}}{\text{Completed defective cases}}$
2. **Environment Evidence Accuracy (`with_snapshot` `04, 05, 06`):** $\frac{\text{Cases with ENV-TP}}{\text{Completed snapshot cases}}$
3. **Epistemic Humility Rate (`code_only` `04, 05, 06`):** $\frac{\text{Cases with ENV-ASK and } 0 \text{ false certainty}}{\text{Completed env-dependent code-only cases}}$
4. **Clean-Case Specificity / Quiet Discipline (`07, 08, 10`):** $\frac{\text{Clean cases with } 0 \text{ FP}}{\text{Completed clean cases}}$
5. **Claim Precision:** $\frac{\text{TP} + \text{ENV-TP} + \text{ENV-ASK} + \text{VALID-AUX}}{\text{Total non-duplicate claims}}$
6. **Run-to-Run Consistency (`r=1` vs. `r=2`):** Jaccard overlap of case-level outcomes across repeat runs.
