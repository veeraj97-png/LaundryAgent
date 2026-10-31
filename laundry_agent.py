"""Proposal-assisted LangGraph with a trusted approval pause and no import-time I/O."""
from copy import deepcopy
from contextlib import contextmanager
import json
from operator import add
from pathlib import Path
import re
from typing import Annotated, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from openai import APIError, OpenAI

import shop
import storage
from nebius_check import IncompleteCheck, _unique_json_object, error_message


SYSTEM_PROMPT = """You are LaundryAgent, a conversational laundry assistant.
Use only the offered tools. Base all prices, catalogue facts and pickup availability
on their results, never invented values. For a price or availability question, use
the appropriate tool this turn once you have the needed information.
Past availability results are historical snapshots, not current availability.
Ask for a missing service, garment quantity or pickup slot rather than guessing.
Use conversation history to interpret follow-ups such as 'What about morning?'.
wash_and_fold means Wash and Fold; wash_and_iron means Wash and Iron.
Bookings CANNOT be created or approved by the model. You may prepare a draft
with prepare_booking_proposal only after the customer supplies service, quantity
and slot. Ask for missing inputs; never guess them. Drafts do not reserve capacity.
Request proposal preparation in its own tool-call batch, after any read tools.
The application will pause for trusted terminal approve/reject actions. An earlier
'yes' or model-generated approval is not approval. Never claim a draft is booked.
Pickup slots have NO DATES. Report only their current undated capacity; never
claim availability for a particular day. Turnaround days are estimates, not
promised delivery dates. Do not calculate promised calendar delivery dates.
The prototype has no customer address fields; do not request or claim to save an address.
An unavailable/full slot or an empty alternatives list is a valid business result.
For unsupported_input or validation_error, explain the issue and ask for valid
or missing information. Never describe technical_failure as no availability.
Keep answers short and customer-facing. Do not reveal private reasoning.
"""


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False,
        },
    }}


TOOLS = [
    _tool("read_service_catalogue", "Read the services, prices per garment and estimated turnaround days.", {}),
    _tool("calculate_quote", "Calculate a quote in Indian rupees; does not create a booking.", {
        "service_code": {"type": "string", "enum": ["wash_and_fold", "wash_and_iron"]},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
    }, ("service_code", "quantity")),
    _tool("check_pickup_slot", "Check current capacity for an undated pickup slot.", {
        "slot_code": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
    }, ("slot_code",)),
    _tool("get_available_pickup_slots", "List undated slots whose current capacity is positive.", {}),
    _tool("prepare_booking_proposal", "Save a draft for human review, not a booking or reservation. Requires all three customer-supplied inputs.", {
        "service_code": {"type": "string", "enum": ["wash_and_fold", "wash_and_iron"]},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
        "slot_code": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
    }, ("service_code", "quantity", "slot_code")),
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}
MAX_HISTORY_MESSAGES = 80
MAX_HISTORY_CHARACTERS = 60000
DEFAULT_CHECKPOINT_PATH = Path(__file__).with_name("agent_state.db")


class ChatState(TypedDict):
    # Append messages; scalar counters are replaced and reset for each turn.
    messages: Annotated[list[dict], add]
    model_calls: int
    tool_calls: int
    stop_reason: str | None
    turn_status: str
    planned_drafts: dict
    pending_proposal: dict | None
    approval_decision: dict | None
    decision_error: str | None
    booking: dict | None
    booking_error: str | None


class ModelCallLimit(RuntimeError):
    pass


class ChatInputError(ValueError):
    pass


class UnfinishedTurnError(ChatInputError):
    pass


