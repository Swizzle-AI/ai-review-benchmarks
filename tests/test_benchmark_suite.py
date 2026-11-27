"""Comprehensive unit tests for the AI Review Benchmarks ground-truth oracle, site simulator, and SQLite ledger."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bench.aws_trace import run_end_to_end_deploy_trace
from bench.cases import load_cases
from bench.ledger import (
    anonymize_text,
    compute_provider_scorecard,
    connect_ledger,
    create_or_update_claim,
    ingest_raw_comment,
    register_provider,
    upsert_attempt,
)
from bench.oracle import verify_all_cases


class TestGroundTruthOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {r.case_id: r for r in verify_all_cases(include_held_out=True)}

    def test_all_ten_cases_loaded_and_verified(self) -> None:
        self.assertEqual(sorted(self.results.keys()), ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10"])

    def test_case_01_tie_timestamp_keyset_pagination_bug(self) -> None:
        r = self.results["01"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TP")
        self.assertEqual(r.reproduction_proof["dropped_delivery_ids"], [1])
        self.assertTrue(r.lambda_smoke_passed)

    def test_case_02_manual_csv_join_escaping_bug(self) -> None:
        r = self.results["02"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TP")
        self.assertGreater(len(r.reproduction_proof["corrupted_rows"]), 0)
        self.assertTrue(r.lambda_smoke_passed)

    def test_case_03_batch_range_remainder_truncation_bug(self) -> None:
        r = self.results["03"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TP")
        self.assertEqual(r.reproduction_proof["single_job_delivered"], 0)
        self.assertEqual(r.reproduction_proof["twenty_five_jobs_delivered"], 20)
        self.assertFalse(r.lambda_smoke_passed)

    def test_case_04_drop_column_breaks_elm_workers17_not_cedar(self) -> None:
        r = self.results["04"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "ENV-ASK")
        self.assertEqual(r.expected_with_snapshot, "ENV-TP")
        self.assertEqual(r.affected_sites, ["elm"])
        self.assertEqual(r.healthy_sites, ["cedar"])
        self.assertTrue(r.lambda_smoke_passed)

    def test_case_05_batch_50_rejected_on_cedar_not_elm(self) -> None:
        r = self.results["05"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "ENV-ASK")
        self.assertEqual(r.expected_with_snapshot, "ENV-TP")
        self.assertEqual(r.affected_sites, ["cedar"])
        self.assertEqual(r.healthy_sites, ["elm"])
        self.assertTrue(r.lambda_smoke_passed)

    def test_case_06_new_queue_strands_elm_and_fails_harness_smoke(self) -> None:
        r = self.results["06"]
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "ENV-ASK")
        self.assertEqual(r.expected_with_snapshot, "ENV-TP")
        self.assertEqual(r.affected_sites, ["elm"])
        self.assertEqual(r.healthy_sites, ["cedar"])
        self.assertFalse(r.lambda_smoke_passed)

    def test_case_07_partial_covering_index_is_clean_improvement(self) -> None:
        r = self.results["07"]
        self.assertFalse(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TN")
        self.assertTrue(r.reproduction_proof["uses_covering_index"])
        self.assertTrue(r.reproduction_proof["eliminates_temp_btree_sort"])
        self.assertTrue(r.lambda_smoke_passed)

    def test_case_08_summary_total_additive_on_app_strict_on_deploy_smoke(self) -> None:
        r = self.results["08"]
        self.assertFalse(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TN")
        self.assertTrue(r.reproduction_proof["pilot_base_additive_safe"])
        # On main, aws/deploy.py:56 checks exact dict equality summary == {'delivered': 1, 'pending': 0}
        self.assertFalse(r.lambda_smoke_passed)

    def test_case_09_held_out_retry_reslices_iterator_and_drops_batch(self) -> None:
        r = self.results["09"]
        self.assertTrue(r.is_held_out)
        self.assertTrue(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TP")
        self.assertEqual(r.reproduction_proof["total_archived_after_one_transient_retry"], 35)
        self.assertEqual(len(r.reproduction_proof["dropped_row_ids"]), 25)

    def test_case_10_held_out_negative_timestamp_guard_is_clean(self) -> None:
        r = self.results["10"]
        self.assertTrue(r.is_held_out)
        self.assertFalse(r.defect_confirmed)
        self.assertEqual(r.expected_code_only, "TN")
        self.assertTrue(r.reproduction_proof["clean_behavior_verified"])
        self.assertTrue(r.lambda_smoke_passed)


class TestLedgerAndScoring(unittest.TestCase):
    def test_webhook_deduplication_and_blind_anonymization_and_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test_bench.sqlite"
            conn = connect_ledger(db_path)
            register_provider(
                conn,
                "proofline",
                "Proofline",
                "github_app",
                "free-trial",
                "$0",
                "isolated",
            )
            aid = upsert_attempt(
                conn,
                provider_id="proofline",
                case_id="01",
                condition="code_only",
                repeat_index=1,
                repo_full_name="Swizzle-AI/svc-proofline",
                pr_number=1,
                base_sha="9b5ac64",
                head_sha="b422577",
                trigger_kind="auto_on_pr_open",
                terminal_status="completed",
            )
            # First delivery is new; second webhook redelivery with identical text is deduplicated
            first_new = ingest_raw_comment(
                conn,
                comment_uid="c-100",
                attempt_id=aid,
                source_kind="review_comment",
                author_login="proofline[bot]",
                created_at="2026-11-27T08:00:00Z",
                body="Proofline found that WHERE scheduled_at < ? drops tied timestamps across pages. See https://proofline.sh/r/1",
            )
            second_new = ingest_raw_comment(
                conn,
                comment_uid="c-101-redelivery",
                attempt_id=aid,
                source_kind="review_comment",
                author_login="proofline[bot]",
                created_at="2026-11-27T08:00:02Z",
                body="Proofline  found that WHERE scheduled_at < ? drops tied timestamps across pages. See https://proofline.sh/r/1",
            )
            self.assertTrue(first_new)
            self.assertFalse(second_new)
            # Re-ingesting the canonical comment on recollection must preserve its canonical status (is_duplicate_delivery = 0)
            re_ingest_canonical = ingest_raw_comment(
                conn,
                comment_uid="c-100",
                attempt_id=aid,
                source_kind="review_comment",
                author_login="proofline[bot]",
                created_at="2026-11-27T08:00:00Z",
                body="Proofline found that WHERE scheduled_at < ? drops tied timestamps across pages. See https://proofline.sh/r/1",
            )
            self.assertTrue(re_ingest_canonical)

            blind_id = create_or_update_claim(
                conn,
                claim_id=f"{aid}:claim1",
                attempt_id=aid,
                comment_uid="c-100",
                raw_text="Proofline found that WHERE scheduled_at < ? drops tied timestamps across pages. See https://proofline.sh/r/1",
                classification="TP",
                cites_env_evidence=True,
                identifies_affected_site=True,
                verification_test="test_case_01_tie_timestamp_keyset_pagination_bug",
                grading_rationale="Matches tie-timestamp keyset loss reproduction.",
                graded_by="blind-grader-1",
            )
            # Re-running claim creation during recollection with default false flags must not erase existing graded evidence flags
            create_or_update_claim(
                conn,
                claim_id=f"{aid}:claim1",
                attempt_id=aid,
                comment_uid="c-100",
                raw_text="Proofline found that WHERE scheduled_at < ? drops tied timestamps across pages. See https://proofline.sh/r/1",
            )
            preserved = conn.execute("SELECT cites_env_evidence, identifies_affected_site FROM claims WHERE blind_id = ?", (blind_id,)).fetchone()
            self.assertEqual(preserved["cites_env_evidence"], 1)
            self.assertEqual(preserved["identifies_affected_site"], 1)
            self.assertTrue(blind_id.startswith("BLIND-"))
            anon = anonymize_text("Proofline and CodeRabbitai linked https://github.com/org/repo/pull/1")
            self.assertNotIn("Proofline", anon)
            self.assertNotIn("CodeRabbit", anon)
            self.assertIn("[REVIEWER]", anon)

            scorecard = compute_provider_scorecard(conn)
            p_score = scorecard["providers"]["proofline"]
            self.assertEqual(p_score["completed_pairs"], 1)
            self.assertEqual(p_score["code_defect_recall"], "1/1")
            self.assertEqual(p_score["claim_precision"], "1/1")
            conn.close()

    def test_end_to_end_aws_deploy_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "trace.sqlite"
            md_path = Path(tmp) / "TRACE.md"
            summary = run_end_to_end_deploy_trace(db_path, md_path)
            self.assertEqual(summary["local_smoke_receipt"]["request_id"], "local-smoke")
            self.assertTrue(summary["emulated_deploy_verify_receipt"]["request_id"].startswith("emulated-lambda-"))
            self.assertEqual(summary["emulated_deploy_verify_receipt"]["summary"], {"delivered": 1, "pending": 0})
            self.assertFalse(summary["case_lambda_smoke_matrix"]["03"]["passed"])
            self.assertFalse(summary["case_lambda_smoke_matrix"]["06"]["passed"])
            self.assertFalse(summary["case_lambda_smoke_matrix"]["08"]["passed"])
            self.assertTrue(md_path.exists())


if __name__ == "__main__":
    unittest.main()
