"""Local Streamlit demo. Importing this file does not start the app."""
import json
import logging
from base64 import b64encode
from html import escape
from pathlib import Path
from uuid import uuid4

import streamlit as st

import laundry_agent as agent
from nebius_check import ConfigurationError, load_config, safe_text
import shop
import storage
from streamlit_backend import decide, open_demo


def queue_action(kind, *details):
    # Callbacks only queue one event. Consume it before any external work so a
    # later redraw cannot repeat a submission. SQLite remains booking authority.
    if not st.session_state.get("busy", False):
        st.session_state["action"] = (st.session_state["thread_id"], kind, details)
        st.session_state["busy"] = True


def submit_chat():
    queue_action("chat", st.session_state["chat_text"])


def submit_order(intent):
    """The form submits a customer request to the same graph, never a booking."""
    service = shop.services[st.session_state["order_service"]]["display_name"]
    quantity = st.session_state["order_quantity"]
    if intent == "quote":
        text = f"What would {service} cost for {quantity} garments?"
    else:
        slot = st.session_state["order_slot"]
        text = f"Prepare a proposal for {service}, {quantity} garments, {slot} pickup."
    queue_action("chat", text)


def render_terms(proposal, clean):
    """Readable customer terms; technical references remain available below."""
    quote, pickup = proposal["quote"], proposal["pickup"]
    st.markdown(f"### {clean(quote['service_name'])}")
    st.write(f"{quote['quantity']} garments × Rs {quote['unit_price']} each")
    st.metric("Total", f"Rs {quote['total_price']}")
    st.markdown(f"**Pickup window**  \n{clean(pickup['display_name'])} · {clean(pickup['slot_code'].title())}")
    st.write(f"Estimated turnaround: {quote['turnaround_days']} days")
    st.caption("Undated pickup. Turnaround is an estimate, not a delivery-date guarantee.")


def confirmed_receipt(content):
    """Only show a receipt when its terms agree with persisted business records."""
    prefix = "Booking confirmed! Saved booking details:\n"
    if not content.startswith(prefix):
        return None
    try:
        booking = json.loads(content[len(prefix):])
        order = storage.get_booking(booking["booking_id"])
        proposal = storage.get_proposal(booking["request_id"])
        quote = booking["quote"]
        if (not order or not proposal or proposal["status"] != "booked"
                or proposal["snapshot_id"] != booking["snapshot_id"]
                or storage._canonical_booking_details(proposal) != storage._canonical_booking_details(booking)
                or any(order[key] != expected for key, expected in {
                    "service_code": quote["service_code"], "quantity": quote["quantity"],
                    "total_price": quote["total_price"], "slot_code": booking["pickup"]["slot_code"],
                    "status": "confirmed"}.items())):
            return None
        return booking
    except (ValueError, KeyError, TypeError):
        return None


def render_message(message, clean, index):
    role, content = message["role"], message.get("content")
    if role not in ("user", "assistant") or not content:
        return
    with st.chat_message(role, avatar="🧺" if role == "assistant" else "👤"):
        if role == "assistant" and content.startswith("Booking confirmed! Saved booking details:\n"):
            booking = confirmed_receipt(content)
            if booking is None:
                st.warning("We couldn't verify this booking confirmation. Check the saved outcome before trying again.")
                return
            with st.container(border=True, key=f"receipt_{index}_{booking['booking_id']}"):
                st.markdown("**✓ Booking confirmed!**")
                st.caption("YOUR LAUNDRY RECEIPT")
                quote, pickup = booking["quote"], booking["pickup"]
                st.markdown(f"### {clean(quote['service_name'])} · Rs {quote['total_price']}")
                st.write(f"{quote['quantity']} garments × Rs {quote['unit_price']} each")
                st.markdown(f"**Pickup:** {clean(pickup['slot_code'].title())} · {clean(pickup['display_name'])}")
                st.write(f"Estimated turnaround: {quote['turnaround_days']} days")
                st.caption("Undated pickup window · turnaround is an estimate.")
                st.markdown("**Booking reference**")
                st.markdown(clean(booking["booking_id"]))
                st.caption("Saved in this local demo. Refreshing won't book again.")
        else:
            st.write(clean(content))