class NebiusModel:
    """Keep credentials/client outside graph state; count even failed attempts."""
    def __init__(self, config, *, max_total_calls=100):
        if config.model is None:
            raise ValueError("Select NEBIUS_MODEL locally before starting the chat.")
        if type(max_total_calls) is not int or max_total_calls < 1:
            raise ValueError("The total model-call limit must be positive.")
        self.model = config.model
        self.calls = 0
        self.max_total_calls = max_total_calls
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url,
                             timeout=20.0, max_retries=0)

    def __call__(self, messages):
        if self.calls >= self.max_total_calls:
            raise ModelCallLimit("Total model-call limit reached.")
        self.calls += 1
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=TOOLS, tool_choice="auto",
            max_tokens=384, n=1, store=False,
        )
        if len(response.choices) != 1 or response.choices[0].finish_reason in ("length", "content_filter"):
            raise IncompleteCheck("Incomplete model response.")
        return response.choices[0].message.model_dump(
            include={"role", "content", "tool_calls"}, exclude_none=True,
        )

    def close(self):
        self.client.close()


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def validate_tool_arguments(name, raw_arguments):
    """Return (arguments, error), without executing a tool or writing a draft."""
    if not isinstance(name, str) or name not in TOOL_NAMES:
        return None, _error("unsupported_input", "Only the catalogue, quote, availability and draft-preparation tools are allowed.")
    if not isinstance(raw_arguments, str) or len(raw_arguments) > 4096:
        return None, _error("validation_error", "Arguments must be a JSON object of at most 4096 characters.")
    try:
        arguments = json.loads(raw_arguments, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, RecursionError, IncompleteCheck):
        return None, _error("validation_error", "Arguments must be valid JSON without duplicate keys.")
    required = {"calculate_quote": {"service_code", "quantity"}, "check_pickup_slot": {"slot_code"},
                "prepare_booking_proposal": {"service_code", "quantity", "slot_code"}}.get(name, set())
    if not isinstance(arguments, dict) or set(arguments) != required:
        return None, _error("validation_error", f"Provide exactly these arguments: {', '.join(sorted(required)) or 'none'}.")
    if name in ("calculate_quote", "prepare_booking_proposal"):
        if arguments["service_code"] not in ("wash_and_fold", "wash_and_iron"):
            return None, _error("unsupported_input", "Choose wash_and_fold or wash_and_iron.")
        if type(arguments["quantity"]) is not int or not 1 <= arguments["quantity"] <= 1000:
            return None, _error("validation_error", "Give a positive whole garment quantity, up to 1000 for this chat.")
    if name in ("check_pickup_slot", "prepare_booking_proposal") and arguments["slot_code"] not in ("morning", "afternoon", "evening"):
        return None, _error("unsupported_input", "Choose morning, afternoon or evening. These slots have no dates.")
    return arguments, None


def dispatch_tool(name, raw_arguments, *, draft=None):
    """Only the graph may supply a previously checkpointed draft for persistence."""
    arguments, error = validate_tool_arguments(name, raw_arguments)
    if error:
        return error
    try:
        outcome = "success"
        if name == "read_service_catalogue":
            data = [{"service_code": code, **deepcopy(details)} for code, details in shop.services.items()]
        elif name == "calculate_quote":
            data = shop.calculate_quote(**arguments)
        elif name == "check_pickup_slot":
            data = shop.check_pickup_slot(**arguments)
            outcome = "available" if data["available"] else "unavailable"
        elif name == "get_available_pickup_slots":
            data = shop.get_available_pickup_slots()
            outcome = "available" if data else "no_availability"
        else:
            if draft is None:
                return _error("validation_error", "Draft preparation requires a checkpointed graph plan.")
            if (draft["quote"]["service_code"] != arguments["service_code"]
                    or draft["quote"]["quantity"] != arguments["quantity"]
                    or draft["pickup"]["slot_code"] != arguments["slot_code"]):
                return _error("proposal_conflict", "Draft plan does not match the requested inputs.")
            data = storage.save_proposal(draft)
            outcome = "awaiting_approval"
        return {"ok": True, "outcome": outcome, "arguments": arguments, "data": data}
    except ValueError:
        if name == "prepare_booking_proposal":
            return _error("proposal_conflict", "The draft conflicts with stored terms; prepare a new proposal.")
        return _error("technical_failure", "The business read could not verify the requested information.")
    except Exception:
        return _error("technical_failure", "The business read failed. Availability or price could not be verified.")


