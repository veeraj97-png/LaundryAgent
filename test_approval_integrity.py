"""Run with: python3 -m unittest -v test_approval_integrity

Every test uses a temporary database; subprocesses use temporary code copies.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import shop
import storage


TEST_EVIDENCE = []
REAL_CONNECT = sqlite3.connect


class ApprovalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laundry-approval-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        for name in ("shop.py", "storage.py", "check_order.py"):
            shutil.copy2(Path(__file__).parent / name, self.project / name)
        self.database = self.project / "laundry.db"
        self.database_patch = patch.object(storage, "DATABASE_PATH", self.database)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        storage.initialize_database()
        self.initial = self.snapshot()
        self.events = []

    def tearDown(self):
        TEST_EVIDENCE.append({"test": self.id(), "before": self.initial,
                              "after": self.snapshot(), "events": self.events})

    def sql(self, statement, parameters=()):
        connection = REAL_CONNECT(self.database)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def snapshot(self, connection=None):
        if connection is None:
            connection = REAL_CONNECT(self.database)
            try:
                return self.snapshot(connection)
            finally:
                connection.close()
        result = {}
        for table in ("orders", "pickup_slots", "booking_requests", "proposals"):
            cursor = sqlite3.Connection.execute(connection, f"SELECT * FROM {table} ORDER BY 1")
            names = [column[0] for column in cursor.description]
            result[table] = [dict(zip(names, row)) for row in cursor.fetchall()]
        return result

    def prepare(self, quantity=5):
        return shop.prepare_booking_proposal("wash_and_iron", quantity, "morning")

    def approve(self, proposal):
        return shop.record_proposal_decision(
            proposal["request_id"], proposal["snapshot_id"], "approved",
        )

    def candidate(self, proposal):
        return {"request_id": proposal["request_id"], "snapshot_id": proposal.get("snapshot_id"),
                "booking_id": str(uuid4()), "quote": deepcopy(proposal["quote"]),
                "pickup": deepcopy(proposal["pickup"]), "status": "confirmed"}

    def capacity(self, state=None):
        state = self.snapshot() if state is None else state
        return next(slot["remaining_capacity"] for slot in state["pickup_slots"]
                    if slot["slot_code"] == "morning")

    def assert_refused(self, function, message):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, message) as raised:
            function()
        self.assertEqual(self.snapshot(), before)
        self.events.append({"error": str(raised.exception), "unchanged": before})

    def subprocess(self, arguments, input_text=None, cwd=None):
        options = {"stdin": subprocess.DEVNULL} if input_text is None else {"input": input_text}
        result = subprocess.run([sys.executable, "-B", *arguments], cwd=cwd or self.project,
                                text=True, capture_output=True, timeout=8, **options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_preparation_persists_fixed_terms_without_booking(self):
        before = self.snapshot()
        first, second = self.prepare(), self.prepare()
        after = self.snapshot()
        for table in ("orders", "pickup_slots", "booking_requests"):
            self.assertEqual(after[table], before[table])
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        stored = storage.get_proposal(first["request_id"])
        self.assertEqual(stored["status"], "awaiting_approval")
        self.assertEqual(stored["quote"], first["quote"])
        self.assertEqual(stored["pickup"], {key: first["pickup"][key] for key in ("slot_code", "display_name")})
        self.assertTrue(stored["created_at"])
        self.assertIsNone(stored["decided_at"])
        self.assertIsNone(stored["booked_at"])

    def test_existing_id_never_overwrites_terms_or_resets_approval(self):
        proposal = self.prepare()
        self.approve(proposal)
        before = self.snapshot()
        saved = storage.save_proposal(proposal)
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(self.snapshot(), before)
        changed = deepcopy(proposal)
        changed["quote"]["quantity"] = 6
        self.assert_refused(lambda: storage.save_proposal(changed), "Proposal terms conflict")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.sql("UPDATE proposals SET business_details='{}' WHERE request_id=?", (proposal["request_id"],))
        self.assertEqual(self.snapshot(), before)

    def test_approval_survives_restart_and_unchanged_terms_book(self):
        proposal = self.prepare()
        approved = self.approve(proposal)
        self.assertEqual(proposal["status"], "awaiting_approval")
        self.assertTrue(approved["decided_at"])
        before = self.snapshot()
        output = self.subprocess(["-c", """
