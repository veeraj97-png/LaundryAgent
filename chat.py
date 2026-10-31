"""Terminal entry point with persistent, local conversation checkpoints."""
import argparse
import json
import logging
from pathlib import Path
import shlex
from uuid import uuid4

from laundry_agent import (
    ChatInputError, UnfinishedTurnError, NebiusModel, DEFAULT_CHECKPOINT_PATH,
    open_agent, run_turn, session_snapshot, validate_database_paths,
    pending_approval, resume_approval, recover_session, format_approval,
)
from nebius_check import ConfigurationError, load_config, safe_text
import storage


DEFAULT_BUSINESS_PATH = Path(__file__).with_name("laundry.db")


def _session_notice(graph, session_id, checkpoint_path, business_path, emit):
    emit("Session: " + session_id)
    command = "python chat.py --session " + shlex.quote(session_id)
    if checkpoint_path != DEFAULT_CHECKPOINT_PATH.resolve():
        command += " --checkpoint-db " + shlex.quote(str(checkpoint_path))
    if business_path != DEFAULT_BUSINESS_PATH.resolve():
        command += " --business-db " + shlex.quote(str(business_path))
    emit("Resume with: " + command)
    try:
        snapshot = session_snapshot(graph, session_id)
    except UnfinishedTurnError as error:
        emit(str(error))
        return
    count = len(snapshot.values.get("messages", []))
    emit(f"Resumed {count} stored messages." if count else "New independent conversation.")
    if snapshot.values.get("turn_status") == "stopped":
        emit("The previous turn stopped without a completed model answer.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="LaundryAgent with persistent, trusted human proposal approval.")
    parser.add_argument("--session", help="Resume this thread ID, or start it if it does not exist.")
    parser.add_argument("--checkpoint-db", type=Path, default=DEFAULT_CHECKPOINT_PATH)
    parser.add_argument("--business-db", type=Path, default=DEFAULT_BUSINESS_PATH)
    arguments = parser.parse_args(argv)
    if arguments.session is not None and (not 1 <= len(arguments.session) <= 128 or not arguments.session.isprintable()):
        parser.error("--session must contain 1 to 128 printable characters.")
    try:
        checkpoint_path, business_path = validate_database_paths(arguments.checkpoint_db, arguments.business_db)
    except ValueError as error:
        parser.error(str(error))
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = load_config()
    except ConfigurationError as error:
        print(str(error))
        return 2
    emit = lambda text: print(safe_text(text, config.api_key))
    model = None
    previous_business_path = storage.DATABASE_PATH
    try:
        if config.model is None:
            emit("Set NEBIUS_MODEL in your local .env first. No model is chosen automatically.")
            return 2
        storage.DATABASE_PATH = business_path
        storage.initialize_database()  # Explicit startup only; tools perform reads.
        model = NebiusModel(config)
        with open_agent(model, checkpoint_path=checkpoint_path,
                        activity=lambda event: emit("[tool] " + json.dumps(event))) as graph:
            session_id = arguments.session or str(uuid4())
            emit("LaundryAgent: the model cannot create bookings. Drafts require a trusted terminal approve/reject action.")
            emit("Choose a pickup date (YYYY-MM-DD) and window. Dates use India time; drafts reserve no capacity.")
            emit("Conversation checkpoints persist locally. /new starts a separate session; /quit exits.")
            emit("Session IDs are not authentication. This is a local synthetic-data prototype.")
            emit("Old availability messages are historical snapshots; current questions use a fresh lookup.")
            initial_snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
            had_pending_work = bool(initial_snapshot.next or initial_snapshot.values.get("booking_error"))
            try:
                recovered = recover_session(graph, session_id)
                if had_pending_work and recovered.get("booking"):
                    emit("Recovered saved outcome:\n" + recovered["messages"][-1]["content"])
            except UnfinishedTurnError:
                pass  # The session notice explains unrelated unfinished turns.
            _session_notice(graph, session_id, checkpoint_path, business_path, emit)
            displayed_proposal = None
            while True:
                pending = pending_approval(graph, session_id)
                if pending and displayed_proposal != (pending["request_id"], pending["snapshot_id"]):
                    emit(format_approval(pending))
                    displayed_proposal = (pending["request_id"], pending["snapshot_id"])
                try:
                    text = input("Decision (approve/reject, /quit): " if pending else "You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    emit("\nChat ended.")
                    return 0
                if text.lower() in ("/quit", "quit", "exit"):
                    return 0
                if text == "/new":
                    session_id = str(uuid4())
                    displayed_proposal = None
                    _session_notice(graph, session_id, checkpoint_path, business_path, emit)
                    continue
                if not text:
                    continue
                try:
                    if pending:
                        result = resume_approval(graph, session_id, text.lower(),
                                                 request_id=pending["request_id"], snapshot_id=pending["snapshot_id"])
                    else:
                        result = run_turn(graph, session_id, text)
                except ChatInputError as error:
                    emit(str(error))
                    continue
                if pending_approval(graph, session_id):
                    continue  # Display stored terms, not an unfinished tool response.
                emit("LaundryAgent: " + result["messages"][-1]["content"])
                if result["stop_reason"] == "technical_failure":
                    emit("Chat stopped after a technical failure. Resume this session to check its saved outcome.")
                    return 1
    except KeyboardInterrupt:
        emit("\nChat stopped.")
        return 0
    except Exception:
        emit("Chat stopped after a technical failure. If approval was submitted, resume this session to check the saved outcome before starting a new booking.")
        return 1
    finally:
        storage.DATABASE_PATH = previous_business_path
        if model is not None:
            model.close()


if __name__ == "__main__":
    raise SystemExit(main())