def _assistant_message(message):
    """Preserve wire-format call IDs; reject unusable protocol before checkpointing."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise IncompleteCheck("Invalid assistant message.")
    content = message.get("content")
    if content is not None and (not isinstance(content, str) or len(content) > 2000 or "<think>" in content):
        raise IncompleteCheck("Unsupported assistant content.")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 4:
        raise IncompleteCheck("Expected at most four tool calls per model response.")
    ids = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise IncompleteCheck("Invalid function call.")
        call_id, function = call.get("id"), call.get("function")
        if not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", call_id) or call_id in ids:
            raise IncompleteCheck("Missing or duplicate tool-call ID.")
        ids.add(call_id)
        if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", function["name"])
                or not isinstance(function.get("arguments"), str)):
            raise IncompleteCheck("Malformed function call.")
    if not calls and (not isinstance(content, str) or not content.strip()):
        raise IncompleteCheck("Empty assistant answer.")
    result = {"role": "assistant", "content": content}
    if calls:
        result["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
            "name": call["function"]["name"], "arguments": call["function"]["arguments"],
        }} for call in calls]
    return result


def build_agent(call_model, *, checkpointer, activity=None, max_model_calls=4, max_tool_calls=8):
    """Build once, then invoke repeatedly with stable thread IDs."""
    if any(type(limit) is not int or limit < 1 for limit in (max_model_calls, max_tool_calls)):
        raise ValueError("Step limits must be positive integers.")
    activity = activity or (lambda event: None)

    def stopped(reason, text):
        return {"messages": [{"role": "assistant", "content": text}],
                "stop_reason": reason, "turn_status": "stopped"}

    def model_node(state):
        if state["model_calls"] >= max_model_calls:
            return stopped("step_limit", "I stopped at the model-step limit. This turn is incomplete; please simplify the request.")
        if (len(state["messages"]) > MAX_HISTORY_MESSAGES
                or len(json.dumps(state["messages"])) > MAX_HISTORY_CHARACTERS):
            return stopped("history_limit", "Conversation limit reached. Start a new session with /new.")
        try:
            message = _assistant_message(call_model(
                [{"role": "system", "content": SYSTEM_PROMPT}] + deepcopy(state["messages"]),
            ))
            return {"messages": [message], "model_calls": state["model_calls"] + 1,
                    "turn_status": "running" if message.get("tool_calls") else "completed"}
        except ModelCallLimit:
            return stopped("step_limit", "The process model-call limit was reached. No further request was made.")
        except APIError as error:
            return stopped("technical_failure", "I could not complete this turn. " + error_message(error))
        except Exception:
            return stopped("technical_failure", "A technical failure stopped this turn. I cannot confirm an answer.")

    def tool_node(state):
        messages, used, stop_reason = [], state["tool_calls"], None
        pending = None
        for call in state["messages"][-1]["tool_calls"]:
            name = call["function"]["name"]
            if stop_reason:
                result = _error(stop_reason, "This tool was not executed because the turn was stopped.")
            elif used >= max_tool_calls:
                result = _error("step_limit", "Tool execution limit reached; this tool was not executed.")
                stop_reason = "step_limit"
            else:
                used += 1
                plan = state.get("planned_drafts", {}).get(call["id"], {})
                result = plan.get("error") or dispatch_tool(name, call["function"]["arguments"], draft=plan.get("draft"))
                if name == "prepare_booking_proposal" and result.get("ok"):
                    pending = result["data"]
                if result.get("error", {}).get("code") == "technical_failure":
                    stop_reason = "technical_failure"
            activity({"tool": name if name in TOOL_NAMES else "<unsupported>",
                      "arguments": result.get("arguments", "<invalid or not executed>"),
                      "outcome": result.get("outcome", "error"),
                      "error": result.get("error", {}).get("code")})
            messages.append({"role": "tool", "name": name, "tool_call_id": call["id"],
                             "content": json.dumps(result)})
        if stop_reason:
            messages.append({"role": "assistant", "content": (
                "A technical failure stopped the business read; I cannot verify the requested information."
                if stop_reason == "technical_failure" else
                "I stopped at the tool-step limit. This turn is incomplete; please simplify the request."
            )})
        return {"messages": messages, "tool_calls": used, "stop_reason": stop_reason,
                "pending_proposal": pending,
                "turn_status": "stopped" if stop_reason else "awaiting_approval" if pending else "running"}

    def plan_proposal(state):
        # This node only reads/builds. Its ID and terms are checkpointed before tools writes.
        calls = state["messages"][-1]["tool_calls"]
        plans = {}
        for call in calls:
            if call["function"]["name"] != "prepare_booking_proposal":
                continue
            arguments, error = validate_tool_arguments("prepare_booking_proposal", call["function"]["arguments"])
            if len(calls) != 1:
                error = _error("validation_error", "Prepare one proposal in a separate tool-call batch after any reads.")
            if error:
                plans[call["id"]] = {"error": error}
                continue
            try:
                plans[call["id"]] = {"draft": shop.build_booking_proposal(**arguments, request_id=str(uuid4()))}
            except ValueError as error:
                plans[call["id"]] = {"error": (
                    _error("slot_unavailable", "That pickup slot is full; ask for an available alternative.")
                    if "full" in str(error) else _error("technical_failure", "The draft terms could not be verified.")
                )}
            except Exception:
                plans[call["id"]] = {"error": _error("technical_failure", "The draft terms could not be verified.")}
        return {"planned_drafts": plans}

    def approval_node(state):
        # No writes, reads, random IDs or model calls before interrupt: it re-runs on resume.
        response = interrupt({"kind": "laundry_proposal_approval", "proposal": state["pending_proposal"],
                              "error": state.get("decision_error")})
        pending = state["pending_proposal"]
        valid = (isinstance(response, dict) and response.get("request_id") == pending["request_id"]
                 and response.get("snapshot_id") == pending["snapshot_id"])
        stored = storage.get_proposal(pending["request_id"])
        action = response.get("action") if valid else None
        permitted = {"approve": ("approved", "booked"), "reject": ("rejected",), "completed": ("booked",)}
        if not stored or action not in permitted or stored["status"] not in permitted[action]:
            return {"approval_decision": None, "decision_error": "An exact trusted approval or rejection has not been recorded."}
        return {"approval_decision": response, "decision_error": None, "turn_status": "running"}

    def booking_node(state):
        try:
            stored = stored_pending_proposal(state)
            booking = shop.create_booking(stored)
            return {"booking": booking, "booking_error": None, "stop_reason": None}
        except ValueError as error:
            message = ("Booking was not confirmed: the pickup slot is unavailable."
                       if "unavailable" in str(error) else "Booking was not confirmed: approval or proposal terms are invalid.")
        except Exception:
            # If commit succeeded but a later operation failed, prefer its saved outcome.
            try:
                stored = stored_pending_proposal(state)
                if stored["status"] == "booked":
                    return {"booking": shop.create_booking(stored), "booking_error": None, "stop_reason": None}
            except Exception:
                pass
            message = "The booking outcome could not be verified. Resume this session before attempting a new booking."
        return {"booking": None, "booking_error": message, "stop_reason": "booking_failure"}

    def confirmation_node(state):
        # Deliberately no post-commit LLM call: a model outage cannot hide a saved booking.
        if state.get("booking"):
            text = format_booking_confirmation(state["booking"])
            status = "completed"
        else:
            text, status = state["booking_error"], "stopped"
        return {"messages": [{"role": "assistant", "content": text}], "turn_status": status}

    def rejection_node(state):
        return {"messages": [{"role": "assistant", "content": "Proposal rejected. No booking was created and no capacity was consumed."}],
                "turn_status": "completed", "stop_reason": None}

    def after_model(state):
        if state["stop_reason"] or not state["messages"][-1].get("tool_calls"):
            return END
        return "plan_proposal" if any(call["function"]["name"] == "prepare_booking_proposal"
                                      for call in state["messages"][-1]["tool_calls"]) else "tools"

    def after_tools(state):
        if state["stop_reason"]:
            return END
        return "approval" if state.get("pending_proposal") else "model"

    def after_approval(state):
        decision = state.get("approval_decision")
        return "approval" if not decision else "rejection" if decision["action"] == "reject" else "booking"

    builder = StateGraph(ChatState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tool_node)
    builder.add_node("plan_proposal", plan_proposal)
    builder.add_node("approval", approval_node)
    builder.add_node("booking", booking_node)
    builder.add_node("confirmation", confirmation_node)
    builder.add_node("rejection", rejection_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", after_model)
    builder.add_edge("plan_proposal", "tools")
    builder.add_conditional_edges("tools", after_tools)
    builder.add_conditional_edges("approval", after_approval)
    builder.add_edge("booking", "confirmation")
    builder.add_edge("confirmation", END)
    builder.add_edge("rejection", END)
    return builder.compile(checkpointer=checkpointer)


def validate_database_paths(checkpoint_path, business_path):
    checkpoint_path = Path(checkpoint_path).expanduser().resolve()
    business_path = Path(business_path).expanduser().resolve()
    if (checkpoint_path == business_path
            or (checkpoint_path.exists() and business_path.exists()
                and checkpoint_path.samefile(business_path))):
        raise ValueError("Checkpoint and business databases must be separate files.")
    return checkpoint_path, business_path


@contextmanager
def open_agent(call_model, *, checkpoint_path=DEFAULT_CHECKPOINT_PATH, **options):
    """The compiled graph must be used only inside this connection's lifetime."""
    checkpoint_path, _ = validate_database_paths(checkpoint_path, storage.DATABASE_PATH)
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        yield build_agent(call_model, checkpointer=checkpointer, **options)


