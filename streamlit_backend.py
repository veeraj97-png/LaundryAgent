"""Small UI adapter; the existing graph owns all business and approval logic."""
from contextlib import contextmanager
import os
from pathlib import Path
from threading import RLock

import laundry_agent as agent
from nebius_check import load_config
import storage


# storage uses a module-level path. Serialize this app's browser sessions while
# that path is selected, including reads. Do not cache SQLite connections.
_BUSINESS_LOCK = RLock()
PROJECT = Path(__file__).resolve().parent


def database_paths():
    runtime = PROJECT / "runtime"
    checkpoints, business = agent.validate_database_paths(
        os.environ.get("LAUNDRY_CHECKPOINT_DB", runtime / "ui_checkpoints.db"),
        os.environ.get("LAUNDRY_BUSINESS_DB", runtime / "ui_business.db"),
    )
    original = PROJECT / "laundry.db"
    for path in (checkpoints, business):
        if path == original or (path.exists() and original.exists() and path.samefile(original)):
            raise ValueError("The interface must use separate demo databases.")
    return checkpoints, business


class LazyModel:
    """Construct a client only when an explicit chat submission needs the model."""
    def __init__(self):
        self.model = None

    def __call__(self, messages):
        if self.model is None:
            self.model = agent.NebiusModel(load_config())
        return self.model(messages)

    def close(self):
        if self.model is not None:
            self.model.close()


@contextmanager
def open_demo():
    checkpoints, business = database_paths()
    with _BUSINESS_LOCK:
        previous = storage.DATABASE_PATH
        model = LazyModel()
        try:
            checkpoints.parent.mkdir(parents=True, exist_ok=True)
            business.parent.mkdir(parents=True, exist_ok=True)
            storage.DATABASE_PATH = business
            storage.initialize_database()
            with agent.open_agent(model, checkpoint_path=checkpoints) as graph:
                yield graph
        finally:
            model.close()
            storage.DATABASE_PATH = previous


def decide(graph, thread_id, action, request_id, snapshot_id):
    """Re-read before accepting a decision bound to the terms actually displayed."""
    pending = agent.pending_approval(graph, thread_id)
    if (pending is None or pending["request_id"] != request_id
            or pending["snapshot_id"] != snapshot_id):
        raise agent.ChatInputError("That review is no longer pending. Review the current session before acting.")
    return agent.resume_approval(graph, thread_id, action,
                                 request_id=request_id, snapshot_id=snapshot_id)
