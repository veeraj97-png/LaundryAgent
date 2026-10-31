"""OFFLINE: dated capacity, immutable approval, legacy migration and restart."""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import laundry_agent as agent
import scheduling
import shop
import storage
from test_date_helpers import TEST_DATE
from test_laundry_agent import FakeModel, call, reply, database_rows


class PickupDateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="laundry-dated-offline-")))
        self.business = self.root / "business.db"
        self.checkpoint = self.root / "checkpoint.db"
        self.enterContext(patch.object(storage, "DATABASE_PATH", self.business))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("OFFLINE")))
        storage.initialize_database()
        self.day = TEST_DATE
        self.other = (date.fromisoformat(self.day) + timedelta(days=1)).isoformat()

    def prepare(self, day=None):
        return shop.prepare_booking_proposal("wash_and_iron", 5, "morning", day or self.day)

    def approve(self, proposal):
        shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "approved")

    def capacity(self, day):
        return shop.check_pickup_slot("morning", day)["remaining_capacity"]

    def test_dates_are_required_valid_and_not_past(self):
        before = database_rows(self.business)
        invalid = [None, "", "tomorrow", "2026-2-01", "2026-02-30", "2025-02-29", 123,
                   (scheduling.business_today() - timedelta(days=1)).isoformat()]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    shop.prepare_booking_proposal("wash_and_iron", 5, "morning", value)
        self.assertEqual(database_rows(self.business), before)
        self.assertEqual(scheduling.validate_pickup_date("2028-02-29", allow_past=True), "2028-02-29")

    def test_date_scoped_availability_is_read_only_and_independent(self):
        before = database_rows(self.business)
        self.assertEqual(self.capacity(self.day), 2)
        self.assertEqual(self.capacity(self.other), 2)
        self.assertEqual(database_rows(self.business), before)
        p = self.prepare(); self.approve(p)
        booking = shop.create_booking(p)
        self.assertEqual(self.capacity(self.day), 1)
        self.assertEqual(self.capacity(self.other), 2)
        self.assertEqual(storage.get_booking(booking["booking_id"])["pickup_date"], self.day)
        self.assertEqual(database_rows(self.business)["pickup_slots"], before["pickup_slots"])

    def test_afternoon_default_books_without_resetting_existing_capacities(self):
        legacy_afternoon = next(slot for slot in storage.read_pickup_slots()
                                if slot["slot_code"] == "afternoon")
        self.assertEqual(legacy_afternoon["remaining_capacity"], 2)
        with sqlite3.connect(self.business) as connection:
            connection.execute("INSERT INTO dated_pickup_slots VALUES (?, 'afternoon', 0)", (self.other,))
        before = database_rows(self.business)
        self.assertEqual(shop.check_pickup_slot("afternoon", self.day)["remaining_capacity"], 2)
        self.assertEqual(database_rows(self.business), before)
        proposal = shop.prepare_booking_proposal("wash_and_iron", 5, "afternoon", self.day)
        self.approve(proposal)
        booking = shop.create_booking(proposal)
        after = database_rows(self.business)
        storage.initialize_database()
        self.assertEqual(database_rows(self.business), after)
        self.assertEqual(shop.check_pickup_slot("afternoon", self.day)["remaining_capacity"], 1)
        self.assertEqual(shop.check_pickup_slot("afternoon", self.other)["remaining_capacity"], 0)
        self.assertEqual(storage.get_booking(booking["booking_id"])["slot_code"], "afternoon")
        self.assertEqual(after["pickup_slots"], before["pickup_slots"])

    def test_date_change_requires_new_snapshot_and_fresh_approval(self):
        p = self.prepare(); self.approve(p)
        altered = deepcopy(p); altered["pickup"]["pickup_date"] = self.other
        before = database_rows(self.business)
        with self.assertRaisesRegex(ValueError, "do not match"):
            shop.create_booking(altered)
        with self.assertRaisesRegex(ValueError, "conflict"):
            storage.save_proposal(altered)
        self.assertEqual(database_rows(self.business), before)
        replacement = self.prepare(self.other)
        self.assertNotEqual(replacement["snapshot_id"], p["snapshot_id"])
        with self.assertRaisesRegex(ValueError, "not approved"):
            shop.create_booking(replacement)

    def test_failed_insert_rolls_back_new_date_row_and_all_business_state(self):
        p = self.prepare(); self.approve(p)
        with sqlite3.connect(self.business) as connection:
            connection.execute("CREATE TRIGGER reject_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT, 'forced date rollback'); END")
        before = database_rows(self.business)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced date rollback"):
            shop.create_booking(p)
        self.assertEqual(database_rows(self.business), before)
        self.assertEqual(before["dated_pickup_slots"], [])
        self.assertEqual(storage.get_proposal(p["request_id"])["status"], "approved")

    def test_completed_dated_retry_succeeds_after_pickup_date_passes(self):
        p = self.prepare(); self.approve(p)
        booking = shop.create_booking(p)
        before = database_rows(self.business)
        with patch.object(scheduling, "business_today", return_value=date.fromisoformat(self.other)):
            self.assertEqual(shop.create_booking(p), booking)
        self.assertEqual(database_rows(self.business), before)

    def test_unbooked_approved_date_that_expires_cannot_book(self):
        p = self.prepare(); self.approve(p)
        before = database_rows(self.business)
        with patch.object(scheduling, "business_today", return_value=date.fromisoformat(self.other)):
            with self.assertRaisesRegex(ValueError, "past"):
                shop.create_booking(p)
        self.assertEqual(database_rows(self.business), before)

    def test_graph_requires_date_and_clarifies_without_defaulting(self):
        raw = json.dumps({"service_code": "wash_and_iron", "quantity": 5, "slot_code": "morning"})
        model = FakeModel([reply(calls=[{"id": "missing-date", "type": "function", "function": {
            "name": "prepare_booking_proposal", "arguments": raw}}]), reply("What pickup date would you like?")])
        before = database_rows(self.business)
        with agent.open_agent(model, checkpoint_path=self.checkpoint) as graph:
            result = agent.run_turn(graph, "date-question", "Prepare wash and iron for five garments, morning pickup.")
        self.assertIn("What pickup date", result["messages"][-1]["content"])
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["error"]["code"], "validation_error")
        self.assertEqual(database_rows(self.business), before)
        self.assertIn("Current business date:", model.requests[0][0]["content"])

    def test_pending_restart_preserves_date_and_duplicate_resume_books_once(self):
        model = FakeModel([reply(calls=[call("prepare_booking_proposal", {
            "service_code": "wash_and_iron", "quantity": 5, "slot_code": "morning", "pickup_date": self.day})])])
        with agent.open_agent(model, checkpoint_path=self.checkpoint) as graph:
            agent.run_turn(graph, "dated", "Prepare five wash-and-iron garments, morning pickup on " + self.day)
            pending = agent.pending_approval(graph, "dated")
        with agent.open_agent(FakeModel([]), checkpoint_path=self.checkpoint) as graph:
            self.assertEqual(agent.pending_approval(graph, "dated"), pending)
            result = agent.resume_approval(graph, "dated", "approve", request_id=pending["request_id"], snapshot_id=pending["snapshot_id"])
            self.assertIn(self.day, result["messages"][-1]["content"])
            before = database_rows(self.business)
            self.assertEqual(agent.resume_approval(graph, "dated", "approve", request_id=pending["request_id"], snapshot_id=pending["snapshot_id"]), result)
        self.assertEqual(database_rows(self.business), before)
        self.assertEqual(self.capacity(self.day), 1)

    def test_legacy_schema_and_completed_requests_migrate_without_invented_dates(self):
        legacy_path = self.root / "old.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("CREATE TABLE orders (booking_id TEXT PRIMARY KEY,service_code TEXT,quantity INTEGER,total_price INTEGER,slot_code TEXT,status TEXT)")
            connection.execute("INSERT INTO orders VALUES ('old-id','wash_and_iron',5,250,'morning','confirmed')")
        with patch.object(storage, "DATABASE_PATH", legacy_path):
            storage.initialize_database(); storage.initialize_database()
            old = storage.get_booking("old-id")
            self.assertEqual(old["total_price"], 250)
            self.assertIsNone(old["pickup_date"])
            p = self.prepare()
            p["pickup"].pop("pickup_date"); p.pop("snapshot_id")
            booking = {**p, "booking_id": "old-id", "status": "confirmed"}
            with sqlite3.connect(legacy_path) as connection:
                connection.execute("INSERT INTO booking_requests VALUES (?,?,?,?)", (
                    p["request_id"], "old-id", storage._canonical_booking_details(booking), json.dumps(booking)))
            before = database_rows(legacy_path)
            self.assertEqual(storage.save_booking(booking), booking)
            self.assertEqual(database_rows(legacy_path), before)

    def test_legacy_approved_proposal_cannot_create_an_undated_order(self):
        p = self.prepare()
        p["pickup"].pop("pickup_date")
        details = storage._canonical_booking_details(p)
        with sqlite3.connect(self.business) as connection:
            # Simulate an old, approved immutable record predating this feature.
            connection.execute("INSERT INTO proposals VALUES (?,?,?,'approved',?,NULL,NULL,NULL)",
                               (str(uuid4()), "legacy-snapshot", details, "2026-01-01"))
            request = connection.execute("SELECT request_id FROM proposals WHERE snapshot_id='legacy-snapshot'").fetchone()[0]
        old = storage.get_proposal(request)
        before = database_rows(self.business)
        with self.assertRaisesRegex(ValueError, "pickup date"):
            shop.create_booking(old)
        self.assertEqual(database_rows(self.business), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
