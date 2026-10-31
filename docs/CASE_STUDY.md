# LaundryAgent: a local conversational laundry prototype

This Markdown document is a source draft for the author's Google Doc. No Google
Doc has been created by this packaging step. Author-specific reflections, course
metadata and the final recording link still need manual review and completion.

## Overview and business problem

A laundry customer needs to understand services, estimate a price, check pickup
capacity and approve clear terms before a booking is recorded. LaundryAgent
explores that workflow through a local conversational prototype. Its intended
user is a customer in a synthetic demonstration, with a trusted local application
recording approval. It is also a guided Python learning project.

The outcome is working local chat backed by business functions and SQLite. This
is not evidence of measured time savings or a production-ready business service.

## Scope and synthetic data

The catalogue contains Wash and Fold at Rs 30 per garment with an estimated
two-day turnaround, and Wash and Iron at Rs 50 with an estimated three-day
turnaround. Five wash-and-iron garments cost Rs 250. Each new pickup date starts with morning
capacity 2 (9 AM–12 PM), afternoon 0 (12 PM–4 PM), and evening 3 (4 PM–8 PM).
Orders begin empty and are created during demonstrations.

Each booking consumes one pickup capacity unit regardless of garment quantity.
Draft preparation and rejection consume none. Initialization is repeatable and
does not restore consumed capacity. New bookings require a date and window; capacity is tracked per date.
Pickup dates use India time and do not create guaranteed delivery dates. There are no real customer records,
addresses, payments, taxes or delivery scheduling in this dataset.

## Architecture and tools

Streamlit (`app.py`) and the terminal entry point (`chat.py`) call the same
LangGraph implementation (`laundry_agent.py`). Nebius provides the model through
an OpenAI-compatible API client. The inspected local model setting was
`Qwen/Qwen3-30B-A3B-Instruct-2507`; users must explicitly configure an accessible,
tool-capable model. The repository does not select a default or include secrets.

The model can request only catalogue reading, quote calculation, pickup checking,
available-slot listing and proposal preparation. It cannot call approval,
booking, superseding, arbitrary Python/SQL or general order lookup. Python
validates the name and exact arguments, calls existing `shop.py` functions, and
returns structured results with their original tool-call IDs.

`storage.py` persists business records. A separate SQLite checkpointer persists
conversation state. The UI defaults to gitignored `runtime/ui_business.db` and
`runtime/ui_checkpoints.db`, protecting the original project database. The CLI
supports explicit paths; its defaults differ and are documented in the README.

## Agent workflow and state

A graph node performs one step. Edges connect steps, and conditional edges choose
whether a model answer ends the turn, tools run, a proposal is planned or human
approval is required. Tool results return to the model for an explanation or
another validated action. Missing inputs prompt clarification rather than guessed
business values.

`ChatState` holds messages, per-turn model/tool counters, workflow status, draft
plans, pending proposal, decision and saved booking result. A checkpoint saves
that state and the next graph step. A stable thread ID restores the same history
across process restarts. Only new messages are submitted; old messages are not
appended again. A different thread starts independent conversation state while
business records remain shared in their separately configured database.

Session IDs provide continuity, not authentication or customer ownership. Old
availability messages are historical snapshots; a current availability question
must perform a new lookup.

## Human approval and exact terms

Proposal preparation persists immutable commercial details under a stable
request ID. A snapshot identifier binds approval to the stored terms. The review
includes service code/name, quantity, unit and total price, turnaround estimate,
pickup date, slot code/window, request ID and snapshot ID.

The graph interrupts rather than asking the model to authorize a booking.
Streamlit disables normal chat while pending and presents Approve/Reject buttons.
Each button is bound to the displayed IDs, revalidates the pending proposal and
calls the existing trusted `resume_approval` path. The CLI uses a trusted typed
decision. Merely editing in-memory status or sending model-generated approval
does not satisfy the storage write boundary.

Approval recording is trusted local application behaviour, not production
authentication. Another identity/ownership design would be required before
customer-facing deployment.

## Idempotency, concurrency and transactions

A request ID identifies an intended proposal across attempts; a booking ID
identifies its saved result. Generating a new request ID for each retry would
defeat duplicate protection. Persistent mappings compare canonical commercial
details. Identical completed requests return the original saved booking before
checking live capacity, so a retry can succeed even if the first booking used
the last place. Conflicting details are refused.

SQLite `BEGIN IMMEDIATE` serializes the request check/write sequence. For a new
booking, stored approval and exact terms are checked in the same transaction as
capacity reduction, order insertion, proposal transition and request mapping.
If any write fails, rollback restores the earlier database state. Rollback and
idempotency solve different problems: failed changes must be undone, while a
successful request repeated later must not book again.