import json, sys
import shop, storage
p = storage.get_proposal(sys.argv[1])
assert p['status'] == 'approved'
print(json.dumps(shop.create_booking(p)))
""", proposal["request_id"]])
        booking = json.loads(output)
        after = self.snapshot()
        self.assertEqual(len(after["orders"]), len(before["orders"]) + 1)
        self.assertEqual(len(after["booking_requests"]), 1)
        self.assertEqual(self.capacity(after), self.capacity(before) - 1)
        self.assertEqual(booking["quote"], proposal["quote"])
        self.assertEqual(booking["snapshot_id"], proposal["snapshot_id"])
        stored = storage.get_proposal(proposal["request_id"])
        self.assertEqual(stored["status"], "booked")
        self.assertTrue(stored["booked_at"])
        self.events.append({"before_booking": before, "booking": booking, "after_booking": after})

    def test_editing_local_status_cannot_authorize_booking(self):
        proposal = self.prepare()
        proposal["status"] = "approved"
        self.assert_refused(lambda: shop.create_booking(proposal), "awaiting_approval, not approved")
        self.assert_refused(lambda: storage.save_booking(self.candidate(proposal), allow_new=True),
                            "awaiting_approval, not approved")

    def test_direct_storage_rejects_unknown_pending_rejected_superseded(self):
        for state in ("unknown", "awaiting_approval", "rejected", "superseded"):
            with self.subTest(state=state):
                proposal = self.prepare()
                if state == "unknown":
                    proposal["request_id"] = "unknown-request"
                    message = "Unknown proposal"
                elif state == "rejected":
                    shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "rejected")
                    message = "rejected, not approved"
                elif state == "superseded":
                    shop.supersede_proposal(proposal["request_id"], proposal["snapshot_id"])
                    message = "superseded, not approved"
                else:
                    message = "awaiting_approval, not approved"
                proposal["status"] = "approved"
                self.assert_refused(lambda: storage.save_booking(self.candidate(proposal)), message)
                self.assert_refused(lambda: shop.create_booking(proposal), message)

    def test_all_commercial_fields_are_enforced_before_and_after_booking(self):
        proposal = self.prepare()
        self.approve(proposal)
        mutations = [("quote", "service_code", "wash_and_fold"), ("quote", "service_name", "Changed"),
                     ("quote", "quantity", 6), ("quote", "unit_price", 51),
                     ("quote", "total_price", 251), ("quote", "turnaround_days", 4),
                     ("pickup", "slot_code", "evening"), ("pickup", "display_name", "Changed")]
        for completed in (False, True):
            if completed:
                shop.create_booking(proposal)
            for section, field, value in mutations:
                with self.subTest(completed=completed, field=field):
                    changed = deepcopy(proposal)
                    changed[section][field] = value
                    message = "Request ID conflict" if completed else "do not match"
                    self.assert_refused(lambda: shop.create_booking(changed), message)
                    self.assert_refused(lambda: storage.save_booking(self.candidate(changed)), message)

    def test_wrong_snapshot_cannot_record_decision_or_book(self):
        proposal = self.prepare()
        self.assert_refused(lambda: shop.record_proposal_decision(proposal["request_id"], "wrong", "approved"),
                            "Stale or mismatched")
        self.assert_refused(lambda: shop.record_proposal_decision("unknown", proposal["snapshot_id"], "approved"),
                            "Unknown proposal")
        self.assert_refused(lambda: shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "yes"),
                            "decision must be approved or rejected")
        self.approve(proposal)
        changed = deepcopy(proposal)
        changed["snapshot_id"] = "wrong"
        self.assert_refused(lambda: storage.save_booking(self.candidate(changed)), "do not match")

    def test_superseded_approval_cannot_authorize_replacement(self):
        old = self.prepare()
        self.approve(old)
        superseded = shop.supersede_proposal(old["request_id"], old["snapshot_id"])
        self.assertTrue(superseded["superseded_at"])
        self.assertEqual(shop.supersede_proposal(old["request_id"], old["snapshot_id"]), superseded)
        replacement = self.prepare(quantity=6)
        self.assertNotEqual(old["request_id"], replacement["request_id"])
        self.assert_refused(lambda: self.approve(old), "superseded")
        self.assert_refused(lambda: shop.create_booking(old), "superseded, not approved")
        self.assert_refused(lambda: shop.record_proposal_decision(replacement["request_id"], old["snapshot_id"], "approved"),
                            "Stale or mismatched")
        self.assert_refused(lambda: shop.create_booking(replacement), "awaiting_approval, not approved")
        self.approve(replacement)
        self.assertEqual(shop.create_booking(replacement)["quote"]["quantity"], 6)

    def test_decision_retries_and_rejection_transitions(self):
        proposal = self.prepare()
        approved = self.approve(proposal)
        before = self.snapshot()
        self.assertEqual(self.approve(proposal), approved)
        self.assertEqual(self.snapshot(), before)
        rejected = shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "rejected")
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(rejected["decided_at"])
        before = self.snapshot()
        self.assertEqual(shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "rejected"), rejected)
        self.assertEqual(self.snapshot(), before)
        self.assert_refused(lambda: self.approve(proposal), "rejected proposal needs a new proposal")
        self.assert_refused(lambda: shop.create_booking(proposal), "rejected, not approved")

    def test_completed_booking_cannot_be_undone_or_reauthorized(self):
        proposal = self.prepare()
        self.approve(proposal)
        booking = shop.create_booking(proposal)
        for decision in ("approved", "rejected"):
            self.assert_refused(lambda: shop.record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], decision),
                                "booked proposal cannot be changed")
        self.assert_refused(lambda: shop.supersede_proposal(proposal["request_id"], proposal["snapshot_id"]),
                            "booked proposal cannot be changed")
        before = self.snapshot()
        proposal["status"] = "rejected"  # Local status has no authority, even for a completed retry.
        self.assertEqual(shop.create_booking(proposal), booking)
        self.assertEqual(self.snapshot(), before)

    def test_booking_failure_rolls_back_even_a_written_booked_state(self):
        proposal = self.prepare()
        self.approve(proposal)
        self.sql("CREATE TRIGGER fail_mapping BEFORE INSERT ON booking_requests BEGIN SELECT RAISE(ABORT, 'forced mapping failure'); END")
        before = self.snapshot()
        observed = []
        test = self
        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if " ".join(sql.split()).upper().startswith("INSERT INTO BOOKING_REQUESTS"):
                    observed.append(test.snapshot(self))
                return super().execute(sql, parameters)
        def connect(*args, **kwargs):
            return REAL_CONNECT(*args, **kwargs, factory=ObservedConnection)
        with patch.object(storage.sqlite3, "connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced mapping failure") as raised:
                shop.create_booking(proposal)
        self.assertEqual(observed[0]["proposals"][0]["state"], "booked")
        self.assertEqual(len(observed[0]["orders"]), 1)
        self.assertEqual(self.capacity(observed[0]), self.capacity(before) - 1)
        self.assertEqual(self.snapshot(), before)
        self.events.append({"error": str(raised.exception), "before_booking": before,
                            "uncommitted": observed[0], "after_rollback": self.snapshot()})

    def test_duplicate_order_id_still_rolls_back_capacity(self):
        self.sql("INSERT INTO orders VALUES (?,?,?,?,?,?)", ("existing", "wash_and_fold", 1, 30, "morning", "confirmed"))
        proposal = self.prepare()
        self.approve(proposal)
        candidate = self.candidate(proposal)
        candidate["booking_id"] = "existing"
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed: orders.booking_id"):
            storage.save_booking(candidate)
        self.assertEqual(self.snapshot(), before)

    def test_capacity_failure_keeps_proposal_approved(self):
        proposal = self.prepare()
        self.approve(proposal)
        self.sql("UPDATE pickup_slots SET remaining_capacity=0 WHERE slot_code='morning'")
        self.assert_refused(lambda: shop.create_booking(proposal), "slot is unavailable")
        self.assertEqual(storage.get_proposal(proposal["request_id"])["status"], "approved")

    def test_last_slot_retry_and_restart_return_original_booking(self):
        self.sql("UPDATE pickup_slots SET remaining_capacity=1 WHERE slot_code='morning'")
        proposal = self.prepare()
        self.approve(proposal)
        booking = shop.create_booking(proposal)
        before = self.snapshot()
        self.assertEqual(self.capacity(), 0)
        proposal["pickup"]["remaining_capacity"] = 99
        proposal["pickup"]["available"] = False
        self.assertEqual(shop.create_booking(proposal), booking)
        output = self.subprocess(["-c", """
