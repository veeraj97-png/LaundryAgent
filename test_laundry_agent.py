"""OFFLINE: fake model replies exercise the real graph and temporary SQLite reads."""
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
from unittest.mock import Mock, patch
from uuid import uuid4

from openai import APIConnectionError
from openai.types.chat import ChatCompletion

import chat
import laundry_agent as agent
from nebius_check import Config
import shop
import storage


EVIDENCE = []
FAKE_KEY = "offline-private-key-not-real"


def call(name, arguments, call_id="offline-call"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return message


class FakeModel:
    """Deterministic OFFLINE responses, never a substitute for a live test."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.close = Mock()

    def __call__(self, messages):
        self.requests.append(deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else deepcopy(response)


def database_rows(path):
    connection = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
                for table in tables}
    finally:
        connection.close()


class ReadOnlyAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laundry-graph-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.database = self.project / "laundry.db"
        self.database_patch = patch.object(storage, "DATABASE_PATH", self.database)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        storage.initialize_database()
        self.before = database_rows(self.database)
        self.activity = []

    def tearDown(self):
        EVIDENCE.append({"test": self.id(), "before": self.before,
                         "after": database_rows(self.database), "activity": self.activity})

    def graph(self, responses, **limits):
        model = FakeModel(responses)
        graph = self.enterContext(agent.open_agent(
            model, checkpoint_path=self.project / f"state-{uuid4()}.db",
            activity=self.activity.append, **limits,
        ))
        return graph, model

    def results(self, state):
        return [json.loads(message["content"]) for message in state["messages"] if message["role"] == "tool"]

    def assert_protocol(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "tool":
                self.assertTrue(pending)
                self.assertEqual(message["tool_call_id"], pending.pop(0))
            else:
                self.assertFalse(pending)
                if message.get("tool_calls"):
                    pending = [item["id"] for item in message["tool_calls"]]
        self.assertFalse(pending)

    def test_exact_allowlist_has_no_write_or_order_lookup_tool(self):
        self.assertEqual(agent.TOOL_NAMES, {"read_service_catalogue", "calculate_quote", "check_pickup_slot", "get_available_pickup_slots", "prepare_booking_proposal"})
        for name in ("create_booking", "record_proposal_decision", "supersede_proposal", "get_booking", "__import__", None, [], {}):
            result = agent.dispatch_tool(name, "{}")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "unsupported_input")
        self.assertEqual(database_rows(self.database), self.before)

    def test_all_four_tools_return_real_read_only_business_results(self):
        catalogue = agent.dispatch_tool("read_service_catalogue", "{}")
        self.assertEqual(catalogue["data"], [{"service_code": code, **details} for code, details in shop.services.items()])
        quote = agent.dispatch_tool("calculate_quote", '{"service_code":"wash_and_iron","quantity":5}')
        self.assertEqual(quote["data"], shop.calculate_quote("wash_and_iron", 5))
        self.assertEqual(quote["data"]["total_price"], 250)
        slot = agent.dispatch_tool("check_pickup_slot", '{"slot_code":"morning"}')
        self.assertEqual(slot["data"]["remaining_capacity"], 2)
        self.assertEqual(slot["outcome"], "available")
        available = agent.dispatch_tool("get_available_pickup_slots", "{}")
        self.assertEqual({slot["slot_code"] for slot in available["data"]}, {"morning", "evening"})
        self.assertEqual(database_rows(self.database), self.before)

    def test_validation_rejects_missing_extra_wrong_types_and_duplicate_keys(self):
        arguments = ["not json", "[]", "null", "{}", '{"service_code":"wash_and_iron"}',
                     '{"service_code":"wash_and_iron","quantity":5,"extra":1}',
                     '{"service_code":"wash_and_iron","quantity":5,"quantity":5}',
                     *[json.dumps({"service_code": "wash_and_iron", "quantity": value})
                       for value in (True, "5", 5.0, 0, -1, 1001, None)], " " * 4097]
        with patch.object(shop, "calculate_quote") as quote:
            for value in arguments:
                with self.subTest(value_type=type(value).__name__):
                    result = agent.dispatch_tool("calculate_quote", value)
                    self.assertEqual(result["error"]["code"], "validation_error")
            quote.assert_not_called()
        self.assertEqual(agent.dispatch_tool("read_service_catalogue", '{"extra":1}')["error"]["code"], "validation_error")

    def test_unsupported_full_empty_and_technical_results_are_distinct(self):
        self.assertEqual(agent.dispatch_tool("check_pickup_slot", '{"slot_code":"night"}')["error"]["code"], "unsupported_input")
        full = agent.dispatch_tool("check_pickup_slot", '{"slot_code":"afternoon"}')
        self.assertTrue(full["ok"])
        self.assertEqual(full["outcome"], "unavailable")
        with patch.object(shop, "get_available_pickup_slots", return_value=[]):
            empty = agent.dispatch_tool("get_available_pickup_slots", "{}")
            self.assertTrue(empty["ok"])
            self.assertEqual(empty["outcome"], "no_availability")
        with patch.object(shop, "check_pickup_slot", side_effect=sqlite3.OperationalError(FAKE_KEY)):
            failed = agent.dispatch_tool("check_pickup_slot", '{"slot_code":"morning"}')
            self.assertEqual(failed["error"]["code"], "technical_failure")
            self.assertNotIn(FAKE_KEY, json.dumps(failed))

    def test_normal_answer_routes_to_end_without_tool(self):
        graph, model = self.graph([reply("Hello. I can explain services, quotes and undated pickup slots.")])
        result = agent.run_turn(graph, "greeting", "Hello")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result["tool_calls"], 0)
        self.assertIsNone(result["stop_reason"])

    def test_model_tool_model_loop_preserves_call_id(self):
        graph, model = self.graph([
            reply(calls=[call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 5}, "quote-1")]),
            reply("Wash and iron for five garments costs Rs 250."),
        ])
        result = agent.run_turn(graph, "quote-session", "What would it cost to wash and iron five garments?")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(self.results(result)[0]["data"]["total_price"], 250)
        self.assertEqual(model.requests[1][-1]["tool_call_id"], "quote-1")
        self.assert_protocol(result["messages"])
        self.assertEqual(database_rows(self.database), self.before)

    def test_multiple_tool_calls_are_all_answered_before_model_runs(self):
        graph, model = self.graph([
            reply(calls=[call("check_pickup_slot", {"slot_code": "evening"}, "evening-1"),
                         call("check_pickup_slot", {"slot_code": "morning"}, "morning-1")]),
            reply("The undated evening and morning slots both have capacity."),
        ])
        result = agent.run_turn(graph, "two-slots", "Check evening and morning")
        self.assertEqual([message["tool_call_id"] for message in model.requests[1][-2:]], ["evening-1", "morning-1"])
        self.assertEqual([value["data"]["remaining_capacity"] for value in self.results(result)], [3, 2])
        self.assertEqual(result["tool_calls"], 2)
        self.assert_protocol(result["messages"])

    def test_validation_result_returns_to_model_for_clarification(self):
        graph, model = self.graph([
            reply(calls=[call("calculate_quote", {"service_code": "wash_and_iron"}, "missing-quantity")]),
            reply("How many garments would you like washed and ironed?"),
        ])
        result = agent.run_turn(graph, "clarify", "How much for wash and iron?")
        self.assertEqual(self.results(result)[0]["error"]["code"], "validation_error")
        self.assertEqual(model.requests[1][-1]["tool_call_id"], "missing-quantity")
        self.assertIsNone(result["stop_reason"])
        self.assertEqual(database_rows(self.database), self.before)

    def test_unknown_tool_returns_structured_error_to_model(self):
        graph, model = self.graph([
            reply(calls=[call("create_booking", {}, "not-allowed")]),
            reply("I cannot create bookings through this chat."),
        ])
        with patch.object(shop, "create_booking", side_effect=AssertionError("Must not execute")):
            result = agent.run_turn(graph, "unsupported", "Create a booking")
        self.assertEqual(self.results(result)[0]["error"]["code"], "unsupported_input")
        self.assertEqual(model.requests[1][-1]["tool_call_id"], "not-allowed")
        self.assert_protocol(result["messages"])
        self.assertEqual(database_rows(self.database), self.before)

    def test_multi_turn_follow_up_has_previous_messages_and_results(self):
        def morning_request(messages):
            self.assertTrue(any(message.get("content") == "Is evening pickup available?" for message in messages))
            self.assertTrue(any(message["role"] == "tool" and '"evening"' in message["content"] for message in messages))
            self.assertEqual(messages[-1]["content"], "What about morning?")
            return reply(calls=[call("check_pickup_slot", {"slot_code": "morning"}, "follow-morning")])
        graph, model = self.graph([
            reply(calls=[call("check_pickup_slot", {"slot_code": "evening"}, "first-evening")]), reply("Evening has capacity; slots have no dates."),
            morning_request, reply("Morning has capacity too; this is undated availability."),
        ])
        agent.run_turn(graph, "stable-thread", "Is evening pickup available?")
        result = agent.run_turn(graph, "stable-thread", "What about morning?")
        self.assertEqual(len(model.requests), 4)
        self.assertEqual(result["model_calls"], 2)  # Reset per turn, not the history.
        self.assertEqual(len(self.results(result)), 2)
        self.assert_protocol(result["messages"])

    def test_new_session_has_independent_history(self):
        graph, model = self.graph([reply("I can help with your laundry."), reply("Which service and quantity do you mean?"), reply("You mentioned wash and iron.")])
        agent.run_turn(graph, "session-A", "I want wash and iron")
        agent.run_turn(graph, "session-B", "What did I choose?")
        self.assertEqual(len(model.requests[1]), 2)  # system + B's user message
        self.assertFalse(any(message.get("content") == "I want wash and iron" for message in model.requests[1]))
        agent.run_turn(graph, "session-A", "What did I choose?")
        self.assertTrue(any(message.get("content") == "I want wash and iron" for message in model.requests[2]))

    def test_new_checkpoint_database_has_no_old_thread_even_with_same_id(self):
        first, _ = self.graph([reply("Hello")])
        agent.run_turn(first, "same-ID", "Remember this conversation")
        second, model = self.graph([reply("This is a new conversation.")])
        agent.run_turn(second, "same-ID", "Hello")
        self.assertEqual(len(model.requests[0]), 2)

    def test_model_step_limit_stops_a_repeating_tool_loop(self):
        graph, model = self.graph([
            reply(calls=[call("read_service_catalogue", {}, "loop-1")]),
            reply(calls=[call("read_service_catalogue", {}, "loop-2")]),
        ], max_model_calls=2)
        result = agent.run_turn(graph, "loop", "Keep checking")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(result["stop_reason"], "step_limit")
        self.assertIn("incomplete", result["messages"][-1]["content"])
        self.assert_protocol(result["messages"])

    def test_tool_limit_answers_every_id_without_executing_extra_tool(self):
        graph, model = self.graph([reply(calls=[
            call("read_service_catalogue", {}, "allowed-1"),
            call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 5}, "over-limit"),
        ])], max_tool_calls=1)
        with patch.object(shop, "calculate_quote") as quote:
            result = agent.run_turn(graph, "tool-limit", "Check catalogue and quote")
            quote.assert_not_called()
        self.assertEqual(result["stop_reason"], "step_limit")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.results(result)[1]["error"]["code"], "step_limit")
        self.assert_protocol(result["messages"])

    def test_technical_tool_failure_stops_and_skips_rest_of_batch(self):
        graph, model = self.graph([reply(calls=[
            call("check_pickup_slot", {"slot_code": "morning"}, "broken-slot"),
            call("calculate_quote", {"service_code": "wash_and_iron", "quantity": 5}, "skipped-quote"),
        ])])
        with patch.object(shop, "check_pickup_slot", side_effect=sqlite3.OperationalError(FAKE_KEY)):
            with patch.object(shop, "calculate_quote") as quote:
                result = agent.run_turn(graph, "failure", "Check slot and quote")
                quote.assert_not_called()
        self.assertEqual(result["stop_reason"], "technical_failure")
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assert_protocol(result["messages"])

    def test_network_and_malformed_model_failures_never_claim_success(self):
        duplicate = call("read_service_catalogue", {}, "same-call")
        for response in (APIConnectionError(request=Mock(), message=FAKE_KEY),
                         reply(calls=[duplicate, duplicate]), reply(""),
                         reply("<think>private reasoning</think>answer")):
            with self.subTest(response_type=type(response).__name__):
                graph, model = self.graph([response])
                result = agent.run_turn(graph, "failed-thread", "Hello")
                self.assertEqual(result["stop_reason"], "technical_failure")
                self.assertNotIn(FAKE_KEY, json.dumps(result))
                self.assertEqual(len(model.requests), 1)

    def test_logs_show_only_safe_arguments_and_outcomes_not_reasoning(self):
        graph, _ = self.graph([
            {**reply(calls=[call("calculate_quote", {"secret": FAKE_KEY}, "invalid-log")]), "reasoning_content": "PRIVATE THOUGHTS"},
            reply("Which laundry service and quantity do you mean?"),
        ])
        result = agent.run_turn(graph, "logging", "Help with a quote")
        logs = json.dumps(self.activity)
        self.assertNotIn(FAKE_KEY, logs)
        self.assertNotIn("PRIVATE THOUGHTS", json.dumps(result))
        self.assertEqual(self.activity[0]["tool"], "calculate_quote")
        self.assertEqual(self.activity[0]["error"], "validation_error")

    def test_prompt_requires_grounding_clarification_read_only_and_no_dates(self):
        for instruction in ("on their results", "Ask for a missing", "Bookings CANNOT", "NO DATES", "technical_failure", "no customer address fields"):
            self.assertIn(instruction, agent.SYSTEM_PROMPT)
        graph, _ = self.graph([reply("Bookings cannot be created through this chat. Which service would you like a quote for?")])
        with patch.object(shop, "create_booking", side_effect=AssertionError("No booking allowed")):
            result = agent.run_turn(graph, "no-booking", "Book laundry for tomorrow")
        self.assertIn("cannot", result["messages"][-1]["content"])
        self.assertEqual(database_rows(self.database), self.before)

    def test_user_and_step_configuration_limits(self):
        graph, model = self.graph([reply("unused")])
        for session, message in (("", "Hello"), ("s", ""), ("s", "x" * 2001)):
            with self.assertRaises(agent.ChatInputError):
                agent.run_turn(graph, session, message)
        self.assertEqual(model.requests, [])
        with self.assertRaises(ValueError):
            agent.build_agent(model, checkpointer=graph.checkpointer, max_tool_calls=0)

    def test_nebius_adapter_uses_verified_protocol_and_total_call_budget(self):
        config = Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", "offline-model")
        response = ChatCompletion.model_validate({
            "id": "offline-sdk", "object": "chat.completion", "created": 0, "model": "offline-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hello"}}],
        })
        with patch.object(agent, "OpenAI") as constructor:
            client = constructor.return_value
            client.chat.completions.create.return_value = response
            adapter = agent.NebiusModel(config, max_total_calls=1)
            self.assertEqual(adapter([{"role": "user", "content": "Hello"}])["content"], "Hello")
            with self.assertRaises(agent.ModelCallLimit):
                adapter([])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(client.chat.completions.create.call_count, 1)
            self.assertEqual(client.chat.completions.create.call_args.kwargs["tools"], agent.TOOLS)
            self.assertEqual(client.chat.completions.create.call_args.kwargs["tool_choice"], "auto")
            self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
            self.assertEqual(constructor.call_args.kwargs["timeout"], 20.0)
            adapter.close()

    def test_chat_startup_initializes_explicitly_and_new_session_is_independent(self):
        model = FakeModel([reply("First conversation."), reply("New conversation.")])
        config = Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", "offline-model")
        output = io.StringIO()
        with patch.object(chat, "load_config", return_value=config), patch.object(chat, "NebiusModel", return_value=model):
            with patch.object(storage, "initialize_database", wraps=storage.initialize_database) as initialize:
                with patch("builtins.input", side_effect=["Hello", "/new", "Hello", "/quit"]), redirect_stdout(output):
                    status = chat.main(["--checkpoint-db", str(self.project / "chat_state.db"),
                                        "--business-db", str(self.database)])
        self.assertEqual(status, 0)
        initialize.assert_called_once()
        model.close.assert_called_once()
        self.assertEqual(len(model.requests[1]), 2)
        self.assertIn("cannot create bookings", output.getvalue())
        self.assertEqual(database_rows(self.database), self.before)

    def test_imports_do_not_load_config_prompt_network_or_initialize_database(self):
        for name in ("shop.py", "storage.py", "nebius_check.py", "laundry_agent.py", "chat.py"):
            shutil.copy2(Path(__file__).parent / name, self.project / name)
        before_bytes = self.database.read_bytes()
        program = """
from unittest.mock import patch
with patch('sqlite3.connect', side_effect=AssertionError('Unexpected database access')):
    with patch('socket.create_connection', side_effect=AssertionError('Unexpected network')):
        with patch('dotenv.dotenv_values', side_effect=AssertionError('Unexpected configuration read')):
            with patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
                import laundry_agent, chat
"""
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=self.project,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.database.read_bytes(), before_bytes)
        self.assertFalse((self.project / "agent_state.db").exists())
        self.database.unlink()  # Only this test's temporary fixture.
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=self.project,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.database.exists())
        self.assertFalse((self.project / "agent_state.db").exists())
        storage.initialize_database()  # Restore fixture for evidence collection.


if __name__ == "__main__":
    unittest.main(verbosity=2)