def render_sidebar(thread, busy):
    st.sidebar.markdown('<div class="sidebar-brand"><span class="brand-mark">la</span> LaundryAgent</div>', unsafe_allow_html=True)
    st.sidebar.caption("YOUR PERSONAL LAUNDRY DESK")
    st.sidebar.button("New conversation", key="new", on_click=queue_action, args=("new",), disabled=busy, width="stretch", type="primary")
    with st.sidebar.expander("Saved conversations"):
        st.caption("Keep this reference to pick up where you left off.")
        st.code(thread, language=None)
        st.text_input("Session ID to resume", key="resume_name", max_chars=128, disabled=busy)
        st.button("Resume conversation", key="resume", on_click=queue_action, args=("resume",), disabled=busy, width="stretch")
        st.caption("Starting fresh keeps your saved bookings and current capacity.")
    st.sidebar.divider()
    st.sidebar.markdown("**A simple way to laundry**")
    st.sidebar.markdown("**01**  Choose a service  \n**02**  Review your price & pickup  \n**03**  Approve when you're ready")
    st.sidebar.divider()
    st.sidebar.caption("LOCAL DEMO · SYNTHETIC DATA")
    st.sidebar.caption("Session IDs are not authentication. No customer identity or ownership checks are implemented. Use fictional details only.")


def render_services(slots, busy):
    st.markdown("#### A little care goes a long way")
    for code, details in shop.services.items():
        icon = "✧" if code == "wash_and_fold" else "♧"
        st.markdown(f'<div class="service-card"><span class="service-icon">{icon}</span><div><strong>{escape(details["display_name"])}</strong><small>Estimated {details["turnaround_days"]}-day turnaround</small></div><div class="service-price">Rs {details["price_per_item"]}<small>per garment</small></div></div>', unsafe_allow_html=True)
    st.markdown("#### Pickup windows")
    st.caption("Current capacity · no dates assigned")
    for slot in slots:
        capacity = slot["remaining_capacity"]
        status = f"{capacity} remaining" if capacity else "Full"
        style = "open" if capacity else "full"
        st.markdown(f'<div class="slot-row"><div><strong>{escape(slot["slot_code"].title())}</strong><small>{escape(slot["display_name"])}</small></div><span class="slot-badge {style}">{status}</span></div>', unsafe_allow_html=True)
    st.button("Refresh availability", key="refresh_availability", disabled=busy, width="stretch")


def render_order_form(slots, disabled):
    available = [slot["slot_code"] for slot in slots if slot["remaining_capacity"] > 0]
    windows = {slot["slot_code"]: slot["display_name"] for slot in slots}
    with st.form("order_builder", enter_to_submit=False):
        st.markdown("### Start with your laundry")
        st.caption("Choose your details. We'll show you a proposal before anything is booked.")
        st.selectbox("Service", list(shop.services), format_func=lambda code: shop.services[code]["display_name"], key="order_service", disabled=disabled)
        st.number_input("Number of garments", min_value=1, max_value=1000, value=5, step=1, key="order_quantity", disabled=disabled)
        st.selectbox("Pickup window", available, format_func=lambda code: f"{code.title()} · {windows[code]}", key="order_slot", disabled=disabled or not available, placeholder="No pickup windows available" if not available else "Choose a window")
        if not available:
            st.info("Pickup is currently full. You can still get a price estimate.")
        st.form_submit_button("Review proposal →", on_click=submit_order, args=("proposal",), disabled=disabled or not available, type="primary", width="stretch")
        st.form_submit_button("Just get a quote", on_click=submit_order, args=("quote",), disabled=disabled, width="stretch")
        st.caption("No payment or capacity reserved at this step.")


