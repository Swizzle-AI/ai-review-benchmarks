"""Zero-AWS CloudFormation + Lambda lifecycle verifier and deployment trace recorder.

Executes the unmodified `aws/deploy.py` (`package`, `smoke`, `deploy`, `verify`, `delete`)
pipeline both via `local-smoke` and via an isolated local CloudFormation + Lambda CLI shim
(when no live AWS account is configured), recording distinct evidence provenance for each tier.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import uuid

from bench.cases import ROOT
from bench.ledger import connect_ledger
from bench.oracle import verify_all_cases


MOCK_AWS_SCRIPT = '''"""Local CloudFormation + Lambda CLI emulator for testing unmodified aws/deploy.py lifecycle."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

STATE_FILE = Path(os.environ["BENCH_MOCK_AWS_STATE"]) if "BENCH_MOCK_AWS_STATE" in os.environ else Path(tempfile.gettempdir()) / "bench_mock_aws_state.json"

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"stacks": {}}

def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")

def main():
    args = sys.argv[1:]
    # Strip --no-cli-pager, --region X, --output json
    clean = []
    region = "us-east-1"
    i = 0
    while i < len(args):
        if args[i] == "--no-cli-pager":
            i += 1
        elif args[i] == "--region":
            region = args[i + 1]
            i += 2
        elif args[i] == "--output":
            i += 2
        else:
            clean.append(args[i])
            i += 1

    st = load_state()
    svc = clean[0]
    op = clean[1]

    def get_opt(flag):
        if flag in clean:
            return clean[clean.index(flag) + 1]
        return None

    if svc == "cloudformation":
        stack_name = get_opt("--stack-name")
        if op == "describe-stacks":
            # Match by stack name or StackId
            found = None
            for k, v in st["stacks"].items():
                if k == stack_name or v["StackId"] == stack_name:
                    found = v
                    break
            if not found:
                sys.stderr.write(f"An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id {stack_name} does not exist\\n")
                sys.exit(254)
            print(json.dumps({"Stacks": [found]}))
            return
        elif op in ("create-stack", "update-stack"):
            tbody = get_opt("--template-body")
            tpath = Path(tbody.removeprefix("file://"))
            template = json.loads(tpath.read_text(encoding="utf-8"))
            tags_raw = get_opt("--tags")
            tags = json.loads(tags_raw) if tags_raw else [{"Key": "Application", "Value": "ai-review-benchmarks"}]
            stack_id = f"arn:aws:cloudformation:{region}:000000000000:stack/{stack_name}/{uuid.uuid4()}"
            st["stacks"][stack_name] = {
                "StackName": stack_name,
                "StackId": stack_id,
                "StackStatus": "CREATE_COMPLETE" if op == "create-stack" else "UPDATE_COMPLETE",
                "Tags": tags,
                "Template": template,
            }
            save_state(st)
            print(json.dumps({"StackId": stack_id}))
            return
        elif op == "delete-stack":
            target = None
            for k, v in list(st["stacks"].items()):
                if k == stack_name or v["StackId"] == stack_name:
                    target = k
            if target:
                del st["stacks"][target]
                save_state(st)
            return
        elif op == "wait":
            return

    elif svc == "lambda" and op == "invoke":
        fn_name = get_opt("--function-name")
        payload_str = get_opt("--payload")
        out_file = Path(clean[-1])
        stack = st["stacks"].get(fn_name)
        if not stack:
            sys.stderr.write(f"ResourceNotFoundException: Function not found: {fn_name}\\n")
            sys.exit(254)
        code = stack["Template"]["Resources"]["ReportFunction"]["Properties"]["Code"]["ZipFile"]
        req_id = f"emulated-lambda-{uuid.uuid4()}"
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "index.py"
            entry.write_text(code, encoding="utf-8")
            driver = """
import json, runpy, sys, tempfile
from types import SimpleNamespace
tempfile.tempdir, entry, payload, req_id = sys.argv[1:]
handler = runpy.run_path(entry)["handler"]
resp = handler(json.loads(payload), SimpleNamespace(aws_request_id=req_id))
print(json.dumps(resp))
"""
            child = __import__("subprocess").run(
                [sys.executable, "-I", "-c", driver, tmp, str(entry), payload_str, req_id],
                check=True, text=True, capture_output=True
            )
            out_file.write_text(child.stdout.strip(), encoding="utf-8")
        print(json.dumps({"StatusCode": 200, "ExecutedVersion": "$LATEST"}))
        return

    sys.stderr.write(f"Unsupported mock aws command: {args}\\n")
    sys.exit(2)

if __name__ == "__main__":
    main()
