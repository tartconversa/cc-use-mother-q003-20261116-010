import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from looplake.api import create_app
from looplake.fixtures import adjustment, balanced_batch, event, revocation, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def put(self, rows):
        return [self.service.ingest(row) for row in rows]

    def paid_july_batch(self):
        results = self.put(balanced_batch())
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        return results

    def test_paid_batch_has_zero_adjustment(self):
        self.paid_july_batch()
        batch = self.service.batch_status("batch-1")
        self.assertEqual(batch["settlement"]["expected_yuan"], "0.80")
        self.assertEqual(batch["settlement"]["actual_yuan"], "0.8")
        self.assertEqual(batch["settlement"]["adjustment_yuan"], "0")
        self.assertEqual(batch["settlement"]["net_yuan"], "0.8")
        self.assertEqual(batch["revoked_deposit_kg"], "0")
        row = self.service.month_report("2026-07")["rows"][0]
        self.assertEqual(row["adjustment_yuan"], "0")
        self.assertEqual(row["revoked_kg"], "0")

    def test_cross_month_revocation_then_full_clawback(self):
        results = self.paid_july_batch()
        deposit_fact_id = results[0]["fact_id"]
        preview = self.service.preview("2026-07")
        release = self.service.publish("july-final", "2026-07", preview["fingerprint"])

        revoked = self.service.ingest(revocation("rev-1", "a-deposit"))
        self.assertEqual(revoked["state"], "normalized")
        deposit = self.service.store.fact(deposit_fact_id)
        self.assertEqual(deposit["state"], "revoked")
        revocation_fact = self.service.store.fact(revoked["fact_id"])
        self.assertEqual(revocation_fact["day"], "2026-08-03")
        self.assertEqual(revocation_fact["original_day"], "2026-07-15")
        self.assertEqual(revocation_fact["target_fact_id"], deposit_fact_id)

        batch = self.service.batch_status("batch-1")
        self.assertFalse(batch["ready"])
        issue = next(x for x in batch["issues"] if x["code"] == "clawback_outstanding")
        self.assertEqual(issue["remaining_yuan"], "0.8")
        self.assertIn(revoked["fact_id"], issue["fact_ids"])
        settled = batch["settlement"]
        self.assertEqual((settled["expected_yuan"], settled["actual_yuan"]), ("0", "0.8"))
        self.assertEqual((settled["net_yuan"], settled["revoked_yuan"]), ("0.8", "0.80"))

        pending_july = self.service.month_report("2026-07")
        self.assertEqual(pending_july["rows"], [])
        self.assertEqual(pending_july["unresolved_batches"], ["batch-1"])
        self.assertIn(deposit_fact_id, pending_july["excluded_fact_ids"])
        draft = self.service.mart.month("2026-07", include_unresolved=True)
        self.assertEqual(draft["rows"][0]["revoked_kg"], "10")
        self.assertEqual(draft["rows"][0]["payout_yuan"], "0.8")
        self.assertFalse(self.service.month_status("2026-08")["ready"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("august-final", "2026-08",
                                 self.service.preview("2026-08")["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")

        adjusted = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        self.assertEqual(adjusted["state"], "normalized")
        adjustment_fact = self.service.store.fact(adjusted["fact_id"])
        self.assertEqual(adjustment_fact["day"], "2026-08-10")
        self.assertEqual(adjustment_fact["original_day"], "2026-07-15")
        self.assertEqual(adjustment_fact["actor_id"], "resident-1")
        self.assertEqual(adjustment_fact["batch_id"], "batch-1")
        self.assertEqual(adjustment_fact["target_fact_id"], revoked["fact_id"])

        batch = self.service.batch_status("batch-1")
        self.assertTrue(batch["ready"])
        self.assertEqual(batch["net_payout_yuan"], "0")
        july = self.service.month_report("2026-07")
        self.assertEqual(len(july["rows"]), 1)
        self.assertEqual(july["rows"][0]["deposit_kg"], "0")
        self.assertEqual(july["rows"][0]["revoked_kg"], "10")
        self.assertEqual(july["rows"][0]["payout_yuan"], "0.8")
        august = self.service.month_report("2026-08")
        self.assertEqual(len(august["rows"]), 1)
        self.assertEqual(august["rows"][0]["street_id"], "street-1")
        self.assertEqual(august["rows"][0]["adjustment_yuan"], "-0.8")
        self.assertEqual(august["rows"][0]["payout_yuan"], "0")
        self.assertEqual(august["rows"][0]["adjustment_events"], 1)
        self.assertIn(revoked["fact_id"], august["rows"][0]["fact_ids"])
        self.assertIn(adjusted["fact_id"], august["rows"][0]["fact_ids"])

        frozen = self.service.releases.get(release["release_id"])
        self.assertEqual(frozen["content"]["mart"]["rows"][0]["deposit_kg"], "10")
        self.assertEqual(frozen["content"]["mart"]["rows"][0]["payout_yuan"], "0.8")
        self.assertEqual(frozen["content"]["mart"]["rows"][0]["adjustment_yuan"], "0")

        august_release = self.service.publish("august-final", "2026-08",
                                              self.service.preview("2026-08")["fingerprint"])
        self.assertTrue(august_release["created"])
        usage = self.service.snapshots.fact_usage(adjusted["fact_id"])
        self.assertEqual(usage["releases"][0]["release_id"], august_release["release_id"])

    def test_partial_clawback_stays_pending_until_balanced(self):
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-0.5"))
        batch = self.service.batch_status("batch-1")
        self.assertFalse(batch["ready"])
        issue = next(x for x in batch["issues"] if x["code"] == "clawback_outstanding")
        self.assertEqual(issue["remaining_yuan"], "0.3")
        self.assertEqual(self.service.month_status("2026-08")["unresolved_batches"], ["batch-1"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("august-final", "2026-08",
                                 self.service.preview("2026-08")["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")
        self.service.ingest(adjustment("adj-2", "rev-1", "-0.3",
                                       happened_at="2026-08-12T10:00:00+08:00"))
        batch = self.service.batch_status("batch-1")
        self.assertTrue(batch["ready"])
        self.assertEqual(batch["settlement"]["net_yuan"], "0")
        flow = self.service.payment_flow("batch-1")
        self.assertEqual([row["kind"] for row in flow["entries"]],
                         ["payout", "revocation", "payout_adjustment", "payout_adjustment"])
        self.assertEqual(flow["net_paid_yuan"], "0")

    def test_over_clawback_is_flagged(self):
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-1"))
        batch = self.service.batch_status("batch-1")
        self.assertFalse(batch["ready"])
        issue = next(x for x in batch["issues"] if x["code"] == "clawback_overpaid")
        self.assertEqual(issue["excess_yuan"], "0.2")

    def test_adjustment_before_revocation_waits_for_review(self):
        self.paid_july_batch()
        early = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        self.assertEqual(early["state"], "quarantined")
        self.assertEqual(early["issue"]["code"], "unknown_revocation")
        quality = self.service.month_status("2026-08")
        self.assertFalse(quality["ready"])
        self.assertEqual([row["id"] for row in quality["quarantined"]],
                         [early["ingestion_id"]])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("august-final", "2026-08",
                                 self.service.preview("2026-08")["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")
        self.service.ingest(revocation("rev-1", "a-deposit"))
        retried = self.service.retry(early["ingestion_id"])
        self.assertEqual(retried["state"], "normalized")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        self.assertEqual(self.service.month_report("2026-08")["rows"][0]["adjustment_yuan"], "-0.8")

    def test_replay_same_events_never_double_charges(self):
        self.paid_july_batch()
        first_revocation = self.service.ingest(revocation("rev-1", "a-deposit"))
        first_adjustment = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        replayed_revocation = self.service.ingest(revocation("rev-1", "a-deposit"))
        replayed_adjustment = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        self.assertFalse(replayed_revocation["created"])
        self.assertFalse(replayed_adjustment["created"])
        self.assertEqual(replayed_revocation["fact_id"], first_revocation["fact_id"])
        self.assertEqual(replayed_adjustment["fact_id"], first_adjustment["fact_id"])
        batch_facts = self.service.ledger.batch("batch-1")
        self.assertEqual(len([f for f in batch_facts if f["event_type"] == "revocation"]), 1)
        self.assertEqual(len([f for f in batch_facts if f["event_type"] == "payout_adjustment"]), 1)
        settled = self.service.batch_status("batch-1")["settlement"]
        self.assertEqual((settled["adjustment_yuan"], settled["net_yuan"]), ("-0.8", "0"))
        conflict = revocation("rev-1", "a-deposit", happened_at="2026-08-04T10:00:00+08:00")
        with self.assertRaises(LakeError) as failure:
            self.service.ingest(conflict)
        self.assertEqual(failure.exception.code, "event_conflict")

    def test_manifest_replay_is_idempotent(self):
        rows = balanced_batch() + [revocation("rev-1", "a-deposit"),
                                   adjustment("adj-1", "rev-1", "-0.8")]
        first = self.service.batches.run({"events": rows})
        self.assertTrue(all(item["state"] == "normalized" for item in first["outcomes"]))
        second = self.service.batches.run({"events": rows})
        self.assertTrue(all(not item["created"] for item in second["outcomes"]))
        self.assertEqual(self.service.batch_status("batch-1")["settlement"]["net_yuan"], "0")
        self.assertEqual(len(self.service.payment_flow("batch-1")["entries"]), 3)

    def test_second_revocation_of_same_deposit_is_pending(self):
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        duplicate = self.service.ingest(revocation("rev-2", "a-deposit",
                                                   happened_at="2026-08-05T10:00:00+08:00"))
        self.assertEqual(duplicate["state"], "quarantined")
        self.assertEqual(duplicate["issue"]["code"], "already_revoked")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        self.assertEqual(self.service.batch_status("batch-1")["settlement"]["net_yuan"], "0")
        self.assertFalse(self.service.month_status("2026-08")["ready"])

    def test_revocation_without_payout_needs_no_clawback(self):
        self.put(balanced_batch(payout=None))
        self.service.ingest(revocation("rev-1", "a-deposit"))
        batch = self.service.batch_status("batch-1")
        self.assertTrue(batch["ready"])
        self.assertEqual(batch["settlement"]["net_yuan"], "0")
        row = self.service.month_report("2026-07")["rows"][0]
        self.assertEqual((row["deposit_kg"], row["revoked_kg"]), ("0", "10"))

    def test_revocation_before_deposit_is_retried_later(self):
        early = self.service.ingest(revocation("rev-1", "a-deposit"))
        self.assertEqual(early["state"], "quarantined")
        self.assertEqual(early["issue"]["code"], "unknown_target")
        self.paid_july_batch()
        pending = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        self.assertEqual(pending["issue"]["code"], "unknown_revocation")
        self.assertEqual(self.service.retry(early["ingestion_id"])["state"], "normalized")
        self.assertEqual(self.service.retry(pending["ingestion_id"])["state"], "normalized")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        self.assertEqual(self.service.batch_status("batch-1")["settlement"]["net_yuan"], "0")

    def test_target_mismatch_and_invalid_target_quarantined(self):
        self.paid_july_batch()
        wrong_device = self.service.ingest(revocation("rev-1", "a-deposit", device_id="box-2"))
        self.assertEqual(wrong_device["issue"]["code"], "target_mismatch")
        wrong_batch = self.service.ingest(revocation("rev-2", "a-deposit", batch_id="batch-2"))
        self.assertEqual(wrong_batch["issue"]["code"], "target_mismatch")
        not_deposit = self.service.ingest(revocation("rev-3", "a-scale",
                                                     target_source="scale-feed"))
        self.assertEqual(not_deposit["issue"]["code"], "invalid_target")
        too_early = self.service.ingest(revocation("rev-4", "a-deposit",
                                                   happened_at="2026-07-14T10:00:00+08:00"))
        self.assertEqual(too_early["issue"]["code"], "invalid_sequence")

    def test_payee_mismatch_quarantined(self):
        self.service.register_dimension({"kind": "actor", "external_id": "resident-2",
                                         "name": "他人", "valid_from": "2026-01-01"})
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        wrong_payee = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8",
                                                     actor_id="resident-2"))
        self.assertEqual(wrong_payee["state"], "quarantined")
        self.assertEqual(wrong_payee["issue"]["code"], "payee_mismatch")

    def test_event_validation(self):
        self.paid_july_batch()
        for payload, code in (
            (adjustment("adj-1", "rev-1", "0"), "invalid_adjustment_yuan"),
            (adjustment("adj-1", "rev-1", "NaN"), "invalid_adjustment_yuan"),
            (adjustment("adj-1", "rev-1", "-0.1234567"), "invalid_adjustment_yuan"),
            (adjustment("adj-1", "rev-1", "-0.8", target_source=None), "missing_target"),
            (revocation("rev-1", None), "missing_target"),
            (event("deposit", "d-x", adjustment_yuan="-1"), "invalid_event"),
            (revocation("rev-1", "a-deposit", weight_kg="1"), "invalid_event"),
            (adjustment("adj-1", "rev-1", "-0.8", channel="scale"), "invalid_channel"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(LakeError) as failure:
                    self.service.ingest(payload)
                self.assertEqual(failure.exception.code, code)

    def test_payment_flow_preserves_traceability(self):
        results = self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        adjusted = self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        flow = self.service.payment_flow("batch-1")
        self.assertEqual(flow["paid_yuan"], "0.8")
        self.assertEqual(flow["adjusted_yuan"], "-0.8")
        self.assertEqual(flow["net_paid_yuan"], "0")
        payout_entry, revocation_entry, adjustment_entry = flow["entries"]
        self.assertEqual(payout_entry["payee"], "resident-1")
        self.assertEqual(payout_entry["business_day"], "2026-07-15")
        self.assertEqual(revocation_entry["processing_day"], "2026-08-03")
        self.assertEqual(revocation_entry["business_day"], "2026-07-15")
        self.assertEqual(revocation_entry["original_day"], "2026-07-15")
        self.assertEqual(revocation_entry["target_fact_id"], results[0]["fact_id"])
        self.assertEqual(adjustment_entry["payee"], "resident-1")
        self.assertEqual(adjustment_entry["processing_day"], "2026-08-10")
        self.assertEqual(adjustment_entry["original_day"], "2026-07-15")
        self.assertEqual(adjustment_entry["amount_yuan"], "-0.8")
        self.assertEqual(adjustment_entry["net_paid_yuan"], "0")
        audits = self.service.lineage.audit_subject(str(results[0]["fact_id"]))
        self.assertIn("deposit_revoked", [row["action"] for row in audits])
        lineage = self.service.lineage.fact(adjusted["fact_id"])
        self.assertEqual(lineage["fact"]["batch_id"], "batch-1")

    def test_street_view_consistent_across_months(self):
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        street = self.service.mart.street("street-1", "2026-07", "2026-08")
        self.assertEqual(len(street["rows"]), 2)
        july, august = street["rows"]
        self.assertEqual((july["payout_yuan"], july["revoked_kg"]), ("0.8", "10"))
        self.assertEqual(august["adjustment_yuan"], "-0.8")
        batch = self.service.batch_status("batch-1")
        self.assertEqual(batch["settlement"]["net_yuan"], "0")
        self.assertTrue(self.service.month_status("2026-07")["ready"])
        self.assertTrue(self.service.month_status("2026-08")["ready"])

    def test_same_month_revocation_and_clawback(self):
        self.paid_july_batch()
        self.service.ingest(revocation("rev-1", "a-deposit",
                                       happened_at="2026-07-20T10:00:00+08:00"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-0.8",
                                       happened_at="2026-07-25T10:00:00+08:00"))
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        row = self.service.month_report("2026-07")["rows"][0]
        self.assertEqual((row["deposit_kg"], row["revoked_kg"]), ("0", "10"))
        self.assertEqual((row["payout_yuan"], row["adjustment_yuan"]), ("0.8", "-0.8"))

    def test_frozen_release_survives_later_revision(self):
        self.paid_july_batch()
        first = self.service.publish("july-1", "2026-07",
                                     self.service.preview("2026-07")["fingerprint"])
        self.service.ingest(revocation("rev-1", "a-deposit"))
        self.service.ingest(adjustment("adj-1", "rev-1", "-0.8"))
        revised = self.service.publish("july-2", "2026-07",
                                       self.service.preview("2026-07")["fingerprint"])
        frozen = self.service.releases.get(first["release_id"])
        self.assertEqual(frozen["content"]["mart"]["rows"][0]["deposit_kg"], "10")
        current = self.service.releases.get(revised["release_id"])
        self.assertEqual(current["content"]["mart"]["rows"][0]["revoked_kg"], "10")
        diff = self.service.releases.compare(first["release_id"], revised["release_id"])
        self.assertEqual(len(diff["changes"]), 1)
        self.assertEqual(diff["changes"][0]["before"]["revoked_kg"], "0")
        self.assertEqual(diff["changes"][0]["after"]["revoked_kg"], "10")


class RevocationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / "api.sqlite3"))
        service = self.client.app.state.service
        seed_dimensions(service)
        self.service = service

    def test_revocation_flow_over_http(self):
        for row in balanced_batch():
            self.assertEqual(self.client.post("/events", json=row).status_code, 200)
        posted = self.client.post("/events", json=revocation("rev-1", "a-deposit"))
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.json()["state"], "normalized")
        posted = self.client.post("/events", json=adjustment("adj-1", "rev-1", "-0.8"))
        self.assertEqual(posted.status_code, 200)
        flow = self.client.get("/batches/batch-1/payments")
        self.assertEqual(flow.status_code, 200)
        self.assertEqual(flow.json()["net_paid_yuan"], "0")
        self.assertEqual([row["kind"] for row in flow.json()["entries"]],
                         ["payout", "revocation", "payout_adjustment"])
        replay = self.client.post("/events", json=adjustment("adj-1", "rev-1", "-0.8"))
        self.assertEqual(replay.json()["created"], False)
        self.assertEqual(self.client.get("/batches/batch-1/payments").json()["net_paid_yuan"], "0")
        bad = self.client.post("/events", json=adjustment("adj-2", "rev-1", "abc"))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["code"], "invalid_adjustment_yuan")

    def test_payments_endpoint_unknown_batch(self):
        response = self.client.get("/batches/ghost/payments")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "unknown_batch")