def render_activity(messages, clean):
    with st.expander("Tool activity", expanded=False):
        found = False
        for message in messages:
            if message.get("role") != "tool":
                continue
            found = True
            try:
                result = json.loads(message["content"])
                name = message.get("name")
                arguments = result.get("arguments", {})
                # Only validated commercial inputs; never raw model arguments.
                safe_arguments = {k: v for k, v in arguments.items()
                                  if k in ("service_code", "quantity", "slot_code")} if isinstance(arguments, dict) else {}
                st.json({"tool": name if name in agent.TOOL_NAMES else "unsupported",
                         "arguments": json.loads(clean(json.dumps(safe_arguments))),
                         "outcome": clean(str(result.get("outcome", "error"))),
                         "error": clean(str((result.get("error") or {}).get("code", "")))})
            except (ValueError, TypeError, KeyError):
                st.caption("A stored tool result could not be displayed.")
        if not found:
            st.caption("No tools have run in this conversation.")


def render_app(graph, config, config_problem):
    secret = config.api_key if config else ""
    clean = lambda value: safe_text(value, secret)
    event = st.session_state.pop("action", None)
    if event:
        thread, kind, details = event
        try:
            with st.spinner("Working… Please wait before submitting again."):
                if kind == "new":
                    st.session_state["thread_id"] = str(uuid4())
                elif kind == "resume":
                    name = st.session_state["resume_name"].strip()
                    if not name or len(name) > 128 or not name.isprintable():
                        raise agent.ChatInputError("Enter a session ID of 1–128 printable characters.")
                    st.session_state["thread_id"] = name
                elif thread != st.session_state["thread_id"]:
                    raise agent.ChatInputError("The conversation changed. Please review it again.")
                elif kind == "chat":
                    if config_problem:
                        raise agent.ChatInputError("Configure Nebius locally before sending a message.")
                    agent.run_turn(graph, thread, details[0])
                elif kind == "recover":
                    agent.recover_session(graph, thread)
                else:
                    decide(graph, thread, kind, *details)
        except agent.ChatInputError:
            st.session_state["notice"] = "The action could not be applied. Check the current proposal/session; interrupted work may need explicit recovery."
        except Exception:
            st.session_state["notice"] = "A technical failure stopped the action. Use Recover saved outcome before attempting another booking. No outcome is assumed."
        finally:
            st.session_state["busy"] = False

    thread = st.session_state["thread_id"]
    st.query_params["session"] = thread
    busy = st.session_state.get("busy", False)
    render_sidebar(thread, busy)

    if config_problem:
        st.warning(config_problem)
    notice = st.session_state.pop("notice", None)
    if notice:
        st.warning(notice)
    snapshot = graph.get_state({"configurable": {"thread_id": thread}})
    messages = snapshot.values.get("messages", [])
    pending = agent.pending_approval(graph, thread)
    unfinished = False
    try:
        agent.session_snapshot(graph, thread)
    except agent.UnfinishedTurnError:
        unfinished = True
    needs_recovery = unfinished or bool(snapshot.values.get("booking_error"))
    slots = sorted(storage.read_pickup_slots(), key=lambda slot: {"morning": 0, "afternoon": 1, "evening": 2}.get(slot["slot_code"], 3))
    if needs_recovery:
        st.warning("This turn is incomplete or its booking outcome needs checking. It is not a confirmed answer. Recovery runs only when you click below.")
        st.button("Recover saved outcome", key="recover", on_click=queue_action, args=("recover",), disabled=busy)
    elif snapshot.values.get("stop_reason") == "technical_failure":
        st.warning("Connection or service failure: check your network and local Nebius configuration. No booking is confirmed by this failed turn.")

    disabled = busy or pending is not None or needs_recovery or config_problem is not None
    conversation, details = st.columns([1.65, 1], gap="large")
    with conversation:
        st.markdown("### Let's take care of your laundry")
        st.caption("Ask a question, or use the request builder alongside your chat.")
        with st.container(height=570, border=True, key="conversation_panel"):
            if not messages:
                with st.chat_message("assistant", avatar="🧺"):
                    st.markdown("**Hi there. A fresh start begins here.**")
                    st.write("I can help you find a price, check pickup windows and put together your laundry request. You'll always review the details before booking.")
                st.button("Price wash & iron for 5 garments", key="quick_quote", on_click=queue_action, args=("chat", "What would it cost to wash and iron five garments?"), disabled=disabled, width="stretch")
                st.button("Show available pickup windows", key="quick_slots", on_click=queue_action, args=("chat", "Which pickup windows are currently available?"), disabled=disabled, width="stretch")
            for index, message in enumerate(messages):
                render_message(message, clean, index)
            if pending:
                with st.chat_message("assistant", avatar="🧺"):
                    st.markdown("**Your proposal is ready to review.**")
                    st.write("Check your service, price and pickup window in the review panel. Choose Approve to book, or Reject to start again. Nothing is reserved yet.")
        st.chat_input("Ask us about prices, pickup, or your next laundry request…",
                      key="chat_text", on_submit=submit_chat, max_chars=2000, disabled=disabled)
        if pending:
            st.caption("Your proposal is ready. Approve or reject it in the review panel before continuing.")
        render_activity(messages, clean)
    with details:
        if pending:
            with st.container(border=True, key="proposal_review"):
                st.caption("ONE LAST CHECK")
                st.markdown("### Everything look right?")
                st.caption("Review your details. Nothing is reserved yet.")
                render_terms(pending, clean)
                with st.expander("Proposal reference & full details"):
                    terms = {key: pending[key] for key in ("request_id", "snapshot_id", "quote", "pickup")}
                    st.json(json.loads(clean(json.dumps(terms))))
                binding = (pending["request_id"], pending["snapshot_id"])
                st.button("Approve", key=f"approve:{thread}:{binding[0]}:{binding[1]}", on_click=queue_action, args=("approve", *binding), disabled=busy, type="primary", width="stretch")
                st.button("Reject", key=f"reject:{thread}:{binding[0]}:{binding[1]}", on_click=queue_action, args=("reject", *binding), disabled=busy, width="stretch")
        else:
            render_order_form(slots, disabled)
        with st.expander("Services & current pickup availability", expanded=not messages and not pending):
            render_services(slots, busy)
    st.caption("Local demo · fictional prices and synthetic data. Pickup windows have no dates; no payment is collected.")


