# Operator's Guide: Adding a Provider, Recovering Runs, and Auditing Scores

This guide shows another engineer how to operate the benchmark end-to-end, add a new AI code review provider, recover from an interrupted batch run, and trace any score back to the exact GitHub review payload and executable verification test.

---

## 1. Exact Commands Used to Operate the Benchmark

### 1.1 Verify All 10 Cases Against the Ground-Truth Oracle & Two-Site Simulator
```bash
python -m unittest discover -s tests -v
python -m bench verify-cases
python -m bench aws-trace
```
- Writes `results/oracle_ground_truth.json`, `results/ORACLE_GROUND_TRUTH.md`, and `results/DEPLOYMENT_EVIDENCE_TRACE.md`.

### 1.2 Initialize the SQLite Ledger & Register a New Provider
```bash
python -m bench init-db

# Add a new provider (e.g., Qodo Merge / Korbit / Cursor Bugbot)
python -m bench add-provider \
  --provider-id qodo-merge \
  --display-name "Qodo Merge (PR-Agent)" \
  --kind github_app \
  --plan "Free Open-Source / Trial" \
  --free-tier-notes "$0 trial on isolated private repo" \
  --learning-control "Isolated pilot/base repo Swizzle-AI/arb-target-qodo-merge"
```

### 1.3 Provision an Isolated `pilot/base` Repository & Open Benchmark PRs
```bash
# Dry-run first to inspect planned (case, condition) attempts
python -m bench provision --org Swizzle-AI --provider proofline --dry-run

# Execute provisioning (creates private repo Swizzle-AI/arb-target-<provider> seeded ONLY with pilot/base)
python -m bench provision --org Swizzle-AI --provider proofline --conditions code_only,with_snapshot
```

### 1.4 Recover from an Interrupted Run (`--resume`)
Every provisioned PR is committed immediately to `results/bench.sqlite` (`attempts` table) before sleeping or advancing to the next case. If a run is interrupted by a network failure, Ctrl-C, or GitHub secondary rate limit after Case `05`, simply re-run the exact same command:
```bash
python -m bench provision --org Swizzle-AI --provider proofline --conditions code_only,with_snapshot
```
The harness queries `attempts` where `pr_number IS NOT NULL AND terminal_status != 'unattempted'`, prints `[resume] Skipping already-provisioned ...`, and resumes at Case `06` without creating duplicate branches or PRs.

### 1.5 Trigger, Collect, Deduplicate, and Blind-Grade Reviews
```bash
# Optional: explicitly trigger or re-trigger a bot on open PRs
python -m bench trigger --provider coderabbit

# Collect all PR reviews, inline diff comments, issue comments, and check runs
# Writes immutable raw JSON payloads to results/raw/<attempt_id>.json with SHA-256 hashes
python -m bench collect --provider proofline

# Export provider-blind claim cards (bot names, avatars, and URLs stripped -> BLIND-<hash>)
python -m bench export-blind --output results/blind_claims.json

# Record a verified claim classification linked to an executable unit test
python -m bench grade \
  --claim-id BLIND-8F4A2C190B \
  --label ENV-TP \
  --env-cited \
  --site-identified \
  --test-name test_case_04_drop_column_breaks_elm_workers17_not_cedar \
  --rationale "Correctly identified that workers-17 on site elm polls legacy_format (inventory-204) while cedar succeeds."

# Generate the complete provider-by-case denominator matrix and scorecard
python -m bench report
```

---

## 2. Tracing Any Score Back to the Original Review & Executable Test

Every score cell in `results/BENCHMARK_REPORT.md` traces through a 4-link chain in `results/bench.sqlite`:
1. **Scorecard Cell (`matrix[provider][condition][case_id]`):** Lists the `pr_number` and `claims` labels.
2. **Attempt Record (`attempts.attempt_id`):** Links to the immutable GitHub API snapshot `results/raw/<attempt_id>.json` verified by `raw_payload_sha256`, plus `base_sha` and `head_sha`.
3. **Deduplicated Comment (`raw_comments.comment_uid`):** Stores the verbatim comment `body`, `author_login`, `created_at`, `html_url`, and `is_duplicate_delivery` flag (`body_norm_sha256` ensures webhook redeliveries are never double-counted).
4. **Blinded Claim & Verification Test (`claims.blind_id` -> `claims.verification_test`):** Links directly to the deterministic test in `tests/test_benchmark_suite.py` and `bench/oracle.py` that executes the patch and proves whether the claim is true (`TP` / `ENV-TP` / `ENV-ASK`) or false (`FP`).
