# LaundryAgent local Streamlit demo

This interface uses synthetic laundry data. Session IDs provide continuity,
not authentication. Run it locally; it is not a customer-facing hosted service.
Slots have no dates. Availability is a current snapshot; turnaround days are
estimates, not promised delivery dates.

## Start

```bash
cd LaundryAgent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Open the localhost URL printed in the terminal. Keep the existing verified
NEBIUS_API_KEY, NEBIUS_BASE_URL and NEBIUS_MODEL in the project's local .env.
There is no credential entry field in the interface. No model is chosen for you.

The customer interface uses a blue theme, a locally bundled AI-generated
washing-machine/dryer background, service/availability cards and
a request builder. Choose a service, garment quantity and available pickup
window, then use **Just get a quote** or **Review proposal →**. Both submit an
ordinary customer request through the existing graph; neither bypasses approval.
Full pickup windows are excluded from the builder, but price estimates remain
available if all windows are full. No dated scheduling or payment is added.

The review panel presents readable terms with full proposal references in an
expander. Confirmations display a receipt checked against stored business records
rather than a raw JSON message. **Saved conversations** in the sidebar contains
the thread reference and resume controls. Tool activity stays in its own expander.
An empty conversation includes explicit-click starter prompts; ordinary form
edits and redraws do not call the model.

Defaults are `runtime/ui_business.db` and `runtime/ui_checkpoints.db`, relative
to the project, regardless of the launching directory. The existing .gitignore
excludes runtime/ and SQLite database/sidecar files. These files are separate
from the original laundry.db; the interface refuses that original path.

For an isolated demo, set absolute paths before launching:

```bash
export LAUNDRY_BUSINESS_DB="/tmp/laundry-ui-demo/business.db"
export LAUNDRY_CHECKPOINT_DB="/tmp/laundry-ui-demo/checkpoints.db"
python -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Parent directories are created on explicit app startup. Do not point these
variables at real customer data. Business and checkpoint paths must differ.

## Manual walkthrough with the configured live model

1. Ask: "What would it cost to wash and iron five garments?" Check the Rs 250
   answer and expand Tool activity to see calculate_quote and its safe arguments.
2. Ask: "Prepare a proposal to wash and iron five garments, morning pickup."
   If morning is full, select an available alternative shown in the sidebar.
3. Review all stored terms, request ID and snapshot ID. The chat input is disabled
   while a decision is pending. Click Reject: no booking/capacity change occurs.
4. Request a new proposal and click Approve. Read the saved booking ID and
   confirmed details. The sidebar shows updated capacity.
5. Refresh the browser. History should remain without another model or booking
   call. Copy the session ID from the sidebar; use Resume conversation to reopen
   it later. The URL also includes this ID for refresh/bookmark continuity.
6. New conversation creates independent conversation history and keeps business
   data. It never resets capacity. There is no destructive reset control.

## How the code works

Streamlit reruns app.py from top to bottom after widget interactions. Ordinary
Python variables are recreated. st.session_state keeps this browser connection's
thread ID, busy flag and one queued action. Browser refresh can clear that UI
state, so the URL/session selector restores the stable thread ID and SQLite
checkpoints supply history. Session IDs are not passwords or ownership checks.

Widget callbacks queue a single action. render_app removes that action before
doing work; a redraw cannot replay it. Normal rendering only reads checkpoint
history and current availability. It never resubmits historical messages.

open_demo selects the configured demo database under a process-local lock because
the existing storage API uses a module-level path. It initializes tables without
resetting rows, opens the existing graph with its supported SQLite context
manager, and always closes its checkpoint connection and any lazy API client.
Connections are not cached across reruns or shared across browser threads.

Chat submissions call the existing run_turn, with its unchanged model/tool limits.
The model client is only constructed if that action actually calls the model.
Approve and Reject callbacks capture the displayed request ID and snapshot ID.
decide re-reads the pending proposal and checks both IDs, then calls the existing
trusted resume_approval path directly. Approval is never sent through the model.
The existing storage transaction enforces immutable approved terms and idempotency.
The existing graph supplies the deterministic confirmation containing the saved ID.

Busy state disables submissions while actions run. Repeated/stale decisions are
revalidated; database protections remain the final authority. If a step is
interrupted, redraw does not replay it. Recover saved outcome explicitly calls
the existing bounded recover_session path; recovery does not support every kind
of interrupted model/network step. Do not start another proposal when the saved
outcome is uncertain. Recover it or inspect the session first.

Tool activity is reconstructed from saved tool results and shows only allowed
tool names, validated commercial arguments and outcome/error codes. It does not
show private model reasoning, raw provider errors or credentials. Model/API
errors use the existing safe classifier; unexpected UI errors use fixed messages.

## Offline verification

```bash
python -m unittest -v test_streamlit_app
python -m unittest -v test_agent_approval test_laundry_agent test_laundry_persistence test_approval_integrity test_nebius_check
```

AppTest uses fake model responses, the real graph/approval APIs, temporary
business/checkpoint files and blocked outbound sockets. It checks quote display,
exact review terms, rejection, booking, stale decisions, redraw safety,
restoration through session selection and URL, missing configuration, connection
failures, saved-result confirmation after a commit error, explicit interrupted
booking recovery, original-database refusal and silent imports.

Automated tests do not establish live model compatibility or pixel-level browser
layout. Browser visual inspection is a separate check, using a fake-model seeded
proposal and temporary databases; no Nebius calls are needed for that inspection.

Historical milestone evidence: 13 AppTest checks and 94 existing regression
tests passed. The current consolidated result is in
[docs/EVALUATION.md](docs/EVALUATION.md); do not add repeated runs together.
Safari desktop inspection confirmed the stored review, disabled pending
chat input, approval confirmation and history after browser refresh. The visual
fixture contained exactly one order after approval and refresh, with morning
capacity 2 → 1 and other slots unchanged. The original databases retained their
hashes and modification times. That automated/visual milestone used no live
Nebius requests. Subsequently the user demonstrated a live quote, clicked
Approve and refreshed the saved confirmation with one morning place remaining.
This session evidence is an example, not a benchmark. No paid/live call was made
for submission packaging. Mobile layout remains untested.

After an ended technical read failure, the UI shows safe failure guidance and
allows a new customer-submitted question; it does not automatically retry.
The CLI instead exits on technical failure and must be restarted with the same
session/database paths. Interrupted booking work requires explicit recovery in
the UI. Session IDs do not enforce identity or ownership; this is a trusted local
demo. Approval still cannot promise calendar-date delivery for undated slots.

For synthetic service prices, initial slot capacities, and the fact that one
booking consumes one capacity unit regardless of garment quantity, see
[business rules](README.md#synthetic-data-and-business-rules). For a planned
recording under five minutes with fresh isolated databases, see
[the demo script](docs/DEMO_SCRIPT.md).

Official API references consulted:

- [Streamlit reruns and session state](https://docs.streamlit.io/develop/concepts/architecture/session-state)
- [Button behavior](https://docs.streamlit.io/develop/concepts/design/buttons)
- [Chat input](https://docs.streamlit.io/develop/api-reference/chat/st.chat_input)
- [AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
