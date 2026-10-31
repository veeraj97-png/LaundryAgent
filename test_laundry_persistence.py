"""OFFLINE: labelled fake model responses, real SQLite checkpoints and subprocesses."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chat
import laundry_agent as agent
from nebius_check import Config
import storage
from test_date_helpers import TEST_DATE, seed_date
from test_laundry_agent import FakeModel, call, reply, database_rows, FAKE_KEY


EVIDENCE = []


class PersistentConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laundry-persistent-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.business = self.project / "business.db"
        self.checkpoints = self.project / "state.db"
        self.database_patch = patch.object(storage, "DATABASE_PATH", self.business)
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        storage.initialize_database()
        seed_date(self.business)
        self.before = database_rows(self.business)
        for name in ("scheduling.py", "test_date_helpers.py", "shop.py", "storage.py", "nebius_check.py", "laundry_agent.py", "chat.py", "test_laundry_agent.py"):
            shutil.copy2(Path(__file__).parent / name, self.project / name)
        self.events = []

    def tearDown(self):
        after = database_rows(self.business)
        EVIDENCE.append({"test": self.id(), "business_before": self.before,
                         "business_after": after, "events": self.events})

    def child(self, program, *arguments):
        result = subprocess.run([sys.executable, "-B", "-c", program, str(self.business), str(self.checkpoints), *arguments],
                                cwd=self.project, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_checkpoint_survives_process_exit_follow_up_is_fresh_and_not_duplicated(self):
        first = self.child("""
import json,sys
from pathlib import Path
import storage,laundry_agent as agent
from test_laundry_agent import FakeModel,call,reply
storage.DATABASE_PATH=Path(sys.argv[1])
model=FakeModel([reply(calls=[call('check_pickup_slot',{'slot_code':'evening'},'saved-evening')]),
                 reply('Evening has capacity in this undated snapshot.')])
with agent.open_agent(model,checkpoint_path=Path(sys.argv[2])) as graph:
    state=agent.run_turn(graph,'restart-session','Is evening pickup available?')
    print(json.dumps({'label':'OFFLINE fake model','state':state}))
""")
        self.assertEqual(len(first["state"]["messages"]), 4)
        self.assertEqual(first["state"]["turn_status"], "completed")
        second = self.child("""
import json,sys
from pathlib import Path
import storage,laundry_agent as agent
from test_laundry_agent import FakeModel,call,reply
storage.DATABASE_PATH=Path(sys.argv[1])
def follow_up(messages):
    assert sum(m['role']=='system' for m in messages)==1
    assert any(m.get('content')=='Is evening pickup available?' for m in messages)
    assert any(m.get('tool_call_id')=='saved-evening' for m in messages)
    assert messages[-1]['content']=='What about morning?'
    return reply(calls=[call('check_pickup_slot',{'slot_code':'morning'},'fresh-morning')])
model=FakeModel([follow_up,reply('Morning has capacity in this new undated snapshot.')])
with agent.open_agent(model,checkpoint_path=Path(sys.argv[2])) as graph:
    prior=agent.session_snapshot(graph,'restart-session').values
    new_thread=agent.session_snapshot(graph,'different-session').values
    state=agent.run_turn(graph,'restart-session','What about morning?')
    print(json.dumps({'label':'OFFLINE fake model','prior':prior,'independent':new_thread,'state':state,'requests':model.requests}))
""")
        self.assertEqual(second["prior"], first["state"])
        self.assertEqual(second["independent"], {})
        self.assertEqual(second["state"]["messages"][:4], first["state"]["messages"])
        self.assertEqual(len(second["state"]["messages"]), 8)
        self.assertEqual([m["tool_call_id"] for m in second["state"]["messages"] if m["role"] == "tool"],
                         ["saved-evening", "fresh-morning"])
        self.assertFalse(any(m["role"] == "system" for m in second["state"]["messages"]))
        self.assertEqual(second["state"]["model_calls"], 2)
        self.assertEqual(second["state"]["tool_calls"], 1)
        self.assertEqual(database_rows(self.business), self.before)
        self.events.append({"process_one": first, "process_two": second})

    def test_current_availability_after_restart_reads_changed_capacity_not_old_snapshot(self):
        model = FakeModel([reply(calls=[call("check_pickup_slot", {"slot_code": "evening"}, "old-read")]), reply("Evening is available.")])
        with agent.open_agent(model, checkpoint_path=self.checkpoints) as graph:
            old = agent.run_turn(graph, "fresh-availability", "Check evening pickup")
        connection = sqlite3.connect(self.business)
        try:
            connection.execute("UPDATE dated_pickup_slots SET remaining_capacity=0 WHERE slot_code='evening'")
            connection.commit()
        finally:
            connection.close()
        after_fixture_change = database_rows(self.business)
        result = self.child("""
