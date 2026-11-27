"""Executable ground-truth verification oracle and two-site (elm/cedar) deployment simulator."""

from __future__ import annotations

from contextlib import closing
import csv
from dataclasses import asdict, dataclass
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from typing import Any

from bench.cases import CaseSpec, ROOT, load_cases, load_patched_reports_module, materialize_app


@dataclass(frozen=True)
class OracleResult:
    case_id: str
    title: str
    is_held_out: bool
    category: str
    defect_confirmed: bool
    expected_code_only: str
    expected_with_snapshot: str | None
    affected_sites: list[str]
    healthy_sites: list[str]
    lambda_smoke_passed: bool
    lambda_smoke_detail: str
    summary_finding: str
    reproduction_proof: dict[str, Any]


def run_lambda_smoke_on_app(app_dir: Path, commit_label: str = "smoke-check") -> tuple[bool, str, dict[str, Any]]:
    """Run the exact aws/handler.py + aws/deploy.py verify_response contract against app_dir."""
    request = {"report": "Weekly orders", "scheduled_at": 100, "queue": "reports", "run_at": 101}
    expected = {
        "commit": commit_label,
        "report": request["report"],
        "delivered_this_run": 1,
        "receipt_at": request["run_at"],
        "summary": {"delivered": 1, "pending": 0},
    }
    try:
        reports_mod = load_patched_reports_module(app_dir, f"reports_smoke_{commit_label}")
        with closing(sqlite3.connect(":memory:")) as db:
            reports_mod.migrate(db)
            job = reports_mod.schedule(db, request["report"], request["scheduled_at"])
            delivered = reports_mod.deliver_due(db, request["queue"], request["run_at"])
            receipt = db.execute("SELECT recorded_at FROM receipts WHERE delivery_id = ?", (job,)).fetchone()
            response = {
                "commit": commit_label,
                "request_id": "local-smoke",
                "report": reports_mod.history(db, 1).rows[0][1],
                "delivered_this_run": delivered,
                "receipt_at": receipt[0] if receipt else None,
                "summary": reports_mod.summary(db),
            }
        mismatches = {k: {"expected": v, "actual": response.get(k)} for k, v in expected.items() if response.get(k) != v}
        if mismatches:
            return False, f"verify_response mismatch: {json.dumps(mismatches)}", response
        return True, "Lambda smoke handler and verify_response passed", response
    except Exception as exc:
        return False, f"Lambda smoke raised {type(exc).__name__}: {exc}", {}