def main():
    st.set_page_config(page_title="LaundryAgent · Your laundry, sorted", page_icon="🧺", layout="wide")
    # Embed the bundled decorative asset: no remote image request or tracking.
    background = b64encode((Path(__file__).parent / 'assets/laundry-room-blue.png').read_bytes()).decode('ascii')
    css = Path(__file__).with_name('ui_styles.css').read_text()
    st.markdown('<style>:root {--laundry-background: url("data:image/png;base64,' + background + '");}' + css + '</style>', unsafe_allow_html=True)
    with st.container(key="hero_banner"):
        st.markdown('<div class="page-topline"><span>LAUNDRY, WITH A LITTLE LESS EFFORT</span><span class="demo-pill">LOCAL DEMO</span></div>', unsafe_allow_html=True)
        st.title("Your laundry, sorted.")
        st.markdown('<p class="hero-description">Clean clothes. Clear prices. You’re in control.<br>Choose a service, find a pickup window, and review before you book.</p>', unsafe_allow_html=True)
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if "thread_id" not in st.session_state:
        candidate = st.query_params.get("session", "")
        st.session_state["thread_id"] = candidate if 1 <= len(candidate) <= 128 and candidate.isprintable() else str(uuid4())
    config, problem = None, None
    try:
        config = load_config()
        if config.model is None:
            problem = "Set NEBIUS_MODEL in your local .env to the model you verified. No model is selected automatically."
    except ConfigurationError:
        problem = "Nebius configuration is missing or invalid. Edit this project's local .env using .env.example: set NEBIUS_API_KEY, NEBIUS_BASE_URL and NEBIUS_MODEL. Do not paste credentials into chat."
    try:
        with open_demo() as graph:
            render_app(graph, config, problem)
    except Exception:
        st.error("The demo could not read its database or session state. Check the separate demo database paths and file permissions. No action will be retried automatically.")


if __name__ == "__main__":
    main()
