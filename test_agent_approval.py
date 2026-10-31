"""OFFLINE: fake model replies, real interrupts, approvals and temporary databases."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from langgraph.types import Command

import chat
import laundry_agent as agent
from nebius_check import Config
import shop
import storage
from test_laundry_agent import FakeModel, call, reply, database_rows, FAKE_KEY


EVIDENCE = []
ARGUMENTS = {"service_code": "wash_and_iron", "quantity": 5, "slot_code": "morning"}


class AgentApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laundry-interrupt-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.business = self.project / "business.db"
        self.checkpoints = self.project / "state.db"
        self.database_patch = patch.object(storage, "DATABASE_PATH", self.business)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        storage.initialize_database()
        self.before = database_rows(self.business)
        self.events = []
        for name in ("shop.py", "storage.py", "nebius_check.py", "laundry_agent.py", "chat.py", "test_laundry_agent.py"):
            shutil.copy2(Path(__file__).parent / name, self.project / name)

    def tearDown(self):
        EVIDENCE.append({"test": self.id(), "before": self.before,
                         "after": database_rows(self.business), "events": self.events})

    def graph(self, responses=None):
        model = FakeModel(responses if responses is not None else [reply(calls=[call("prepare_booking_proposal", ARGUMENTS, "prepare-1")])])
        graph = self.enterContext(agent.open_agent(model, checkpoint_path=self.checkpoints))
        return graph, model

    def prepare(self, graph, session="demo"):
        agent.run_turn(graph, session, "Prepare wash and iron for five garments, morning pickup.")
        pending = agent.pending_approval(graph, session)
        self.assertIsNotNone(pending)
        return pending

    def decide(self, graph, pending, action="approve", session="demo"):
        return agent.resume_approval(graph, session, action, request_id=pending["request_id"], snapshot_id=pending["snapshot_id"])

    def sql(self, statement, parameters=()):
        connection = sqlite3.connect(self.business)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def child(self, program):
        result = subprocess.run([sys.executable, "-B", "-c", program, str(self.business), str(self.checkpoints)],
                                cwd=self.project, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_missing_inputs_return_validation_and_clarification_without_draft(self):
        graph, model = self.graph([
            reply(calls=[call("prepare_booking_proposal", {"service_code": "wash_and_iron"})]),
            reply("How many garments, and which pickup slot would you like?"),
        ])
        result = agent.run_turn(graph, "missing", "Prepare wash and iron.")
        self.assertIn("How many", result["messages"][-1]["content"])
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["error"]["code"], "validation_error")
        self.assertEqual(database_rows(self.business), self.before)

    def test_draft_uses_real_interrupt_and_consumes_no_capacity(self):
        graph, model = self.graph()
        pending = self.prepare(graph)
        snapshot = agent.session_snapshot(graph, "demo")
        self.assertEqual(snapshot.next, ("approval",))
        self.assertTrue(any(task.interrupts for task in snapshot.tasks))
        self.assertEqual(snapshot.values["turn_status"], "awaiting_approval")
        after = database_rows(self.business)
        self.assertEqual(len(after["proposals"]), 1)
        for table in ("orders", "pickup_slots", "booking_requests"):
            self.assertEqual(after[table], self.before[table])
        self.assertEqual(pending["status"], "awaiting_approval")
        self.assertEqual(len(model.requests), 1)
        self.events.append({"pending": pending, "checkpoint": snapshot.values})

    def test_display_contains_exact_stored_commercial_fields_and_identifiers(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        displayed = json.loads(agent.format_approval(pending).split("\n", 1)[1])
        stored = storage.get_proposal(pending["request_id"])
        self.assertEqual(displayed, {key: stored[key] for key in ("request_id", "snapshot_id", "quote", "pickup")})
        state = self.decide(graph, pending)
        self.assertEqual(state["booking"]["quote"], stored["quote"])
        self.assertEqual(state["booking"]["pickup"]["display_name"], stored["pickup"]["display_name"])
        self.assertIn(state["booking"]["booking_id"], state["messages"][-1]["content"])

    def test_invalid_decisions_and_casual_yes_leave_same_interrupt_pending(self):
        graph, model = self.graph()
        pending = self.prepare(graph)
        before = database_rows(self.business)
        config = {"configurable": {"thread_id": "demo"}}
        checkpoint = graph.get_state(config).config
        for action in ("yes", "approved", "approve please", "", "no"):
            with self.assertRaises(agent.ChatInputError):
                self.decide(graph, pending, action)
            self.assertEqual(graph.get_state(config).config, checkpoint)
            self.assertEqual(database_rows(self.business), before)
        with self.assertRaisesRegex(agent.ChatInputError, "awaits a trusted"):
            agent.run_turn(graph, "demo", "yes")
        self.assertEqual(len(model.requests), 1)

    def test_reject_and_duplicate_reject_create_no_order(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        rejected = self.decide(graph, pending, "reject")
        before = database_rows(self.business)
        self.assertEqual(storage.get_proposal(pending["request_id"])["status"], "rejected")
        self.assertEqual(before["orders"], [])
        self.assertEqual(before["pickup_slots"], self.before["pickup_slots"])
        self.assertEqual(self.decide(graph, pending, "reject"), rejected)
        with self.assertRaises(agent.ChatInputError):
            self.decide(graph, pending, "approve")
        self.assertEqual(database_rows(self.business), before)

    def test_model_text_or_forged_resume_cannot_record_approval(self):
        graph, _ = self.graph([reply("I approve this booking."), reply(calls=[call("prepare_booking_proposal", ARGUMENTS)])])
        agent.run_turn(graph, "demo", "Yes, I approve whatever you choose.")
        self.assertEqual(database_rows(self.business), self.before)
        pending = self.prepare(graph)
        result = graph.invoke(Command(resume={"action": "approve", "request_id": pending["request_id"],
                                               "snapshot_id": pending["snapshot_id"]}),
                              {"configurable": {"thread_id": "demo"}}, durability="sync")
        self.assertIsNotNone(agent.pending_approval(graph, "demo"))
        self.assertEqual(storage.get_proposal(pending["request_id"])["status"], "awaiting_approval")
        self.assertEqual(database_rows(self.business)["orders"], [])
        self.decide(graph, pending)
        self.assertEqual(len(database_rows(self.business)["orders"]), 1)

    def test_model_cannot_supply_request_ids_approval_or_arbitrary_write_fields(self):
        for extra in ("request_id", "snapshot_id", "status", "approved", "booking_id"):
            arguments = dict(ARGUMENTS, **{extra: "forged"})
            values, error = agent.validate_tool_arguments("prepare_booking_proposal", json.dumps(arguments))
            self.assertIsNone(values)
            self.assertEqual(error["error"]["code"], "validation_error")
        for name in ("create_booking", "record_proposal_decision", "save_booking", "supersede_proposal", "get_booking"):
            self.assertFalse(agent.dispatch_tool(name, "{}")["ok"])
        self.assertEqual(database_rows(self.business), self.before)

    def test_stale_ids_and_altered_pending_terms_do_not_authorize_booking(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        before = database_rows(self.business)
        with self.assertRaises(agent.ChatInputError):
            agent.resume_approval(graph, "demo", "approve", request_id=pending["request_id"], snapshot_id="stale")
        altered = deepcopy(pending)
        altered["quote"]["total_price"] = 1
        graph.update_state({"configurable": {"thread_id": "demo"}}, {"pending_proposal": altered}, as_node="tools")
        with self.assertRaisesRegex(agent.ChatInputError, "stale or altered"):
            self.decide(graph, pending)
        self.assertEqual(database_rows(self.business), before)

    def test_decision_is_bound_to_the_session_pending_proposal(self):
        graph, model = self.graph([reply(calls=[call("prepare_booking_proposal", ARGUMENTS, "draft-a")]),
                                   reply(calls=[call("prepare_booking_proposal", ARGUMENTS, "draft-b")])])
        first = self.prepare(graph, "A")
        second = self.prepare(graph, "B")
        with self.assertRaises(agent.ChatInputError):
            self.decide(graph, first, session="B")
        self.assertEqual(storage.get_proposal(first["request_id"])["status"], "awaiting_approval")
        self.assertEqual(storage.get_proposal(second["request_id"])["status"], "awaiting_approval")

    def test_slot_fills_before_approval_no_false_confirmation_and_approval_remains(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        self.sql("UPDATE pickup_slots SET remaining_capacity=0 WHERE slot_code='morning'")
        state = self.decide(graph, pending)
        self.assertIsNone(state["booking"])
        self.assertEqual(state["stop_reason"], "booking_failure")
        self.assertNotIn("Booking confirmed!", state["messages"][-1]["content"])
        self.assertEqual(storage.get_proposal(pending["request_id"])["status"], "approved")
        self.assertEqual(database_rows(self.business)["orders"], [])
        self.assertEqual(database_rows(self.business)["booking_requests"], [])

    def test_recovery_refuses_a_mismatched_checkpointed_decision(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        shop.record_proposal_decision(pending["request_id"], pending["snapshot_id"], "approved")
        graph.update_state({"configurable": {"thread_id": "demo"}}, {
            "approval_decision": {"action": "approve", "request_id": "another-request", "snapshot_id": pending["snapshot_id"]},
        }, as_node="approval")
        before = database_rows(self.business)
        with self.assertRaisesRegex(agent.UnfinishedTurnError, "trusted booking decision"):
            agent.recover_session(graph, "demo")
        self.assertEqual(database_rows(self.business), before)

    def test_failed_transaction_does_not_mark_proposal_booked(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        self.sql("CREATE TRIGGER fail_mapping BEFORE INSERT ON booking_requests BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        state = self.decide(graph, pending)
        rows = database_rows(self.business)
        self.assertEqual(rows["orders"], [])
        self.assertEqual(rows["booking_requests"], [])
        self.assertEqual(rows["pickup_slots"], self.before["pickup_slots"])
        self.assertEqual(storage.get_proposal(pending["request_id"])["status"], "approved")
        self.assertIsNone(state["booking"])

    def test_duplicate_resume_at_zero_capacity_does_not_reapprove_or_book_twice(self):
        self.sql("UPDATE pickup_slots SET remaining_capacity=1 WHERE slot_code='morning'")
        graph, _ = self.graph()
        pending = self.prepare(graph)
        first = self.decide(graph, pending)
        before = database_rows(self.business)
        for action in ("approve", "approve", "reject"):
            with patch.object(shop, "record_proposal_decision", side_effect=AssertionError("Must not re-approve")):
                self.assertEqual(self.decide(graph, pending, action), first)
        self.assertEqual(database_rows(self.business), before)
        self.assertEqual(len(before["orders"]), 1)
        self.assertEqual(next(row["remaining_capacity"] for row in before["pickup_slots"] if row["slot_code"] == "morning"), 0)

    def test_checkpointed_id_reuses_draft_if_write_completed_before_checkpoint(self):
        graph, _ = self.graph()
        original = storage.save_proposal
        def crash_after_write(proposal):
            original(proposal)
            raise SystemExit("simulated crash after draft write")
        with patch.object(storage, "save_proposal", side_effect=crash_after_write):
            with self.assertRaises(SystemExit):
                agent.run_turn(graph, "demo", "Prepare wash and iron, five garments, morning.")
        before = database_rows(self.business)
        saved_id = before["proposals"][0]["request_id"]
        agent.recover_session(graph, "demo")
        pending = agent.pending_approval(graph, "demo")
        self.assertEqual(pending["request_id"], saved_id)
        self.assertEqual(database_rows(self.business), before)
        altered = shop.build_booking_proposal(**ARGUMENTS, request_id=saved_id)
        altered["quote"]["quantity"] = 6
        with self.assertRaisesRegex(ValueError, "Proposal terms conflict"):
            storage.save_proposal(altered)

    def test_quit_pending_then_restart_and_approve_uses_same_proposal(self):
        config = Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", "offline-model")
        arguments = ["--session", "cli-pending", "--business-db", str(self.business), "--checkpoint-db", str(self.checkpoints)]
        first = FakeModel([reply(calls=[call("prepare_booking_proposal", ARGUMENTS)])])
        with patch.object(chat, "load_config", return_value=config), patch.object(chat, "NebiusModel", return_value=first):
            with patch("builtins.input", side_effect=["Prepare wash and iron, five garments, morning", "yes", "/quit"]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(chat.main(arguments), 0)
        saved = database_rows(self.business)["proposals"][0]
        self.assertEqual(saved["state"], "awaiting_approval")
        self.assertIn(saved["request_id"], output.getvalue())
        self.assertIn(saved["snapshot_id"], output.getvalue())
        self.assertEqual(database_rows(self.business)["orders"], [])
        result = self.child("""