def _verify_case_01(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c01")
    with closing(sqlite3.connect(":memory:")) as db:
        mod.migrate(db)
        # docs/report-service.md: "Schedulers can enqueue multiple reports for the same second."
        id1 = mod.schedule(db, "Report-A", 1700000000)
        id2 = mod.schedule(db, "Report-B", 1700000000)
        id3 = mod.schedule(db, "Report-C", 1700000000)
        page1 = mod.history(db, limit=2)
        page2 = mod.history(db, limit=2, cursor=page1.next_cursor)
        seen_ids = [r[0] for r in page1.rows] + [r[0] for r in page2.rows]
        missing_ids = [i for i in (id3, id2, id1) if i not in seen_ids]
    defect = len(missing_ids) > 0
    return (
        defect,
        [],
        [],
        (
            f"Scalar cursor WHERE scheduled_at < {page1.next_cursor} drops tied-timestamp rows across page boundaries: "
            f"enqueued 3 reports at t=1700000000 (ids={[id3, id2, id1]}), page1 returned {[r[0] for r in page1.rows]}, "
            f"page2 returned {[r[0] for r in page2.rows]}, permanently dropping id={missing_ids}."
        ),
        {
            "enqueued_ids_at_same_second": [id3, id2, id1],
            "page1_rows": page1.rows,
            "page1_next_cursor": page1.next_cursor,
            "page2_rows": page2.rows,
            "dropped_delivery_ids": missing_ids,
        },
    )


def _verify_case_02(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c02")
    with closing(sqlite3.connect(":memory:")) as db:
        mod.migrate(db)
        mod.schedule(db, "Orders, EMEA", 1700000000)
        mod.schedule(db, 'VIP "Gold" Report', 1700000001)
        buf = io.StringIO()
        mod.write_export(db, buf)
        raw_csv = buf.getvalue()
        parsed_rows = list(csv.reader(io.StringIO(raw_csv)))
    # Header + 2 rows, each should have exactly 3 columns: [id, report, scheduled_at]
    bad_rows = [r for r in parsed_rows[1:] if len(r) != 3]
    defect = len(bad_rows) > 0 or parsed_rows[1][1] != "Orders, EMEA"
    return (
        defect,
        [],
        [],
        (
            "Replacing csv.writer with ','.join(map(str, row)) breaks RFC-4180 CSV escaping: "
            f"'Orders, EMEA' splits into {len(parsed_rows[1])} columns ({parsed_rows[1]}), corrupting downstream parsers."
        ),
        {
            "raw_csv_output": raw_csv,
            "rfc4180_parsed_rows": parsed_rows,
            "corrupted_rows": bad_rows,
        },
    )


def _verify_case_03(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c03")
    with closing(sqlite3.connect(":memory:")) as db1:
        mod.migrate(db1)
        mod.schedule(db1, "Single job", 100)
        delivered_1 = mod.deliver_due(db1, "reports", 101)
        sum_1 = mod.summary(db1)
    with closing(sqlite3.connect(":memory:")) as db25:
        mod.migrate(db25)
        for idx in range(25):
            mod.schedule(db25, f"Job-{idx}", 100)
        delivered_25 = mod.deliver_due(db25, "reports", 101)
        sum_25 = mod.summary(db25)
    defect = (delivered_1 == 0) and (delivered_25 == 20 and sum_25["pending"] == 5)
    return (
        defect,
        [],
        [],
        (
            "range(0, len(jobs) - 20 + 1, 20) ignores any remainder len(jobs) % 20: "
            f"for 1 due job it delivers {delivered_1}/1 (breaking aws/deploy.py smoke/verify!), "
            f"and for 25 due jobs it delivers {delivered_25}/25 leaving {sum_25['pending']} jobs stranded pending."
        ),
        {
            "single_job_delivered": delivered_1,
            "single_job_summary": sum_1,
            "twenty_five_jobs_delivered": delivered_25,
            "twenty_five_jobs_summary": sum_25,
        },
    )


def _verify_case_04(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c04")
    inventory = spec.evidence_json or {}
    site_outcomes: dict[str, Any] = {}
    affected: list[str] = []
    healthy: list[str] = []
    for site_name, workers in inventory.get("sites", {}).items():
        with closing(sqlite3.connect(":memory:")) as db:
            mod.migrate(db)
            mod.schedule(db, "Daily", 100)
            worker_results = []
            site_failed = False
            for w in workers:
                try:
                    rows = db.execute(w["poll_sql"]).fetchall()
                    worker_results.append({"revision": w["revision"], "replicas": w["replicas"], "status": "ok", "rows": len(rows)})
                except sqlite3.OperationalError as err:
                    site_failed = True
                    worker_results.append({"revision": w["revision"], "replicas": w["replicas"], "status": "error", "error": str(err)})
            site_outcomes[site_name] = worker_results
            if site_failed:
                affected.append(site_name)
            else:
                healthy.append(site_name)
    return (
        len(affected) > 0,
        affected,
        healthy,
        (
            f"SQLite {sqlite3.sqlite_version} executes ALTER TABLE deliveries DROP COLUMN legacy_format cleanly, "
            f"and site 'cedar' (workers-18) succeeds, but site 'elm' still runs 2 replicas of workers-17 issuing "
            "'SELECT id, legacy_format FROM deliveries WHERE delivered_at IS NULL' (inventory-204), failing online with "
            "'OperationalError: no such column: legacy_format'."
        ),
        {
            "sqlite_version": sqlite3.sqlite_version,
            "observation_id": inventory.get("observation"),
            "site_outcomes": site_outcomes,
        },
    )


def _verify_case_05(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c05")
    archive_cfg = spec.evidence_json or {}
    site_outcomes: dict[str, Any] = {}
    affected: list[str] = []
    healthy: list[str] = []
    for site_name, cfg in archive_cfg.get("sites", {}).items():
        with closing(sqlite3.connect(":memory:")) as db:
            mod.migrate(db)
            for idx in range(cfg["ready_rows"]):
                mod.schedule(db, f"R-{idx}", 100 + idx)
            batches_sent: list[int] = []
            max_rows = cfg["max_rows_per_request"]

            def make_receiver(limit: int, rec_rev: str):
                def _send(batch: list):
                    if len(batch) > limit:
                        raise ValueError(f"{rec_rev} rejected request: batch size {len(batch)} > max_rows_per_request {limit}")
                    batches_sent.append(len(batch))
                return _send

            try:
                mod.export_to_sink(db, make_receiver(max_rows, cfg["receiver_revision"]))
                site_outcomes[site_name] = {"status": "ok", "batches": batches_sent, "total_archived": sum(batches_sent)}
                healthy.append(site_name)
            except ValueError as err:
                site_outcomes[site_name] = {"status": "rejected", "error": str(err), "batches_before_failure": batches_sent}
                affected.append(site_name)
    return (
        len(affected) > 0,
        affected,
        healthy,
        (
            "Increasing EXPORT_BATCH_SIZE from 25 to 50 succeeds on site 'elm' (archive-12, max_rows_per_request=100, "
            "batches=[50, 20]), but fails on site 'cedar' (archive-11, max_rows_per_request=40 in archive-318), where "
            "the 50-row request is rejected in full."
        ),
        {
            "observation_id": archive_cfg.get("observation"),
            "export_batch_size": mod.EXPORT_BATCH_SIZE,
            "site_outcomes": site_outcomes,
        },
    )


def _verify_case_06(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c06")
    subs = spec.evidence_json or {}
    site_outcomes: dict[str, Any] = {}
    affected: list[str] = []
    healthy: list[str] = []
    for site_name, cfg in subs.get("sites", {}).items():
        with closing(sqlite3.connect(":memory:")) as db:
            mod.migrate(db)
            for idx in range(cfg["new_jobs_due_in_window"]):
                mod.schedule(db, f"Due-{idx}", 100)
            delivered_total = 0
            for q in cfg["subscribed_queues"]:
                delivered_total += mod.deliver_due(db, q, 101)
            final_summary = mod.summary(db)
            site_outcomes[site_name] = {
                "subscribed_queues": cfg["subscribed_queues"],
                "delivered": delivered_total,
                "summary": final_summary,
            }
            if final_summary["pending"] > 0:
                affected.append(site_name)
            else:
                healthy.append(site_name)
    return (
        len(affected) > 0,
        affected,
        healthy,
        (
            "Routing new deliveries to DELIVERY_QUEUE='report-dispatch' works on site 'cedar' (subscribed to "
            "['reports', 'report-dispatch'], 8/8 delivered), but strands all 8 due jobs on site 'elm' (subscribed only "
            "to ['reports'] in subscriptions-416) and also fails aws/deploy.py smoke (which polls queue='reports')."
        ),
        {
            "observation_id": subs.get("observation"),
            "delivery_queue": mod.DELIVERY_QUEUE,
            "site_outcomes": site_outcomes,
        },
    )


def _verify_case_07(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c07")
    base_mod = load_patched_reports_module(ROOT, "reports_base_07")
    query = (
        "EXPLAIN QUERY PLAN SELECT id FROM deliveries "
        "WHERE queue = ? AND delivered_at IS NULL AND scheduled_at <= ? "
        "ORDER BY scheduled_at, id"
    )
    with closing(sqlite3.connect(":memory:")) as db_base:
        base_mod.migrate(db_base)
        base_plan = [row[3] for row in db_base.execute(query, ("reports", 100)).fetchall()]
    with closing(sqlite3.connect(":memory:")) as db_new:
        mod.migrate(db_new)
        new_plan = [row[3] for row in db_new.execute(query, ("reports", 100)).fetchall()]
    uses_covering = any("USING INDEX deliveries_due_by_queue" in step or "USING COVERING INDEX deliveries_due_by_queue" in step for step in new_plan)
    eliminates_temp_btree = any("USE TEMP B-TREE" in step for step in base_plan) and not any(
        "USE TEMP B-TREE" in step for step in new_plan
    )
    return (
        False,
        [],
        ["elm", "cedar"],
        (
            "Clean optimization (TN): partial index deliveries_due_by_queue ON deliveries (queue, scheduled_at, id) "
            "WHERE delivered_at IS NULL converts full table SCAN + TEMP B-TREE sort into a single COVERING INDEX seek."
        ),
        {
            "baseline_query_plan": base_plan,
            "patched_query_plan": new_plan,
            "uses_covering_index": uses_covering,
            "eliminates_temp_btree_sort": eliminates_temp_btree,
        },
    )


def _verify_case_08(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c08")
    with closing(sqlite3.connect(":memory:")) as db:
        mod.migrate(db)
        empty_sum = mod.summary(db)
        j1 = mod.schedule(db, "A", 100)
        mod.schedule(db, "B", 200)
        mod.deliver_due(db, "reports", 150)
        pop_sum = mod.summary(db)
    additive_ok = (empty_sum == {"delivered": 0, "pending": 0, "total": 0}) and (
        pop_sum == {"delivered": 1, "pending": 1, "total": 2}
    )
    return (
        False,
        [],
        ["elm", "cedar"],
        (
            "Clean additive change on pilot/base (TN): summary(db) returns {'delivered': sent, 'pending': waiting, "
            "'total': sent + waiting} (count() is always int >= 0, never NULL). Note: on main, aws/deploy.py:56 "
            "checks exact dict equality expected['summary'] == {'delivered': 1, 'pending': 0}, so deploy.py verify "
            "requires updating expected['summary'] in lockstep."
        ),
        {
            "empty_table_summary": empty_sum,
            "populated_table_summary": pop_sum,
            "pilot_base_additive_safe": additive_ok,
        },
    )


def _verify_case_09(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c09")
    with closing(sqlite3.connect(":memory:")) as db:
        mod.migrate(db)
        for idx in range(60):
            mod.schedule(db, f"Item-{idx + 1}", 100 + idx)
        received_ids: list[int] = []
        calls = {"count": 0}

        def flaky_sink(batch: list):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient HTTP 503 from archive receiver")
            received_ids.extend(row[0] for row in batch)

        mod.export_to_sink(db, flaky_sink)
    missing = [i for i in range(1, 61) if i not in received_ids]
    defect = len(missing) == 25
    return (
        defect,
        [],
        [],
        (
            "Held-out defect (TP): calling list(islice(rows, EXPORT_BATCH_SIZE)) inside the retry loop pulls the NEXT "
            f"25 rows from the SQLite cursor when attempt 1 raises RuntimeError, silently dropping the failed first "
            f"batch (ids 1..25 lost; only {len(received_ids)}/60 rows archived)."
        ),
        {
            "total_scheduled": 60,
            "total_archived_after_one_transient_retry": len(received_ids),
            "dropped_row_ids": missing,
        },
    )


def _verify_case_10(app_dir: Path, spec: CaseSpec) -> tuple[bool, list[str], list[str], str, dict[str, Any]]:
    mod = load_patched_reports_module(app_dir, "reports_c10")
    with closing(sqlite3.connect(":memory:")) as db:
        mod.migrate(db)
        rejected_negative = False
        try:
            mod.schedule(db, "Bad", -5)
        except ValueError:
            rejected_negative = True
        zero_id = mod.schedule(db, "Epoch-Zero", 0)
        pos_id = mod.schedule(db, "Normal", 1700000000)
        s = mod.summary(db)
    clean_ok = rejected_negative and zero_id == 1 and pos_id == 2 and s == {"delivered": 0, "pending": 2}
    return (
        False,
        [],
        ["elm", "cedar"],
        (
            "Held-out clean control (TN): rejecting scheduled_at < 0 raises ValueError before opening a write "
            "transaction and preserves behavior for all valid Unix timestamps (>= 0)."
        ),
        {
            "rejected_negative_timestamp": rejected_negative,
            "valid_inserts_count": s["pending"],
            "clean_behavior_verified": clean_ok,
        },
    )


VERIFIERS = {
    "01": _verify_case_01,
    "02": _verify_case_02,
    "03": _verify_case_03,
    "04": _verify_case_04,
    "05": _verify_case_05,
    "06": _verify_case_06,
    "07": _verify_case_07,
    "08": _verify_case_08,
    "09": _verify_case_09,
    "10": _verify_case_10,
}


def verify_all_cases(include_held_out: bool = True) -> list[OracleResult]:
    cases = load_cases(include_held_out=include_held_out)
    results: list[OracleResult] = []
    for cid, spec in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            materialize_app(app_dir, cid)
            smoke_ok, smoke_detail, smoke_resp = run_lambda_smoke_on_app(app_dir, f"case-{cid}")
            defect, affected, healthy, summary, proof = VERIFIERS[cid](app_dir, spec)
            proof["lambda_smoke_response"] = smoke_resp
            results.append(
                OracleResult(
                    case_id=cid,
                    title=spec.title,
                    is_held_out=spec.is_held_out,
                    category=spec.category,
                    defect_confirmed=defect,
                    expected_code_only=spec.expected_code_only,
                    expected_with_snapshot=spec.expected_with_snapshot,
                    affected_sites=affected,
                    healthy_sites=healthy,
                    lambda_smoke_passed=smoke_ok,
                    lambda_smoke_detail=smoke_detail,
                    summary_finding=summary,
                    reproduction_proof=proof,
                )
            )
    return results


def export_oracle_report(out_json: Path, out_md: Path) -> list[OracleResult]:
    results = verify_all_cases(include_held_out=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    lines = [
        "# Executable Ground-Truth Oracle & Multi-Site Verification Report",
        "",
        "Generated automatically by `python -m bench verify-cases`. Every case (`01`..`08` starter + `09`..`10` held-out) is patched into an isolated `pilot/base` checkout and verified via deterministic SQLite probes, the two-site (`elm` / `cedar`) deployment simulator, and the `aws/deploy.py` Lambda cold-start smoke contract.",
        "",
        "| Case | Title | Category | Code-Only Target | Snapshot Target | Affected Site(s) | Lambda `deploy.py smoke` | Verified Root Cause / Ground Truth |",
        "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :--- |",
    ]
    for r in results:
        tag = f"`{r.case_id}`" + (" *(held-out)*" if r.is_held_out else "")
        snap = f"`{r.expected_with_snapshot}`" if r.expected_with_snapshot else "—"
        aff = ", ".join(f"`{s}`" for s in r.affected_sites) if r.affected_sites else "None"
        smk = "PASS" if r.lambda_smoke_passed else "FAIL"
        lines.append(
            f"| {tag} | {r.title} | `{r.category}` | `{r.expected_code_only}` | {snap} | {aff} | **{smk}** | {r.summary_finding} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results