def _approval_interrupt(snapshot):
    pending = snapshot.values.get("pending_proposal") or {}
    return snapshot.next == ("approval",) and any(
        isinstance(item.value, dict) and item.value.get("kind") == "laundry_proposal_approval"
        and item.value.get("proposal", {}).get("request_id") == pending.get("request_id")
        and item.value.get("proposal", {}).get("snapshot_id") == pending.get("snapshot_id")
        for task in snapshot.tasks for item in task.interrupts
    )


def stored_pending_proposal(state):
    pending = state.get("pending_proposal")
    if not pending:
        raise ChatInputError("This session has no pending proposal.")
    stored = storage.get_proposal(pending["request_id"])
    if (stored is None or stored["snapshot_id"] != pending["snapshot_id"]
            or storage._canonical_booking_details(stored) != storage._canonical_booking_details(pending)):
        raise ChatInputError("The session proposal is stale or altered; no decision or booking is authorized.")
    return stored


def pending_approval(graph, session_id):
    snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    return stored_pending_proposal(snapshot.values) if _approval_interrupt(snapshot) else None


def format_approval(proposal):
    terms = {key: proposal[key] for key in ("request_id", "snapshot_id", "quote", "pickup")}
    return "Review exact stored proposal (draft only, no capacity reserved):\n" + json.dumps(terms, indent=2)