'''


def run_end_to_end_deploy_trace(db_path: Path, out_md: Path) -> dict[str, Any]:
    """Execute unmodified aws/deploy.py package -> smoke -> deploy -> verify -> delete and log receipts."""
    deploy_py = ROOT / "aws" / "deploy.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        template_path = tmp_dir / "review-bench-stack.json"

        # 1. Package exact checked-out git commit via aws/deploy.py package
        subprocess.run(
            [sys.executable, str(deploy_py), "package", "--output", str(template_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        template_bytes = template_path.stat().st_size

        # 2. Run official aws/deploy.py smoke (request_id == "local-smoke")
        smoke_proc = subprocess.run(
            [sys.executable, str(deploy_py), "smoke", "--template", str(template_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        smoke_receipt = json.loads(smoke_proc.stdout.strip())

        # 3. Run unmodified aws/deploy.py deploy -> verify -> delete via local CloudFormation+Lambda shim
        shim_py = tmp_dir / "aws_shim.py"
        shim_py.write_text(MOCK_AWS_SCRIPT, encoding="utf-8")
        shim_cmd = tmp_dir / "aws"
        shim_cmd.write_text(f'#!/usr/bin/env sh\nexec "{sys.executable}" "{shim_py}" "$@"\n', encoding="utf-8")
        try:
            shim_cmd.chmod(0o755)
        except OSError:
            pass

        env = os.environ.copy()
        env["PATH"] = f"{tmp_dir}{os.pathsep}{env.get('PATH', '')}"
        env["BENCH_MOCK_AWS_STATE"] = str(tmp_dir / "mock_aws_state.json")
        env["BENCH_AWS_SHIM_PY"] = str(shim_py)
        role_arn = "arn:aws:iam::000000000000:role/ai-review-bench-github-RuntimeRole"

        # On Windows CreateProcessW does not resolve extensionless 'aws' when shell=False,
        # so we transparently redirect ['aws', ...] to [sys.executable, shim_py, ...] while
        # executing the 100% unmodified aws/deploy.py file via runpy.
        runner_code = (
            "import os, runpy, subprocess, sys\n"
            "_orig_run = subprocess.run\n"
            "def _patched_run(cmd, *a, **kw):\n"
            "    if isinstance(cmd, list) and cmd and cmd[0] == 'aws':\n"
            "        cmd = [sys.executable, os.environ['BENCH_AWS_SHIM_PY'], *cmd[1:]]\n"
            "    return _orig_run(cmd, *a, **kw)\n"
            "subprocess.run = _patched_run\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )

        def run_deploy_py(*dargs: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-c", runner_code, str(deploy_py), *dargs],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

        deploy_proc = run_deploy_py(
            "deploy",
            "--region",
            "us-east-1",
            "--stack",
            "ai-review-bench",
            "--role-arn",
            role_arn,
        )
        verify_proc = run_deploy_py(
            "verify",
            "--region",
            "us-east-1",
            "--stack",
            "ai-review-bench",
        )
        emulated_receipt = json.loads(verify_proc.stdout.strip())
        delete_proc = run_deploy_py(
            "delete",
            "--region",
            "us-east-1",
            "--stack",
            "ai-review-bench",
        )
        delete_again_proc = run_deploy_py(
            "delete",
            "--region",
            "us-east-1",
            "--stack",
            "ai-review-bench",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = connect_ledger(db_path)
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO deployment_traces (
                trace_id, environment_kind, commit_sha, workflow_run_url,
                stack_name, request_id, response_json, recorded_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "deploy-py-local-smoke",
                "local-smoke",
                smoke_receipt["commit"],
                "local://aws/deploy.py#smoke",
                "ai-review-bench",
                smoke_receipt["request_id"],
                json.dumps(smoke_receipt),
                now_iso,
                f"Inline CloudFormation template size: {template_bytes} / 51200 bytes.",
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO deployment_traces (
                trace_id, environment_kind, commit_sha, workflow_run_url,
                stack_name, request_id, response_json, recorded_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "deploy-py-emulated-cfn-lambda",
                "emulated-cloudformation-lambda",
                emulated_receipt["commit"],
                "local://aws/deploy.py#deploy-verify-delete",
                "ai-review-bench",
                emulated_receipt["request_id"],
                json.dumps(emulated_receipt),
                now_iso,
                f"deploy={deploy_proc.stdout.strip()} | delete={delete_proc.stdout.strip()} | idempotent_delete={delete_again_proc.stdout.strip()}",
            ),
        )
    conn.close()

    oracle_results = verify_all_cases(include_held_out=True)
    trace_summary = {
        "template_bytes": template_bytes,
        "cloudformation_limit_bytes": 51200,
        "local_smoke_receipt": smoke_receipt,
        "emulated_deploy_verify_receipt": emulated_receipt,
        "deploy_stdout": deploy_proc.stdout.strip(),
        "delete_stdout": delete_proc.stdout.strip(),
        "idempotent_delete_stdout": delete_again_proc.stdout.strip(),
        "case_lambda_smoke_matrix": {
            r.case_id: {
                "passed": r.lambda_smoke_passed,
                "detail": r.lambda_smoke_detail,
            }
            for r in oracle_results
        },
    }

    md = [
        "# End-to-End CI, Packaging, Lambda Verification & Evidence Provenance Trace",
        "",
        "Per `README.md` (*Working knowledge of the systems*) and `docs/aws.md` (*Keep those results distinct*), this report separates facts observed from:",
        "1. **Live/Local Packaged Lambda Execution (`aws/deploy.py package` -> `smoke` & `deploy` -> `verify` -> `delete`)**",
        "2. **Supplied Multi-Site Case Snapshots (`cases/04..06/evidence.md`: `inventory-204`, `archive-318`, `subscriptions-416`)**",
        "",
        "## 1. Packaged Revision & Cold-Start Lambda Verification Receipts",
        "",
        f"- **Packaged Git Commit (`SOURCE_COMMIT`):** `{smoke_receipt['commit']}`",
        f"- **Generated CloudFormation Inline Template Size:** `{template_bytes:,} bytes` (within CloudFormation's `51,200-byte` `ZipFile` limit enforced at `aws/deploy.py:49`)",
        f"- **Official `aws/deploy.py smoke` Receipt (`request_id: local-smoke`):**",
        "```json",
        json.dumps(smoke_receipt, indent=2),
        "```",
        f"- **Unmodified `aws/deploy.py deploy -> verify -> delete` Lifecycle Receipt (Emulated CloudFormation + Lambda CLI, $0 No-AWS-Card Mode):**",
        "  - `deploy`: `" + deploy_proc.stdout.strip() + "`",
        "  - `verify` response:",
        "```json",
        json.dumps(emulated_receipt, indent=2),
        "```",
        "  - `delete`: `" + delete_proc.stdout.strip() + "`",
        "  - `delete` (idempotency check): `" + delete_again_proc.stdout.strip() + "`",
        "",
        "## 2. Which Cases Pass vs. Fail the Deployed Lambda Workload (`aws/handler.py` + `verify_response`)?",
        "",
        "| Case | Proposed Change | `aws/deploy.py smoke/verify` | Why the Single-Request Lambda Probe Catches or Misses It |",
        "| :---: | :--- | :---: | :--- |",
    ]
    for r in oracle_results:
        badge = "**PASS**" if r.lambda_smoke_passed else "**FAIL**"
        md.append(f"| `{r.case_id}` | {r.title} | {badge} | {r.lambda_smoke_detail} |")

    md.extend([
        "",
        "## 3. Provenance Separation: Packaged Lambda Probe vs. Supplied Case Snapshots",
        "",
        "| Fact / Constraint | Provenance Source | Can `aws/handler.py` Observe It Alone? | Explanation |",
        "| :--- | :--- | :---: | :--- |",
        "| `SOURCE_COMMIT` embedded in inline Lambda `index.py` | `aws/deploy.py package` (`git show COMMIT:path`) | **Yes** | Materialized at cold start into `tempfile.mkdtemp()` and returned in `response['commit']`. |",
        "| Single-job delivery broken when `len(jobs) < 20` (Case `03`) | Packaged Lambda invocation (`REQUEST` schedules 1 job) | **Yes (`delivered_this_run: 0 != 1`)** | `handler.py` schedules 1 job and runs `deliver_due`; `range(0, -19, 20)` executes 0 iterations. |",
        "| Queue mismatch `'report-dispatch'` vs `'reports'` (Case `06`) | Packaged Lambda invocation + `subscriptions-416` snapshot | **Yes (for `REQUEST`) / Snapshot needed for `elm` vs `cedar`** | `REQUEST` polls `queue='reports'`, failing `verify_response`; snapshot `subscriptions-416` proves `cedar` subscribes to both queues while `elm` strands 8 due jobs. |",
        "| Strict dict equality on `summary` (`{'delivered': 1, 'pending': 0}`) (Case `08`) | `aws/deploy.py:56` (`verify_response`) | **Yes (verifier contract)** | Hidden from reviewers seeing only `pilot/base`, where `reports.py` has no caller asserting exact 2-key dict equality. |",
        "| Active `workers-17` replicas on site `elm` querying `legacy_format` (Case `04`) | Supplied snapshot `cases/04/evidence.md` (`inventory-204`) | **No** | `handler.py` runs an isolated in-memory SQLite DB (`:memory:`) with current code; only `inventory-204` reveals `elm`'s live `workers-17` replicas. |",
        "| Site `cedar` `archive-11` rejecting batches `> 40` rows (Case `05`) | Supplied snapshot `cases/05/evidence.md` (`archive-318`) | **No** | `handler.py` never calls `export_to_sink`; only `archive-318` supplies per-site `max_rows_per_request` (`elm=100`, `cedar=40`). |",
    ])
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    return trace_summary
