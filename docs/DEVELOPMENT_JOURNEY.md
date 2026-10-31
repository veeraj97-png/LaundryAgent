# Development journey: evidence, prompts and lessons

This record is based on the current source/tests and the user instructions and
observations available in this development session. The sequence below follows
those requested milestones. It does not assign unverified dates, infer hours
worked, or present personal reflections as the author's own words. The current
source is the final implementation, not a complete archive of earlier versions.

## Verbatim available development prompt excerpts

These are exact excerpts from the user's requests in this session, not prompts
invented afterward:

> This is a guided learning project: do not add features or rewrite the application.

> Do not interpret this as proof of retry safety: we are testing rollback only.

> Implement one small structural step: make shop.py safe to import.

> A booking must use the exact proposal that the customer explicitly approved.

> A page redraw, sidebar interaction, or browser refresh must not repeat a
> model call, approval, or booking automatically.

> Do not add LangGraph yet.

The final excerpt belongs to the earlier Nebius-only milestone. LangGraph was
introduced later in a separate explicit request. These development instructions
are different from the runtime assistant `SYSTEM_PROMPT` in `laundry_agent.py`.

## Milestones and verified lessons

The descriptions in this table are reconstructed summaries of available session
instructions and current code evidence, not verbatim historical code changes.

| Milestone | Verified implementation/evidence | Technical lesson illustrated |
|---|---|---|
| Small-step Python | Catalogue and functions in [shop.py](../shop.py); narrow requests explicitly prevented unrelated integrations. | Isolate a learning objective and preserve tested behaviour between steps. |
| Business functions and validation | `calculate_quote`, pickup checks and strict graph argument validation; `test_laundry_agent.py` tests types, missing keys, unsupported inputs and duplicate JSON keys. | The model suggests calls; Python owns validation and arithmetic. |
| SQLite persistence | `initialize_database`, orders/slots tables and `get_booking`; lookup and restart tests. | Data can outlive a Python process; `SELECT` retrieves it without booking. |
| Transactions and rollback | `save_booking` couples capacity, order, proposal and mapping updates; duplicate-order and forced-mapping failures in `test_approval_integrity.py`. | A transaction rolls back a failed group of changes. This alone does not prevent a successful repeated request. |
| Import-safe modules | `main()` guards in application entry points; silent-import tests. | Defining functions must not prompt or initialize databases merely because another module imports them. |
| Idempotency and concurrency | Stable request IDs, canonical business details, `booking_requests`, `BEGIN IMMEDIATE`; identical-request and last-slot competition tests. | Request identity survives retries; SQLite serializes the check/write sequence. A booking ID identifies the saved result. |
| Immutable proposals and trusted approval | Stored proposal terms, snapshot identifiers, decision transitions and transactional checks. | Editing local status is not authorization. The storage write path must compare the exact approved terms. |
| Nebius tool round trip | `nebius_check.py` plus fake-protocol tests; session screenshot reports a live quote round-trip PASS at Rs 250. | The model requests a tool, the application validates/executes it, and the result returns to the model. Model discovery is not a tool-support test. |
| LangGraph routing and persistent state | `build_agent`, conditional edges, `ChatState`, `SqliteSaver`, real subprocess restart tests. | Conversation state and business records persist independently. A stable thread ID selects history, not a customer identity. |
| Approval interrupts and replay | Checkpointed proposal plans, `interrupt`, `resume_approval`, `recover_session`; replay, stale-ID and post-commit crash tests. | A replay may rerun a node. Persist stable identity before writes and retrieve committed results rather than inventing new bookings. |
| Streamlit reruns | `queue_action`, `render_app`, `open_demo`, `decide`; AppTest and Safari/session evidence. | Redrawing a page must not replay model calls or decisions. Buttons bind to the displayed snapshot and use the existing trusted backend. |

## Iterations visible in the available evidence

1. Initial behaviour and rollback were tested in temporary copies before adding
   lookup or structural changes. Application code was explicitly protected during
   verification-only steps.
2. `shop.py` was made import-safe so later tools could call business functions
   without starting its demonstration CLI.
3. Duplicate-booking protection was requested separately from rollback. Stable
   request identity and persistent mappings address successful retries.
4. Persistent approval was then requested because mutable in-memory status could
   not prove human approval of exact terms.
5. Nebius integration began as one bounded quote tool round trip. The available
   screenshots show model discovery, an initial model-access check failure and a
   later successful tool result. They do not establish the reason for every
   configuration edit or a benchmark across models.
6. A conversational read-only graph preceded proposal preparation. In-memory
   conversation state was later replaced with separate persistent checkpoints.
7. Agent-assisted proposals added a trusted human interrupt and deterministic
   saved confirmation. Later checks exercised rejection, restart, duplicate
   resume, slot exhaustion and post-commit response failure.
8. Streamlit reused the graph instead of implementing another agent loop. The
   user demonstrated a quote, approved a proposal, and reported refresh preserved
   the booking; the screenshot showed one morning capacity unit remaining.
9. A focused readiness audit found packaging gaps and verified read-failure
   continuation with temporary checks. The current documentation step addresses
   those packaging gaps without changing application behaviour.

## What Codex contributed, and what this does not establish

Available session evidence shows Codex inspecting files, implementing requested
increments, consulting framework/provider documentation during integration,
running isolated checks, and explaining code and results. User instructions
set scope and approval boundaries; the user entered credentials locally and
made the observed approval decision in the live interface.

This is not evidence that every generated line was correct on first attempt,
that every possible failure was tested, or that a particular amount of time was
saved. Current reusable-suite results are recorded once in
[EVALUATION.md](EVALUATION.md), separately from historical demonstrations.

## Author input still needed

- Add your own reflections: what you understood, found difficult and would now
  implement differently. These cannot be inferred from code or supplied for you.
- Confirm course/Week 3 rubric wording, authorship/AI-use disclosure requirements,
  and any required dates or original prompt exports.
- Select sanitized screenshots and recording excerpts you want submitted.
  Raw conversation exports may contain local paths or account information;
  review them privately rather than publishing them wholesale.
