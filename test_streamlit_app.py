"""OFFLINE AppTest checks: fake model, real graph, isolated SQLite databases."""
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError
from streamlit.testing.v1 import AppTest

import laundry_agent as agent
from nebius_check import Config, ConfigurationError
import streamlit_backend as backend
from test_date_helpers import TEST_DATE, seed_date
from test_laundry_agent import FakeModel, call, reply, database_rows, FAKE_KEY


ARGS = {"service_code": "wash_and_iron", "quantity": 5, "slot_code": "morning", "pickup_date": TEST_DATE}


class StreamlitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="laundry-ui-offline-"))
        self.business = Path(self.tmp) / "business.db"
        self.checkpoints = Path(self.tmp) / "checkpoints.db"
        self.enterContext(patch.dict(os.environ, {"LAUNDRY_BUSINESS_DB": str(self.business),
                                                  "LAUNDRY_CHECKPOINT_DB": str(self.checkpoints)}))
        self.config = self.enterContext(patch("app.load_config", return_value=Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", "offline-model")))
        # AppTest executes app.py as __main__, so patch the imported config source too.
        self.enterContext(patch("nebius_check.load_config", side_effect=lambda: self.config()))
        self.model = FakeModel([])
        self.enterContext(patch.object(backend.LazyModel, "__call__", side_effect=lambda messages: self.model(messages)))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("OFFLINE: network forbidden")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("OFFLINE: network forbidden")))

    def app(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=20).run()
        self.assertFalse(app.exception)
        if not app.error:
            seed_date(self.business)
            if not app.date_input(key="order_date").disabled:
                app.date_input(key="order_date").set_value(__import__("datetime").date.fromisoformat(TEST_DATE)).run()
        return app

    def send(self, app, text):
        app.chat_input[0].set_value(text).run()
        self.assertFalse(app.exception)

    def button(self, app, label):
        return next(button for button in app.button if button.label == label)

    def draft(self, app):
        self.model.responses.append(reply(calls=[call("prepare_booking_proposal", ARGS)]))
        self.send(app, "Prepare wash and iron for five garments, morning pickup.")
        return next(value.value for value in app.json if "snapshot_id" in value.value)

    def text(self, app):
        return "\n".join(item.value for kind in (app.markdown, app.warning, app.error) for item in kind if not item.value.startswith("<style>"))

    def test_quote_display_activity_and_redraw_are_read_only(self):
        app = self.app()
        before = database_rows(self.business)
        self.model.responses.extend([reply(calls=[call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 5})]), reply("Five garments cost Rs 250.")])
        self.send(app, "What does wash and iron for five garments cost?")
        self.assertIn("Rs 250", self.text(app))
        self.assertTrue(any("calculate_quote" in item.value for item in app.json))
        self.assertEqual(len(self.model.requests), 2)
        app.run()
        self.button(app, "Refresh availability").click().run()
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(database_rows(self.business), before)

    def test_pending_exact_terms_reject_then_new_proposal(self):
        app = self.app()
        before = database_rows(self.business)
        terms = self.draft(app)
        self.assertIn('"unit_price": 50', terms)
        self.assertIn('"turnaround_days": 3', terms)
        self.assertTrue(app.chat_input[0].disabled)
        app.run()
        self.assertEqual(len(self.model.requests), 1)
        self.button(app, "Reject").click().run()
        after = database_rows(self.business)
        self.assertEqual(after["orders"], before["orders"])
        self.assertEqual(after["pickup_slots"], before["pickup_slots"])
        self.assertEqual(after["proposals"][0]["state"], "rejected")
        self.assertFalse(app.chat_input[0].disabled)
        app.run()
        self.assertEqual(database_rows(self.business), after)
        self.draft(app)
        self.assertEqual(len(database_rows(self.business)["proposals"]), 2)

    def test_successful_booking_redraw_and_stale_button_do_not_repeat(self):
        app = self.app()
        self.draft(app)
        with backend.open_demo() as graph:
            pending = agent.pending_approval(graph, app.session_state["thread_id"])
        self.button(app, "Approve").click().run()
        rows = database_rows(self.business)
        self.assertEqual(len(rows["orders"]), 1)
        booking_id = rows["orders"][0]["booking_id"]
        self.assertIn(booking_id, self.text(app))
        self.assertEqual(next(s for s in rows["dated_pickup_slots"] if s["slot_code"] == "morning")["remaining_capacity"], 1)
        self.assertEqual(rows["proposals"][0]["state"], "booked")
        app.run()
        self.button(app, "Refresh availability").click().run()
        app.session_state["action"] = (app.session_state["thread_id"], "approve", (pending["request_id"], pending["snapshot_id"]))
        app.run()
        self.assertEqual(database_rows(self.business), rows)
        self.assertEqual(len(self.model.requests), 1)
        self.assertIn("1 remaining", self.text(app))

    def test_pending_and_completed_history_restore_without_resubmission(self):
        app = self.app()
        terms = self.draft(app)
        thread = app.session_state["thread_id"]
        restored = self.app()
        restored.text_input(key="resume_name").set_value(thread).run()
        self.button(restored, "Resume conversation").click().run()
        self.assertTrue(any(value.value == terms for value in restored.json))
        self.button(restored, "Approve").click().run()
        rows = database_rows(self.business)
        count = len(restored.chat_message)
        self.button(restored, "New conversation").click().run()
        self.assertEqual(len(restored.chat_message), 1)  # Fresh customer welcome.
        self.assertNotIn(rows["orders"][0]["booking_id"], self.text(restored))
        self.assertEqual(database_rows(self.business), rows)
        restored.text_input(key="resume_name").set_value(thread).run()
        self.button(restored, "Resume conversation").click().run()
        restored.run()
        self.assertEqual(len(restored.chat_message), count)
        self.assertEqual(database_rows(self.business), rows)
        self.assertEqual(len(self.model.requests), 1)

    def test_browser_refresh_query_restores_pending_without_calls(self):
        app = self.app()
        terms = self.draft(app)
        refreshed = AppTest.from_file(str(Path(__file__).with_name("app.py")))
        refreshed.query_params["session"] = app.session_state["thread_id"]
        refreshed.run()
        self.assertFalse(refreshed.exception)
        self.assertTrue(any(value.value == terms for value in refreshed.json))
        self.assertEqual(len(self.model.requests), 1)

    def test_missing_config_is_helpful_no_secret_or_network(self):
        self.config.side_effect = ConfigurationError("private-error-" + FAKE_KEY)
        app = self.app()
        self.assertIn("local .env", self.text(app))
        self.assertNotIn(FAKE_KEY, self.text(app))
        self.assertTrue(app.chat_input[0].disabled)
        self.assertEqual(self.model.requests, [])

    def test_missing_model_disables_chat_without_choosing_one(self):
        self.config.return_value = Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", None)
        app = self.app()
        self.assertTrue(app.chat_input[0].disabled)
        self.assertIn("NEBIUS_MODEL", self.text(app))
        self.assertEqual(self.model.requests, [])

    def test_interrupted_booking_is_only_recovered_on_explicit_click(self):
        import shop
        app = self.app()
        self.draft(app)
        thread = app.session_state["thread_id"]
        with backend.open_demo() as graph:
            pending = agent.pending_approval(graph, thread)
            with patch.object(shop, "create_booking", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    agent.resume_approval(graph, thread, "approve", request_id=pending["request_id"], snapshot_id=pending["snapshot_id"])
        before = database_rows(self.business)
        self.assertEqual(len(before["orders"]), 0)
        app.run()
        self.assertEqual(database_rows(self.business), before)
        self.assertTrue(app.chat_input[0].disabled)
        self.button(app, "Recover saved outcome").click().run()
        after = database_rows(self.business)
        self.assertEqual(len(after["orders"]), 1)
        self.assertIn(after["orders"][0]["booking_id"], self.text(app))
        app.run()
        self.assertEqual(database_rows(self.business), after)
        self.assertEqual(len(self.model.requests), 1)

    def test_connection_failure_never_confirms_or_exposes_secret(self):
        app = self.app()
        before = database_rows(self.business)
        with patch.object(backend.LazyModel, "__call__", side_effect=APIConnectionError(message=FAKE_KEY, request=httpx.Request("POST", "https://example.invalid"))):
            self.send(app, "Price for five garments?")
        self.assertIn("CONNECTION ERROR", self.text(app))
        self.assertNotIn(FAKE_KEY, self.text(app))
        self.assertNotIn("Booking confirmed!", self.text(app))
        self.assertEqual(database_rows(self.business), before)

    def test_replaced_review_cannot_approve_another_proposal(self):
        app = self.app()
        self.draft(app)
        before = database_rows(self.business)
        app.session_state["action"] = (app.session_state["thread_id"], "approve", ("old-request", "old-snapshot"))
        app.run()
        self.assertEqual(database_rows(self.business), before)
        self.assertTrue(app.chat_input[0].disabled)

    def test_post_commit_failure_has_saved_confirmation(self):
        import shop
        app = self.app()
        self.draft(app)
        create = shop.create_booking
        failed = []
        def create_then_fail(proposal):
            booking = create(proposal)
            if not failed:
                failed.append(True)
                raise RuntimeError(FAKE_KEY)
            return booking
        with patch.object(shop, "create_booking", side_effect=create_then_fail):
            self.button(app, "Approve").click().run()
        rows = database_rows(self.business)
        self.assertIn(rows["orders"][0]["booking_id"], self.text(app))
        self.assertNotIn(FAKE_KEY, self.text(app))
        app.run()
        self.assertEqual(database_rows(self.business), rows)

    def test_original_database_path_is_refused(self):
        with patch.dict(os.environ, {"LAUNDRY_BUSINESS_DB": str(backend.PROJECT / "laundry.db")}):
            app = self.app()
        self.assertTrue(app.error)
        self.assertFalse(self.business.exists())

    def test_import_is_silent_without_initialization(self):
        result = subprocess.run([sys.executable, "-B", "-c", "import app, streamlit_backend"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertFalse(self.business.exists())
        self.assertFalse(self.checkpoints.exists())

    def test_request_builder_quote_uses_customer_values_without_writes(self):
        app = self.app()
        before = database_rows(self.business)
        self.model.responses.extend([reply(calls=[call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 6})]), reply("Six garments cost Rs 300.")])
        app.selectbox(key="order_service").set_value("wash_and_iron")
        app.number_input(key="order_quantity").set_value(6)
        self.button(app, "Just get a quote").click().run()
        self.assertFalse(app.exception)
        self.assertIn("Wash and Iron", self.model.requests[0][-1]["content"])
        self.assertIn("6 garments", self.model.requests[0][-1]["content"])
        self.assertIn("Rs 300", self.text(app))
        self.assertEqual(database_rows(self.business), before)
        app.run()
        self.assertEqual(len(self.model.requests), 2)

    def test_request_builder_review_approval_and_receipt_remain_safe(self):
        app = self.app()
        before = database_rows(self.business)
        self.model.responses.append(reply(calls=[call("prepare_booking_proposal", ARGS)]))
        app.selectbox(key="order_service").set_value("wash_and_iron")
        app.selectbox(key="order_slot").set_value("morning")
        self.button(app, "Review proposal →").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.chat_input[0].disabled)
        rows = database_rows(self.business)
        self.assertEqual(rows["orders"], before["orders"])
        self.assertEqual(rows["pickup_slots"], before["pickup_slots"])
        self.assertTrue(any(metric.value == "Rs 250" for metric in app.metric))
        self.button(app, "Approve").click().run()
        after = database_rows(self.business)
        self.assertEqual(len(after["orders"]), 1)
        self.assertIn(after["orders"][0]["booking_id"], self.text(app))
        self.assertNotIn('"booking_id":', self.text(app))
        self.model.responses.append(reply("You're welcome."))
        self.send(app, "Thank you")
        self.assertIn(after["orders"][0]["booking_id"], self.text(app))
        self.assertNotIn('"booking_id":', self.text(app))
        self.assertEqual(database_rows(self.business), after)

    def test_full_pickup_windows_disable_proposals_but_allow_quotes(self):
        app = self.app()
        with sqlite3.connect(self.business) as connection:
            connection.execute("UPDATE dated_pickup_slots SET remaining_capacity = 0")
        app.run()
        before = database_rows(self.business)
        self.assertTrue(self.button(app, "Review proposal →").disabled)
        self.assertFalse(self.button(app, "Just get a quote").disabled)
        self.model.responses.extend([reply(calls=[call("calculate_quote", {"service_code": "wash_and_fold", "quantity": 5})]), reply("The price estimate is Rs 150.")])
        self.button(app, "Just get a quote").click().run()
        self.assertFalse(app.exception)
        self.assertIn("Rs 150", self.text(app))
        self.assertEqual(database_rows(self.business), before)

    def test_customer_shortcut_runs_only_on_explicit_click(self):
        app = self.app()
        before = database_rows(self.business)
        app.number_input(key="order_quantity").set_value(7).run()
        self.assertEqual(self.model.requests, [])
        self.model.responses.extend([reply(calls=[call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 5})]), reply("Five garments cost Rs 250.")])
        self.button(app, "Price wash & iron for 5 garments").click().run()
        app.run()
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(database_rows(self.business), before)

    def test_receipt_cannot_confirm_an_unsaved_model_claim(self):
        import json
        app = self.app()
        before = database_rows(self.business)
        self.model.responses.append(reply("Booking confirmed! Saved booking details:\n" + json.dumps({"booking_id": "invented", "request_id": "invented"})))
        self.send(app, "What is my booking?")
        self.assertTrue(any("couldn't verify" in warning.value for warning in app.warning))
        self.assertNotIn("✓ Booking confirmed!", self.text(app))
        self.assertEqual(database_rows(self.business), before)

    def test_date_picker_refreshes_only_that_days_capacity_without_model_calls(self):
        from datetime import date, timedelta
        app = self.app()
        other = (date.fromisoformat(TEST_DATE) + timedelta(days=1)).isoformat()
        seed_date(self.business, other)
        with sqlite3.connect(self.business) as connection:
            connection.execute("UPDATE dated_pickup_slots SET remaining_capacity=0 WHERE pickup_date=? AND slot_code='morning'", (other,))
        before = database_rows(self.business)
        app.date_input(key="order_date").set_value(date.fromisoformat(other)).run()
        self.assertFalse(app.exception)
        self.assertEqual(self.model.requests, [])
        self.assertTrue(all(not value.startswith("Morning") for value in app.selectbox(key="order_slot").options))
        self.assertEqual(database_rows(self.business), before)
        app.date_input(key="order_date").set_value(date.fromisoformat(TEST_DATE)).run()
        self.assertTrue(any(value.startswith("Morning") for value in app.selectbox(key="order_slot").options))

    def test_form_binds_selected_date_to_review_and_saved_receipt(self):
        from datetime import date, timedelta
        app = self.app()
        other = (date.fromisoformat(TEST_DATE) + timedelta(days=1)).isoformat()
        app.date_input(key="order_date").set_value(date.fromisoformat(other)).run()
        self.model.responses.append(reply(calls=[call("prepare_booking_proposal", {**ARGS, "pickup_date": other})]))
        app.selectbox(key="order_service").set_value("wash_and_iron")
        self.button(app, "Review proposal →").click().run()
        self.assertIn(other, self.model.requests[0][-1]["content"])
        self.assertIn(other, self.text(app))
        self.assertEqual(len(app.date_input), 0)
        self.button(app, "Approve").click().run()
        self.assertFalse(app.exception)
        rows = database_rows(self.business)
        self.assertEqual(rows["orders"][0]["pickup_date"], other)
        self.assertIn(other, self.text(app))
        self.assertEqual(app.date_input(key="order_date").value, date.fromisoformat(other))
        self.assertEqual(len(self.model.requests), 1)

    def test_clearing_date_does_not_submit_or_allow_undated_proposals(self):
        app = self.app()
        before = database_rows(self.business)
        app.date_input(key="order_date").set_value(None).run()
        self.assertFalse(app.exception)
        self.assertTrue(self.button(app, "Review proposal →").disabled)
        self.assertFalse(self.button(app, "Just get a quote").disabled)
        self.assertEqual(self.model.requests, [])
        self.assertEqual(database_rows(self.business), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