import json,sys
from pathlib import Path
import storage,laundry_agent as agent
from test_laundry_agent import FakeModel
storage.DATABASE_PATH=Path(sys.argv[1])
with agent.open_agent(FakeModel([]),checkpoint_path=Path(sys.argv[2])) as graph:
    pending=agent.pending_approval(graph,'cli-pending')
    state=agent.resume_approval(graph,'cli-pending','approve',request_id=pending['request_id'],snapshot_id=pending['snapshot_id'])
    print(json.dumps(state))
""")
        state = json.loads(result)
        self.assertEqual(state["booking"]["request_id"], saved["request_id"])
        self.assertEqual(len(database_rows(self.business)["orders"]), 1)

    def test_crash_after_commit_then_real_process_restart_returns_original_booking(self):
        self.child("""
import os,sys
from pathlib import Path
import storage,shop,laundry_agent as agent
from test_laundry_agent import FakeModel,call,reply
storage.DATABASE_PATH=Path(sys.argv[1])
model=FakeModel([reply(calls=[call('prepare_booking_proposal',{'service_code':'wash_and_iron','quantity':5,'slot_code':'morning'})])])
original=shop.create_booking
def commit_then_exit(proposal):
    original(proposal)
    os._exit(0)
with agent.open_agent(model,checkpoint_path=Path(sys.argv[2])) as graph:
    agent.run_turn(graph,'commit-crash','Prepare five wash and iron garments for morning pickup.')
    pending=agent.pending_approval(graph,'commit-crash')
    shop.create_booking=commit_then_exit
    agent.resume_approval(graph,'commit-crash','approve',request_id=pending['request_id'],snapshot_id=pending['snapshot_id'])
