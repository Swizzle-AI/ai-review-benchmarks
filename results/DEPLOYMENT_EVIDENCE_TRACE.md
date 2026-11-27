# End-to-End CI, Packaging, Lambda Verification & Evidence Provenance Trace

Per `README.md` (*Working knowledge of the systems*) and `docs/aws.md` (*Keep those results distinct*), this report separates facts observed from:
1. **Live/Local Packaged Lambda Execution (`aws/deploy.py package` -> `smoke` & `deploy` -> `verify` -> `delete`)**
2. **Supplied Multi-Site Case Snapshots (`cases/04..06/evidence.md`: `inventory-204`, `archive-318`, `subscriptions-416`)**

## 1. Packaged Revision & Cold-Start Lambda Verification Receipts

- **Packaged Git Commit (`SOURCE_COMMIT`):** `79d03777827b88aa23ee9309ebefab5f9766cd09`
- **Generated CloudFormation Inline Template Size:** `5,736 bytes` (within CloudFormation's `51,200-byte` `ZipFile` limit enforced at `aws/deploy.py:49`)
- **Official `aws/deploy.py smoke` Receipt (`request_id: local-smoke`):**
```json
{
  "commit": "79d03777827b88aa23ee9309ebefab5f9766cd09",
  "request_id": "local-smoke",
  "report": "Weekly orders",
  "delivered_this_run": 1,
  "receipt_at": 101,
  "summary": {
    "delivered": 1,
    "pending": 0
  }
}
```
- **Unmodified `aws/deploy.py deploy -> verify -> delete` Lifecycle Receipt (Emulated CloudFormation + Lambda CLI, $0 No-AWS-Card Mode):**
  - `deploy`: `Deployed 79d03777827b88aa23ee9309ebefab5f9766cd09 to ai-review-bench in us-east-1.`
  - `verify` response:
```json
{
  "commit": "79d03777827b88aa23ee9309ebefab5f9766cd09",
  "request_id": "emulated-lambda-56b1e563-b945-478e-97d3-39fa7e87bb20",
  "report": "Weekly orders",
  "delivered_this_run": 1,
  "receipt_at": 101,
  "summary": {
    "delivered": 1,
    "pending": 0
  }
}
```
  - `delete`: `Removed ai-review-bench from us-east-1.`
  - `delete` (idempotency check): `ai-review-bench is absent in us-east-1.`

## 2. Which Cases Pass vs. Fail the Deployed Lambda Workload (`aws/handler.py` + `verify_response`)?

| Case | Proposed Change | `aws/deploy.py smoke/verify` | Why the Single-Request Lambda Probe Catches or Misses It |
| :---: | :--- | :---: | :--- |
| `01` | Page report history by scheduled time | **PASS** | Lambda smoke handler and verify_response passed |
| `02` | Stream the report export | **PASS** | Lambda smoke handler and verify_response passed |
| `03` | Batch queued deliveries | **FAIL** | verify_response mismatch: {"delivered_this_run": {"expected": 1, "actual": 0}, "receipt_at": {"expected": 101, "actual": null}, "summary": {"expected": {"delivered": 1, "pending": 0}, "actual": {"delivered": 0, "pending": 1}}} |
| `04` | Retire the legacy delivery column | **PASS** | Lambda smoke handler and verify_response passed |
| `05` | Increase export batch size | **PASS** | Lambda smoke handler and verify_response passed |
| `06` | Use the new delivery queue | **FAIL** | verify_response mismatch: {"delivered_this_run": {"expected": 1, "actual": 0}, "receipt_at": {"expected": 101, "actual": null}, "summary": {"expected": {"delivered": 1, "pending": 0}, "actual": {"delivered": 0, "pending": 1}}} |
| `07` | Index pending deliveries | **PASS** | Lambda smoke handler and verify_response passed |
| `08` | Include a total in the activity summary | **FAIL** | verify_response mismatch: {"summary": {"expected": {"delivered": 1, "pending": 0}, "actual": {"delivered": 1, "pending": 0, "total": 1}}} |
| `09` | Retry archive batches on transient sink errors | **PASS** | Lambda smoke handler and verify_response passed |
| `10` | Reject negative timestamps when scheduling reports | **PASS** | Lambda smoke handler and verify_response passed |

## 3. Provenance Separation: Packaged Lambda Probe vs. Supplied Case Snapshots

| Fact / Constraint | Provenance Source | Can `aws/handler.py` Observe It Alone? | Explanation |
| :--- | :--- | :---: | :--- |
| `SOURCE_COMMIT` embedded in inline Lambda `index.py` | `aws/deploy.py package` (`git show COMMIT:path`) | **Yes** | Materialized at cold start into `tempfile.mkdtemp()` and returned in `response['commit']`. |
| Single-job delivery broken when `len(jobs) < 20` (Case `03`) | Packaged Lambda invocation (`REQUEST` schedules 1 job) | **Yes (`delivered_this_run: 0 != 1`)** | `handler.py` schedules 1 job and runs `deliver_due`; `range(0, -19, 20)` executes 0 iterations. |
| Queue mismatch `'report-dispatch'` vs `'reports'` (Case `06`) | Packaged Lambda invocation + `subscriptions-416` snapshot | **Yes (for `REQUEST`) / Snapshot needed for `elm` vs `cedar`** | `REQUEST` polls `queue='reports'`, failing `verify_response`; snapshot `subscriptions-416` proves `cedar` subscribes to both queues while `elm` strands 8 due jobs. |
| Strict dict equality on `summary` (`{'delivered': 1, 'pending': 0}`) (Case `08`) | `aws/deploy.py:56` (`verify_response`) | **Yes (verifier contract)** | Hidden from reviewers seeing only `pilot/base`, where `reports.py` has no caller asserting exact 2-key dict equality. |
| Active `workers-17` replicas on site `elm` querying `legacy_format` (Case `04`) | Supplied snapshot `cases/04/evidence.md` (`inventory-204`) | **No** | `handler.py` runs an isolated in-memory SQLite DB (`:memory:`) with current code; only `inventory-204` reveals `elm`'s live `workers-17` replicas. |
| Site `cedar` `archive-11` rejecting batches `> 40` rows (Case `05`) | Supplied snapshot `cases/05/evidence.md` (`archive-318`) | **No** | `handler.py` never calls `export_to_sink`; only `archive-318` supplies per-site `max_rows_per_request` (`elm=100`, `cedar=40`). |