import json, sys
import shop, storage
print(json.dumps(shop.create_booking(storage.get_proposal(sys.argv[1]))))
""", proposal["request_id"]])
        self.assertEqual(json.loads(output), booking)
        self.assertEqual(self.snapshot(), before)
        self.events.append({"booking": booking, "retry_state": before})

    def concurrent_bookings(self, proposals):
        worker = """
import json, sys, time
from pathlib import Path
import shop
payload, ready, start = map(Path, sys.argv[1:])
proposal = json.loads(payload.read_text())
ready.touch()
deadline = time.monotonic() + 5
while not start.exists():
    if time.monotonic() > deadline:
        raise TimeoutError('Worker barrier timed out')
    time.sleep(0.01)
try:
    result = {'ok': True, 'booking': shop.create_booking(proposal)}
except Exception as error:
    result = {'ok': False, 'type': type(error).__name__, 'error': str(error)}
print(json.dumps(result))
"""
        processes = []
        try:
            for index, proposal in enumerate(proposals):
                payload = self.project / f"proposal-{index}.json"
                payload.write_text(json.dumps(proposal))
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", worker, str(payload),
                     str(self.project / f"ready-{index}"), str(self.project / "start")],
                    cwd=self.project, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 5
            while not all((self.project / f"ready-{index}").exists() for index in range(2)):
                self.assertLess(time.monotonic(), deadline, "Workers failed to reach barrier")
                time.sleep(0.01)
            (self.project / "start").touch()
            results = []
            for process in processes:
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(error, "")
                results.append(json.loads(output))
            self.events.append({"concurrent_results": results})
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)

    def test_concurrent_identical_requests_book_once(self):
        self.sql("UPDATE pickup_slots SET remaining_capacity=1 WHERE slot_code='morning'")
        proposal = self.prepare()
        self.approve(proposal)
        results = self.concurrent_bookings([proposal, proposal])
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["booking"], results[1]["booking"])
        after = self.snapshot()
        self.assertEqual(len(after["orders"]), 1)
        self.assertEqual(len(after["booking_requests"]), 1)
        self.assertEqual(after["proposals"][0]["state"], "booked")
        self.assertEqual(self.capacity(), 0)

    def test_concurrent_distinct_requests_cannot_overbook(self):
        self.sql("UPDATE pickup_slots SET remaining_capacity=1 WHERE slot_code='morning'")
        proposals = [self.prepare(), self.prepare()]
        for proposal in proposals:
            self.approve(proposal)
        results = self.concurrent_bookings(proposals)
        self.assertEqual(sum(result["ok"] for result in results), 1)
        failure = next(result for result in results if not result["ok"])
        self.assertEqual(failure["type"], "ValueError")
        self.assertIn("unavailable", failure["error"])
        after = self.snapshot()
        self.assertEqual(len(after["orders"]), 1)
        self.assertEqual(len(after["booking_requests"]), 1)
        self.assertEqual(sorted(row["state"] for row in after["proposals"]), ["approved", "booked"])
        self.assertEqual(self.capacity(), 0)

    def test_migration_preserves_legacy_orders_and_completed_requests(self):
        legacy = self.candidate(self.prepare())
        legacy.pop("snapshot_id")
        legacy["booking_id"] = "legacy-booking"
        self.sql("DROP TABLE proposals")
        self.sql("INSERT INTO orders VALUES (?,?,?,?,?,?)", ("legacy-booking", "wash_and_iron", 5, 250, "morning", "confirmed"))
        self.sql("INSERT INTO booking_requests VALUES (?,?,?,?)", (legacy["request_id"], legacy["booking_id"],
                 storage._canonical_booking_details(legacy), json.dumps(legacy)))
        self.sql("UPDATE pickup_slots SET remaining_capacity=0 WHERE slot_code='morning'")
        connection = REAL_CONNECT(self.database)
        try:
            connection.row_factory = sqlite3.Row
            legacy_rows = {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("orders", "pickup_slots", "booking_requests")
            }
        finally:
            connection.close()
        storage.initialize_database()
        before = self.snapshot()
        for table, saved_rows in legacy_rows.items():
            self.assertEqual(before[table], saved_rows)
        self.events.append({"before_migration": legacy_rows, "after_migration": before})
        storage.initialize_database()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(before["proposals"], [])
        self.assertEqual(storage.save_booking(legacy), legacy)
        self.assertEqual(storage.get_booking("legacy-booking")["total_price"], 250)
        self.assert_refused(lambda: storage.save_proposal(legacy), "completed legacy request")
        self.assertEqual(self.snapshot(), before)
        self.sql("DELETE FROM orders WHERE booking_id='legacy-booking'")
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "missing booking"):
            storage.save_booking(legacy)
        self.assertEqual(self.snapshot(), before)

    def test_lookup_and_check_order_remain_read_only(self):
        proposal = self.prepare()
        self.approve(proposal)
        booking = shop.create_booking(proposal)
        before = self.snapshot()
        self.assertEqual(storage.get_booking(booking["booking_id"]), {
            "booking_id": booking["booking_id"], "service_code": "wash_and_iron", "quantity": 5,
            "total_price": 250, "slot_code": "morning", "status": "confirmed"})
        self.assertIsNone(storage.get_booking("unknown"))
        output = self.subprocess(["check_order.py"], "  " + booking["booking_id"] + "  \n")
        self.assertIn("Quantity: 5 garments", output)
        self.assertIn("Total price: Rs 250", output)
        self.assertIn("No booking found with that ID.", self.subprocess(["check_order.py"], "unknown\n"))
        self.assertIn("ID cannot be empty", self.subprocess(["check_order.py"], "  \n"))
        self.assertEqual(self.snapshot(), before)

    def test_imports_are_silent_and_do_not_initialize_storage(self):
        before = self.snapshot()
        before_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(self.subprocess(["-c", "import shop, storage, check_order"]), "")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before_hash)
        empty = self.project / "no_database"
        empty.mkdir()
        for name in ("shop.py", "storage.py", "check_order.py"):
            shutil.copy2(self.project / name, empty / name)
        self.assertEqual(self.subprocess(["-c", "import shop, storage, check_order"], cwd=empty), "")
        self.assertFalse((empty / "laundry.db").exists())

    def test_cli_records_human_decisions_and_preserves_interactions(self):
        for label, inputs in (("approve", "morning\nyes\n"), ("reject", "morning\nno\n"),
                              ("quit", "quit\n"), ("invalid", "night\nmorning\nno\n")):
            with self.subTest(label=label):
                before = self.snapshot()
                output = self.subprocess(["shop.py"], inputs)
                after = self.snapshot()
                if label == "approve":
                    self.assertIn("Booking confirmed!", output)
                    self.assertEqual(len(after["orders"]), len(before["orders"]) + 1)
                    self.assertEqual(self.capacity(after), self.capacity(before) - 1)
                else:
                    self.assertEqual(after["orders"], before["orders"])
                    self.assertEqual(after["pickup_slots"], before["pickup_slots"])
                    self.assertEqual(after["booking_requests"], before["booking_requests"])
                if label != "quit":
                    added = [row for row in after["proposals"] if row["request_id"] not in
                             {row["request_id"] for row in before["proposals"]}]
                    self.assertEqual(len(added), 1)
                    self.assertEqual(added[0]["state"], "booked" if label == "approve" else "rejected")
                    for text in ("Proposal ID:", "Snapshot:", "Unit price: Rs 50", "Turnaround: 3 days",
                                 "Service code: wash_and_iron", "Pickup code: morning"):
                        self.assertLess(output.index(text), output.index("Do you approve"))
                if label == "quit":
                    self.assertIn("Pickup selection cancelled.", output)
                    self.assertEqual(after, before)
                if label == "invalid":
                    self.assertIn("Please choose a supported pickup slot.", output)
                    self.assertEqual(output.count("Choose a pickup slot"), 2)
                self.events.append({"interaction": label, "stdout": output, "before": before, "after": after})


if __name__ == "__main__":
    unittest.main(verbosity=2)
