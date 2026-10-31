"""Offline tests only: simulated API responses are not evidence of Nebius access."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai import APIConnectionError, APITimeoutError, AuthenticationError
from openai.types import Model
from openai.types.chat import ChatCompletion

import nebius_check as check


FAKE_KEY = "offline-test-secret-not-a-real-key"
MODEL_ID = check.DOCUMENTED_TOOL_MODELS[0]


def tool_call(arguments=None, name="calculate_quote"):
    return {"id": "offline-call-1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(check.EXPECTED_ARGUMENTS if arguments is None else arguments),
    }}


def completion(calls=None, content=None, finish=None):
    return ChatCompletion.model_validate({
        "id": "offline-completion", "object": "chat.completion", "created": 0, "model": MODEL_ID,
        "choices": [{"index": 0, "finish_reason": finish or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
    })


def fake_client(responses=None, ids=None):
    client = Mock()
    client.models.list.return_value = SimpleNamespace(data=[
        Model(id=model_id, object="model", created=0, owned_by="offline-test")
        for model_id in ([MODEL_ID] if ids is None else ids)
    ])
    client.chat.completions.create.side_effect = responses or [
        completion([tool_call()]), completion(content="Wash and iron for five garments costs Rs 250."),
    ]
    return client


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laundry-nebius-config-")
        self.addCleanup(self.temporary.cleanup)
        self.env_file = Path(self.temporary.name) / ".env"

    def test_missing_credentials_fail_without_echoing_values(self):
        with self.assertRaisesRegex(check.ConfigurationError, "NEBIUS_API_KEY is missing"):
            check.load_config(self.env_file, {})

    def test_empty_key_and_whitespace_key_are_rejected(self):
        for key in ("", "  ", "offline secret"):
            with self.subTest(key=bool(key)), self.assertRaises(check.ConfigurationError):
                check.load_config(self.env_file, {"NEBIUS_API_KEY": key})

    def test_env_file_and_default_url_without_model(self):
        self.env_file.write_text(f"NEBIUS_API_KEY='{FAKE_KEY}'\nNEBIUS_MODEL=\n")
        config = check.load_config(self.env_file, {})
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.base_url, check.DEFAULT_BASE_URL)
        self.assertIsNone(config.model)
        self.assertNotIn(FAKE_KEY, repr(config))

    def test_exported_environment_takes_precedence(self):
        self.env_file.write_text("NEBIUS_API_KEY=file-only-fake\nNEBIUS_MODEL=file-model\n")
        config = check.load_config(self.env_file, {"NEBIUS_API_KEY": FAKE_KEY, "NEBIUS_MODEL": MODEL_ID})
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.model, MODEL_ID)
        with self.assertRaises(check.ConfigurationError):
            check.load_config(self.env_file, {"NEBIUS_API_KEY": ""})

    def test_no_variable_expansion_or_sibling_env_search(self):
        self.env_file.write_text("NEBIUS_API_KEY=${DO_NOT_EXPAND}\n")
        config = check.load_config(self.env_file, {"DO_NOT_EXPAND": "unrelated-secret"})
        self.assertEqual(config.api_key, "${DO_NOT_EXPAND}")
        child = self.env_file.parent / "child"
        child.mkdir()
        with self.assertRaises(check.ConfigurationError):
            check.load_config(child / ".env", {})

    def test_invalid_base_urls_and_model_ids_are_not_echoed(self):
        for url in ("http://api.tokenfactory.nebius.com/v1/", "https://example.com/v1/",
                    f"https://user:{FAKE_KEY}@api.tokenfactory.nebius.com/v1/",
                    f"https://api.tokenfactory.nebius.com/v1/?key={FAKE_KEY}",
                    "https://api.tokenfactory.nebius.com/wrong", "not-a-url"):
            with self.subTest(url_type=url.split(":")[0]):
                with self.assertRaises(check.ConfigurationError) as raised:
                    check.load_config(self.env_file, {"NEBIUS_API_KEY": FAKE_KEY, "NEBIUS_BASE_URL": url})
                self.assertNotIn(FAKE_KEY, str(raised.exception))
        with self.assertRaisesRegex(check.ConfigurationError, "one exact model ID"):
            check.load_config(self.env_file, {"NEBIUS_API_KEY": FAKE_KEY, "NEBIUS_MODEL": "not one id"})


class DispatchTests(unittest.TestCase):
    def test_schema_exposes_only_quote_and_precise_parameters(self):
        self.assertEqual(check.QUOTE_TOOL["function"]["name"], "calculate_quote")
        parameters = check.QUOTE_TOOL["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["required"]), {"service_code", "quantity"})
        self.assertEqual(set(parameters["properties"]), {"service_code", "quantity"})

    def test_real_function_returns_250_without_sqlite_access(self):
        with patch("sqlite3.connect", side_effect=AssertionError("SQLite must not be accessed")):
            arguments, result = check.dispatch_tool_call(tool_call())
        self.assertEqual(arguments, check.EXPECTED_ARGUMENTS)
        self.assertEqual(result, {
            "service_code": "wash_and_iron", "service_name": "Wash and Iron", "quantity": 5,
            "unit_price": 50, "total_price": 250, "turnaround_days": 3,
        })

    def test_unknown_tools_never_execute_python(self):
        for name in ("create_booking", "record_proposal_decision", "supersede_proposal", "get_booking", "__import__"):
            with self.subTest(name=name), patch.object(check, "calculate_quote") as quote:
                with self.assertRaisesRegex(check.IncompleteCheck, "Only calculate_quote"):
                    check.dispatch_tool_call(tool_call(name=name))
                quote.assert_not_called()

    def test_invalid_arguments_never_execute_python(self):
        arguments = [[], None, {}, {"service_code": "wash_and_iron"},
                     {"service_code": "wash_and_iron", "quantity": 5, "extra": "not-allowed"},
                     {"service_code": "night", "quantity": 5},
                     *({"service_code": "wash_and_iron", "quantity": quantity}
                       for quantity in (True, False, "5", 5.0, 0, -1, 1001, None)),
                     {"service_code": "wash_and_fold", "quantity": 5},
                     {"service_code": "wash_and_iron", "quantity": 4}]
        for value in arguments:
            call = tool_call()
            call["function"]["arguments"] = json.dumps(value)
            with self.subTest(value=value), patch.object(check, "calculate_quote") as quote:
                with self.assertRaises(check.IncompleteCheck):
                    check.dispatch_tool_call(call)
                quote.assert_not_called()

    def test_malformed_json_duplicate_keys_and_oversized_arguments(self):
        for raw in ("not-json", "{", '"text"', '{"service_code":"wash_and_iron","quantity":5,"quantity":5}',
                    " " * 2049, None):
            call = tool_call()
            call["function"]["arguments"] = raw
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(check.IncompleteCheck):
                check.dispatch_tool_call(call)

    def test_malformed_call_structures_are_rejected(self):
        for call in (None, [], {}, {"type": "custom"}, {**tool_call(), "id": ""},
                     {**tool_call(), "id": 7}, {**tool_call(), "function": None}):
            with self.subTest(call_type=type(call).__name__), self.assertRaises(check.IncompleteCheck):
                check.dispatch_tool_call(call)


class RoundTripTests(unittest.TestCase):
    def test_simulated_protocol_runs_real_quote_and_sends_matching_tool_id(self):
        client = fake_client()
        output = []
        with patch("sqlite3.connect", side_effect=AssertionError("No database access")):
            result = check.run_tool_round_trip(client, MODEL_ID, output.append)
        self.assertEqual(result["total_price"], 250)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["tools"], [check.QUOTE_TOOL])
        self.assertEqual(calls[0].kwargs["tool_choice"]["function"]["name"], "calculate_quote")
        second = calls[1].kwargs
        self.assertEqual(second["tool_choice"], "none")
        assistant, tool = second["messages"][-2:]
        self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])
        self.assertEqual(json.loads(tool["content"]), result)
        self.assertEqual(tool["name"], "calculate_quote")
        for call in calls:
            self.assertEqual(call.kwargs["max_tokens"], 256)
            self.assertFalse(call.kwargs["store"])
        self.assertIn("Tool: calculate_quote", output)
        self.assertIn("Customer response:", output[-1])

    def test_missing_multiple_or_wrong_calls_are_incomplete(self):
        for response in (completion(content="It is Rs 250."), completion([tool_call(), tool_call()]),
                         completion([tool_call(name="create_booking")])):
            client = fake_client([response])
            with self.subTest(response_type=response.choices[0].finish_reason), self.assertRaises(check.IncompleteCheck):
                check.run_tool_round_trip(client, MODEL_ID, lambda text: None)
            self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_truncated_response_is_not_a_pass(self):
        client = fake_client([completion([tool_call()], finish="length")])
        with self.assertRaisesRegex(check.IncompleteCheck, "untruncated"):
            check.run_tool_round_trip(client, MODEL_ID, lambda text: None)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_no_extra_tool_loop_and_no_empty_or_wrong_final_answer(self):
        for final in (completion([tool_call()]), completion(content=""),
                      completion(content="It costs Rs 999."), completion(content="It costs Rs 2500."),
                      completion(content="It costs Rs 250.01."),
                      completion(content="Rs 250 " + "x" * 2001)):
            client = fake_client([completion([tool_call()]), final])
            with self.subTest(final_type=final.choices[0].finish_reason), self.assertRaises(check.IncompleteCheck):
                check.run_tool_round_trip(client, MODEL_ID, lambda text: None)
            self.assertEqual(client.chat.completions.create.call_count, 2)


class CliAndPrivacyTests(unittest.TestCase):
    def run_main(self, client=None, config=None, arguments=()):
        output = io.StringIO()
        client = fake_client() if client is None else client
        config = check.Config(FAKE_KEY, check.DEFAULT_BASE_URL, MODEL_ID) if config is None else config
        with patch.object(check, "load_config", return_value=config), patch.object(check, "OpenAI") as constructor:
            constructor.return_value.__enter__.return_value = client
            with redirect_stdout(output):
                status = check.main(list(arguments))
        return status, output.getvalue(), constructor

    def test_configured_simulated_main_is_three_requests_no_retries(self):
        client = fake_client()
        status, output, constructor = self.run_main(client)
        self.assertEqual(status, 0)
        client.models.list.assert_called_once_with(extra_query={"verbose": "true"})
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 20.0)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertNotIn(FAKE_KEY, output)

    def test_missing_key_never_constructs_client(self):
        output = io.StringIO()
        with patch.object(check, "load_config", side_effect=check.ConfigurationError("NEBIUS_API_KEY is missing.")):
            with patch.object(check, "OpenAI") as constructor, redirect_stdout(output):
                status = check.main([])
        self.assertEqual(status, 2)
        constructor.assert_not_called()
        self.assertIn("LIVE TEST NOT RUN", output.getvalue())

    def test_missing_model_lists_choices_without_inference(self):
        client = fake_client()
        status, output, _ = self.run_main(client, check.Config(FAKE_KEY, check.DEFAULT_BASE_URL, None))
        self.assertEqual(status, 2)
        client.chat.completions.create.assert_not_called()
        self.assertIn(MODEL_ID, output)
        self.assertIn("not live-verified", output)
        self.assertIn("No model is selected automatically", output)
        self.assertNotIn("ROUND TRIP PASS", output)

    def test_list_only_is_not_a_tool_pass_even_with_model_configured(self):
        client = fake_client()
        status, output, _ = self.run_main(client, arguments=["--list-models"])
        self.assertEqual(status, 0)
        client.chat.completions.create.assert_not_called()
        self.assertIn("LIVE TOOL TEST NOT RUN", output)
        self.assertNotIn("ROUND TRIP PASS", output)

    def test_inaccessible_model_does_not_generate(self):
        client = fake_client(ids=["different-model"])
        status, output, _ = self.run_main(client)
        self.assertEqual(status, 1)
        self.assertIn("MODEL ACCESS CHECK FAILED", output)
        client.chat.completions.create.assert_not_called()

    def test_model_display_is_bounded_and_not_claimed_tool_capable(self):
        lines = []
        check.show_model_choices([{"id": f"offline-model-{index}"} for index in range(30)], lines.append)
        self.assertEqual(sum(line.startswith("  offline-model-") for line in lines), 20)
        self.assertIn("listing alone does NOT prove tool support", "\n".join(lines))

    def test_error_categories_suppress_raw_provider_secrets(self):
        for status, label in ((401, "AUTHENTICATION"), (403, "ACCESS"), (404, "ACCESS"),
                              (429, "RATE-LIMIT"), (400, "CAPABILITY"), (422, "CAPABILITY"), (500, "API ERROR")):
            error = Exception(FAKE_KEY)
            error.status_code = status
            message = check.error_message(error)
            self.assertIn(label, message)
            self.assertNotIn(FAKE_KEY, message)
        request = Mock()
        self.assertIn("CONNECTION ERROR", check.error_message(APIConnectionError(request=request, message=FAKE_KEY)))
        self.assertIn("TIMEOUT", check.error_message(APITimeoutError(request=request)))
        response = SimpleNamespace(status_code=401, headers={}, request=request)
        error = AuthenticationError(FAKE_KEY, response=response, body={"secret": FAKE_KEY})
        client = fake_client()
        client.models.list.side_effect = error
        status, output, _ = self.run_main(client)
        self.assertEqual(status, 1)
        self.assertIn("AUTHENTICATION ERROR", output)
        self.assertNotIn(FAKE_KEY, output)

    def test_unexpected_errors_and_echoed_secrets_are_redacted(self):
        client = fake_client()
        client.models.list.side_effect = RuntimeError(FAKE_KEY)
        status, output, _ = self.run_main(client)
        self.assertEqual(status, 1)
        self.assertNotIn(FAKE_KEY, output)
        client = fake_client([completion([tool_call()]), completion(content=f"It costs Rs 250. {FAKE_KEY}")])
        status, output, _ = self.run_main(client)
        self.assertEqual(status, 0)
        self.assertNotIn(FAKE_KEY, output)
        self.assertIn("[REDACTED]", output)
        self.assertLessEqual(len(check.safe_text("x" * 3000, FAKE_KEY)), 2000)

    def test_import_has_no_env_network_database_or_prompt_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="laundry-nebius-import-") as directory:
            project = Path(directory)
            for name in ("scheduling.py", "nebius_check.py", "shop.py", "storage.py"):
                shutil.copy2(Path(__file__).parent / name, project / name)
            program = """
from unittest.mock import patch
with patch('dotenv.dotenv_values', side_effect=AssertionError('Env read during import')):
    with patch('socket.create_connection', side_effect=AssertionError('Network during import')):
        with patch('sqlite3.connect', side_effect=AssertionError('Database during import')):
            with patch('builtins.input', side_effect=AssertionError('Prompt during import')):
                import nebius_check
"""
            process = subprocess.run([sys.executable, "-B", "-c", program], cwd=project,
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout, "")
            self.assertEqual(process.stderr, "")
            self.assertFalse((project / "laundry.db").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