import json,sys
from pathlib import Path
import storage,laundry_agent as agent
from test_laundry_agent import FakeModel,call,reply
storage.DATABASE_PATH=Path(sys.argv[1])
model=FakeModel([reply(calls=[call('check_pickup_slot',{'slot_code':'evening'},'new-read')]),reply('Evening is now full in the undated capacity data.')])
with agent.open_agent(model,checkpoint_path=Path(sys.argv[2])) as graph:
    state=agent.run_turn(graph,'fresh-availability','Is evening still available now?')
    print(json.dumps(state))
""")
        observations = [json.loads(message["content"]) for message in result["messages"] if message["role"] == "tool"]
        self.assertEqual([item["data"]["remaining_capacity"] for item in observations], [3, 0])
        self.assertEqual(observations[-1]["outcome"], "unavailable")
        self.assertEqual(result["messages"][:4], old["messages"])
        self.assertEqual(database_rows(self.business), after_fixture_change)
        self.events.append({"fixture_change_only": after_fixture_change, "resumed_state": result})

    def test_per_turn_limits_reset_across_sqlite_reopens(self):
        for index in range(3):
            model = FakeModel([reply(calls=[call("read_service_catalogue", {}, f"catalogue-{index}")]), reply("Here are the services.")])
            with agent.open_agent(model, checkpoint_path=self.checkpoints, max_model_calls=2, max_tool_calls=1) as graph:
                state = agent.run_turn(graph, "limits", f"Read catalogue, turn {index}")
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["model_calls"], 2)
            self.assertEqual(state["tool_calls"], 1)
            self.assertEqual(len(state["messages"]), 4 * (index + 1))

    def test_unfinished_checkpoint_is_not_replayed_or_accepted_as_completed(self):
        result = self.child("""
import json,sys
from pathlib import Path
import storage,laundry_agent as agent
from test_laundry_agent import FakeModel,call,reply
storage.DATABASE_PATH=Path(sys.argv[1])
model=FakeModel([reply(calls=[call('check_pickup_slot',{'slot_code':'evening'},'pending-read')])])
config={'configurable':{'thread_id':'interrupted'}}
with agent.open_agent(model,checkpoint_path=Path(sys.argv[2])) as graph:
    graph.invoke({'messages':[{'role':'user','content':'Check evening'}], 'model_calls':0,
                  'tool_calls':0,'stop_reason':None,'turn_status':'running'},
                 config,interrupt_after=['model'],durability='sync')
    state=graph.get_state(config)
    print(json.dumps({'next':state.next,'values':state.values}))