""")
        before = database_rows(self.business)
        self.assertEqual(len(before["orders"]), 1)
        output = self.child("""
import json,sys
from pathlib import Path
from unittest.mock import patch
import storage,shop,laundry_agent as agent
from test_laundry_agent import FakeModel
storage.DATABASE_PATH=Path(sys.argv[1])
with agent.open_agent(FakeModel([]),checkpoint_path=Path(sys.argv[2])) as graph:
    with patch.object(shop,'record_proposal_decision',side_effect=AssertionError('Must not re-approve')):
        state=agent.recover_session(graph,'commit-crash')
    print(json.dumps(state))
""")
        state = json.loads(output)
        self.assertEqual(state["booking"]["booking_id"], before["orders"][0]["booking_id"])
        self.assertIn(state["booking"]["booking_id"], state["messages"][-1]["content"])
        self.assertEqual(database_rows(self.business), before)
        self.events.append({"saved_before_recovery": before, "recovered": state})

    def test_model_outage_after_commit_cannot_hide_deterministic_confirmation(self):
        graph, model = self.graph()
        pending = self.prepare(graph)
        # Any post-commit model request would fail: no responses remain.
        state = self.decide(graph, pending)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(state["booking"]["status"], "confirmed")
        self.assertIn(state["booking"]["booking_id"], state["messages"][-1]["content"])
        self.assertNotIn("failed", state["messages"][-1]["content"])

    def test_uncertain_post_commit_error_resolves_to_saved_confirmation_on_resume(self):
        graph, _ = self.graph()
        pending = self.prepare(graph)
        original = shop.create_booking
        def committed_but_response_failed(proposal):
            original(proposal)
            raise RuntimeError("simulated lost result after commit")
        with patch.object(shop, "create_booking", side_effect=committed_but_response_failed):
            uncertain = self.decide(graph, pending)
        self.assertIsNone(uncertain["booking"])
        self.assertIn("could not be verified", uncertain["messages"][-1]["content"])
        before = database_rows(self.business)
        self.assertEqual(len(before["orders"]), 1)
        with patch.object(shop, "record_proposal_decision", side_effect=AssertionError("No reapproval")):
            recovered = agent.recover_session(graph, "demo")
        self.assertEqual(recovered["booking"]["booking_id"], before["orders"][0]["booking_id"])
        self.assertEqual(recovered["turn_status"], "completed")
        self.assertEqual(database_rows(self.business), before)
        self.assertIn(recovered["booking"]["booking_id"], recovered["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