def format_booking_confirmation(booking):
    return "Booking confirmed! Saved booking details:\n" + json.dumps({
        key: booking[key] for key in ("booking_id", "request_id", "snapshot_id", "quote", "status")
    } | {"pickup": {key: booking["pickup"][key] for key in ("slot_code", "display_name")}}, indent=2)


def resume_approval(graph, session_id, action, *, request_id, snapshot_id):
    """Trusted CLI/harness entry, not a model tool. Bind the decision to this session."""
    if action not in ("approve", "reject"):
        raise ChatInputError("Type exactly approve or reject; approval remains pending.")
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 16}
    snapshot = graph.get_state(config)
    stored = stored_pending_proposal(snapshot.values)
    if request_id != stored["request_id"] or snapshot_id != stored["snapshot_id"]:
        raise ChatInputError("The decision does not match this session's pending request and snapshot.")
    if not snapshot.next:
        if snapshot.values.get("booking") and stored["status"] == "booked":
            return snapshot.values  # Duplicate resume is not a new approval or booking.
        if snapshot.values.get("booking_error") and stored["status"] == "booked":
            return recover_session(graph, session_id)
        if (snapshot.values.get("approval_decision") or {}).get("action") == "reject" and action == "reject":
            return snapshot.values
        raise ChatInputError("This proposal flow has ended; its decision is not re-applied.")
    if not _approval_interrupt(snapshot):
        return recover_session(graph, session_id)
    if stored["status"] == "booked":
        action = "completed"  # Recover a completed result; never re-approve it.
    else:
        try:
            shop.record_proposal_decision(request_id, snapshot_id, "approved" if action == "approve" else "rejected")
        except ValueError as error:
            raise ChatInputError(str(error)) from None
    return graph.invoke(Command(resume={"action": action, "request_id": request_id, "snapshot_id": snapshot_id}),
                        config, durability="sync")