""")
        self.assertEqual(result["next"], ["tools"])
        model = FakeModel([])
        with agent.open_agent(model, checkpoint_path=self.checkpoints) as graph:
            before = graph.get_state({"configurable": {"thread_id": "interrupted"}})
            with self.assertRaisesRegex(agent.UnfinishedTurnError, "unfinished turn"):
                agent.run_turn(graph, "interrupted", "What about morning?")
            after = graph.get_state({"configurable": {"thread_id": "interrupted"}})
            self.assertEqual(before.config, after.config)
            self.assertEqual(before.values, after.values)
        self.assertEqual(model.requests, [])
        self.assertEqual(database_rows(self.business), self.before)
        self.events.append({"interrupted_checkpoint": result, "no_replay": True})

    def test_stopped_turn_is_labelled_and_new_turn_can_start_without_reusing_it(self):
        model = FakeModel([RuntimeError("offline failure")])
        with agent.open_agent(model, checkpoint_path=self.checkpoints) as graph:
            failed = agent.run_turn(graph, "stopped", "Hello")
        self.assertEqual(failed["turn_status"], "stopped")
        model = FakeModel([reply("A new completed response.")])
        with agent.open_agent(model, checkpoint_path=self.checkpoints) as graph:
            output = []
            chat._session_notice(graph, "stopped", self.checkpoints, self.business, output.append)
            self.assertIn("previous turn stopped", " ".join(output))
            self.assertFalse(any(failed["messages"][-1]["content"] in line for line in output))
            state = agent.run_turn(graph, "stopped", "Try a new question")
        self.assertEqual(state["turn_status"], "completed")
        self.assertEqual(state["model_calls"], 1)
        self.assertEqual(len(state["messages"]), 4)

    def test_checkpoint_connections_close_after_normal_exit_and_exception(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                connection = None
                try:
                    with agent.open_agent(FakeModel([reply("Hello")]), checkpoint_path=self.checkpoints) as graph:
                        connection = graph.checkpointer.conn
                        agent.run_turn(graph, f"close-{fail}", "Hello")
                        if fail:
                            raise RuntimeError("test context exit")
                except RuntimeError:
                    self.assertTrue(fail)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_checkpoint_and_business_paths_cannot_alias(self):
        model = FakeModel([])
        with self.assertRaisesRegex(ValueError, "separate files"):
            with agent.open_agent(model, checkpoint_path=self.business):
                self.fail("must not open the business file for checkpoints")
        alias = self.project / "alias.db"
        os.link(self.business, alias)
        with self.assertRaisesRegex(ValueError, "separate files"):
            agent.validate_database_paths(alias, self.business)
        self.assertEqual(database_rows(self.business), self.before)

    def test_checkpoint_schema_is_separate_from_unchanged_business_tables(self):
        with agent.open_agent(FakeModel([reply("Hello")]), checkpoint_path=self.checkpoints) as graph:
            agent.run_turn(graph, "separate-files", "Hello")
        checkpoint_tables = set(database_rows(self.checkpoints))
        self.assertIn("checkpoints", checkpoint_tables)
        self.assertFalse(checkpoint_tables & set(self.before))
        self.assertEqual(database_rows(self.business), self.before)

    def test_named_session_cli_resume_and_generated_session_commands(self):
        config = Config(FAKE_KEY, "https://api.tokenfactory.nebius.com/v1/", "offline-model")
        arguments = ["--session", "named-test", "--checkpoint-db", str(self.checkpoints), "--business-db", str(self.business)]
        for index, responses in enumerate(([reply("First saved answer.")], [])):
            output = io.StringIO()
            model = FakeModel(responses)
            with patch.object(chat, "load_config", return_value=config), patch.object(chat, "NebiusModel", return_value=model):
                with patch("builtins.input", side_effect=["Hello", "/quit"] if index == 0 else ["/quit"]), redirect_stdout(output):
                    self.assertEqual(chat.main(arguments), 0)
            self.assertIn("Session: named-test", output.getvalue())
            self.assertIn("Resume with: python chat.py --session named-test", output.getvalue())
            if index:
                self.assertIn("Resumed 2 stored messages.", output.getvalue())
                self.assertNotIn("First saved answer.", output.getvalue())
                self.assertEqual(model.requests, [])
        output = io.StringIO()
        with patch.object(chat, "load_config", return_value=config), patch.object(chat, "NebiusModel", return_value=FakeModel([])):
            with patch("builtins.input", side_effect=["/quit"]), redirect_stdout(output):
                self.assertEqual(chat.main(arguments[2:]), 0)
        resume = next(line for line in output.getvalue().splitlines() if line.startswith("Resume with: "))
        parsed = shlex.split(resume.removeprefix("Resume with: "))
        self.assertEqual(parsed[:3], ["python", "chat.py", "--session"])
        self.assertNotEqual(parsed[3], "named-test")
        self.assertIn(str(self.checkpoints.resolve()), parsed)
        self.assertEqual(database_rows(self.business), self.before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