The graph checkpoints a proposal ID and terms before writing the draft, making
supported replay reuse the same identity. Saved confirmation is deterministic
and includes the actual booking ID; no post-commit model explanation call is
required. Post-commit recovery retrieves the saved outcome without consuming
capacity twice.

## Failure handling and UI reruns

Technical tool failure is distinct from full/empty availability. A failed read
stops the turn, skips the remaining batch and displays safe guidance. It does
not automatically retry. Validation errors instead return to the model for
clarification. Each new customer turn is bounded by model/tool counters,
recursion and network timeout limits; there is no automated support handoff.

The CLI exits after technical failure and can be restarted with the same paths
and session. Streamlit permits another explicit question after an ended read
failure. Supported interrupted booking steps require an explicit Recover saved
outcome click in Streamlit. General interrupted network work is not universally
recoverable, and uncertain outcomes must not be treated as failed bookings.

Streamlit reruns its script after interactions. Session state holds the thread ID,
busy flag and one queued action; rendering history does not replay submissions.
The action is consumed before execution. SQLite checkpoint connections open and
close inside a context manager on each rerun. Backend idempotency remains the
final protection against repeated or stale booking actions.

## AI coding approach, prompts and iterations

The available development session shows Codex being used for small, explicitly
scoped increments: inspect current behaviour, explain a change, implement that
step, test in isolated databases and stop. Verification-only requests prohibited
application edits. Integrations were added only after the earlier Python and
storage steps were established.

Verbatim user prompt excerpts include:

> Implement one small structural step: make shop.py safe to import.

> A booking must use the exact proposal that the customer explicitly approved.

> Do not interpret this as proof of retry safety: we are testing rollback only.

Reconstructed iteration summary: business functions and persistence were followed
by lookup/import safety, idempotency, persistent approval, one Nebius tool round
trip, a conversational graph, persistent checkpoints, agent-assisted approval,
and Streamlit rerun protection. These are summaries of available session
instructions and current code, not verbatim prompts or a complete Git history.

Technical lessons illustrated are separating model suggestions from execution,
transactions from retries, business data from conversation state, and page
redraws from authorized side effects. The author's personal reflections are not
inferred here. See [DEVELOPMENT_JOURNEY.md](DEVELOPMENT_JOURNEY.md) for evidence
and author-input items.

## Actual evaluation evidence

During submission preparation, all **107 current reusable unittest cases passed
with 0 failures, 0 errors and 0 skips**, once, in a temporary source copy and a
fresh CPython 3.12.10 virtual environment. No real `.env` or original database was
copied. Tests use fake responses and real graph/business logic with temporary
databases, including subprocess restarts, concurrency and Streamlit AppTest.
Fresh dependency installation and `pip check` passed. Original Python files and
databases retained their hashes and modification times during that packaging step.

The suite covers quote dispatch, conditional routing, multi-call sequencing,
session isolation, pending approval restart, rejection, altered terms, duplicate
requests, rollback, last-slot races, post-commit recovery and rerun safety. A
preceding focused audit additionally verified explicit continuation after a
temporary read failure with an earlier confirmed booking retained. Its checks
overlap the reusable suite and are not added to the 107-case total.

Session screenshots and observations also show a live Rs 250 quote round trip,
conversational pickup checks and a user-approved Streamlit booking surviving
refresh with morning capacity 1. These are successful examples, not a repeated
model-quality, latency, cost or time-saving benchmark. No live Nebius calls were
made for packaging. Detailed evidence and untested areas are in
[EVALUATION.md](EVALUATION.md).

## Limitations and future work

Customer identity/ownership, authentication, delivery scheduling, payments, encrypted
checkpoint storage, retention policies, shared multi-user operation and general
interruption recovery remain unimplemented. The project has no production or
reliability benchmark. Mobile/other-platform testing and full transitive dependency
locking remain outside this verification. Automatic retries or human escalation
would require a separate explicit design; they are not existing features.

Future work would first depend on the course scope and a clear user need. Before
any real customer exposure, identity/authorization, data handling and operational
failure policies would need design and evaluation. This document does not promise
those integrations or add voice, external messaging or public hosting.

## Author completion before submission

- Add your name, course details and the required AI-assistance disclosure.
- Review this source, supply personal reflections and confirm the rubric mapping.
- Rehearse and record the [planned demo](DEMO_SCRIPT.md), then add its link.
- Choose sanitized evidence and copy/edit this source into your Google Doc.
- Review the local repository before deciding on a commit, remote or publication.

## Pickup date extension

A later requested step added a customer calendar, date-required tools and new
per-date/window capacity rows. The date is included in immutable terms, approvals,
orders and receipts. Legacy orders keep an unrecorded date rather than an invented
one; old pending undated proposals require replacement. Completed retries remain
valid even after the booked date passes. See [PICKUP_DATES.md](PICKUP_DATES.md)
and the latest [evaluation](EVALUATION.md) for the new test evidence.