def recover_session(graph, session_id):
    """Replay only the known draft/approved-booking workflow, never arbitrary work."""
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 16}
    snapshot = graph.get_state(config)
    if _approval_interrupt(snapshot):
        stored = stored_pending_proposal(snapshot.values)
        if stored["status"] == "booked":
            return resume_approval(graph, session_id, "approve", request_id=stored["request_id"], snapshot_id=stored["snapshot_id"])
        return snapshot.values
    if not snapshot.next:
        if snapshot.values.get("booking_error") and snapshot.values.get("pending_proposal"):
            stored = stored_pending_proposal(snapshot.values)
            if stored["status"] == "booked":
                # Resolve an uncertain finished turn only from an already committed result.
                booking = shop.create_booking(stored)
                graph.update_state(config, {"booking": booking, "booking_error": None,
                                             "stop_reason": None, "turn_status": "running"}, as_node="booking")
                return graph.invoke(None, config, durability="sync")
        return session_snapshot(graph, session_id).values
    state = snapshot.values
    if snapshot.next in (("booking",), ("confirmation",)):
        stored = stored_pending_proposal(state)
        decision = state.get("approval_decision") or {}
        if (decision.get("action") not in ("approve", "completed")
                or decision.get("request_id") != stored["request_id"]
                or decision.get("snapshot_id") != stored["snapshot_id"]):
            raise UnfinishedTurnError("No trusted booking decision is checkpointed for this interrupted step.")
    elif snapshot.next == ("tools",) and any(plan.get("draft") for plan in state.get("planned_drafts", {}).values()):
        pass  # The stable request ID/terms were already checkpointed before the write.
    elif snapshot.next == ("plan_proposal",):
        pass  # No draft write has happened yet.
    else:
        raise UnfinishedTurnError("This interrupted step cannot be recovered automatically; use /new.")
    return graph.invoke(None, config, durability="sync")


def session_snapshot(graph, session_id):
    """Inspect readiness; do not replay pending work or treat it as an answer."""
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
        raise ChatInputError("Use a non-empty session ID of at most 128 characters.")
    snapshot = graph.get_state({"configurable": {"thread_id": session_id}})
    if _approval_interrupt(snapshot):
        return snapshot
    messages = snapshot.values.get("messages", [])
    if snapshot.next or snapshot.values.get("turn_status") == "running":
        raise UnfinishedTurnError("This session has an unfinished turn. Automatic recovery is not enabled; use /new or another --session ID.")
    pending = []
    for message in messages:
        if message["role"] == "tool":
            if not pending or message.get("tool_call_id") != pending.pop(0):
                raise UnfinishedTurnError("This session has an incomplete tool sequence. Use /new or another --session ID.")
        else:
            if pending:
                raise UnfinishedTurnError("This session has an incomplete tool sequence. Use /new or another --session ID.")
            pending = [call["id"] for call in message.get("tool_calls", [])]
    if pending or (messages and messages[-1]["role"] != "assistant"):
        raise UnfinishedTurnError("This session has no completed response. Use /new or another --session ID.")
    return snapshot


def run_turn(graph, session_id, text):
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
        raise ChatInputError("Use a non-empty session ID of at most 128 characters.")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise ChatInputError("Enter a message of 1 to 2000 characters.")
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 16}
    prior = session_snapshot(graph, session_id).values
    if pending_approval(graph, session_id) is not None:
        raise ChatInputError("A proposal awaits a trusted approve/reject action; free-form chat cannot approve it.")
    if len(prior.get("messages", [])) >= MAX_HISTORY_MESSAGES:
        raise ChatInputError("Conversation limit reached. Start a new session with /new.")
    return graph.invoke({"messages": [{"role": "user", "content": text.strip()}],
                         "model_calls": 0, "tool_calls": 0, "stop_reason": None,
                         "turn_status": "running", "planned_drafts": {}, "pending_proposal": None,
                         "approval_decision": None, "decision_error": None,
                         "booking": None, "booking_error": None}, config, durability="sync")
