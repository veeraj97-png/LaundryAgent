# LaundryAgent

LaundryAgent is a guided Python learning project for a local laundry customer
assistant. A customer can ask about services, prices and pickup capacity, then
choose a pickup date and review a stored proposal before explicitly approving a synthetic booking.
The prototype demonstrates a conversational tool loop with persistent state and
business protections. It makes no measured time-saving or production-readiness
claim. Use synthetic data only.

## What is implemented

- Nebius chooses among catalogue, quote, pickup-check, available-slot and
  proposal-preparation tools. Python validates every requested tool and argument.
- LangGraph handles conditional routing, message history and approval interrupts.
- Separate SQLite files persist business records and conversation checkpoints.
- Trusted Approve/Reject controls operate outside the model. Stored approval is
  tied to exact immutable terms and checked at the booking write boundary.
- Stable request IDs, transactions and serialized SQLite writes prevent duplicate
  bookings and overbooking in the tested cases. Confirmation uses the saved ID.
- Streamlit provides local chat, exact proposal review, safe tool activity,
  session selection and rerun protection. The terminal applications remain usable.
- Execution limits and safe errors stop failed turns. Customers can explicitly
  retry; there is no automatic technical-read retry or human-support handoff.

## Fresh checkout setup

Use **CPython 3.12**. Packaging verification used **3.12.10** on macOS arm64.
The installed direct packages declare Python >=3.10 except Streamlit, which
declares >=3.11. A clean 3.12 installation is verified; other Python versions
and operating systems are not tested here. You also need a browser, network
access for package installation, and Nebius account access for live chat.
Offline tests do not need Nebius credentials.

From the directory containing your checkout:

```bash
cd LaundryAgent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
```

Open `.env` in a local editor. Set `NEBIUS_API_KEY` privately, retain the example
Nebius HTTPS base URL, and select your exact accessible tool-capable
`NEBIUS_MODEL`. Never paste your key into chat, documentation or a recording.
Exported environment variables override the project-local `.env`.

There is **no default model**. The locally configured model inspected for this
submission was `Qwen/Qwen3-30B-A3B-Instruct-2507`. This is a configuration example,
not a guarantee of access, pricing or future availability. `.env.example` keeps
the model empty. Model discovery alone does not establish tool-call support.
To deliberately discover/test your configuration, use the bounded live commands
in [NEBIUS_SETUP.md](NEBIUS_SETUP.md). They can incur provider usage; packaging
verification itself made no live model calls.

Launch the UI:

```bash
python -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Open the printed localhost URL. Try:

> What would it cost to wash and iron five garments?

Then:

> Prepare a proposal to wash and iron five garments, morning pickup tomorrow.

Review all terms before clicking Approve or Reject. Chat is disabled while the
decision is pending. A proposal is not a reservation. If capacity changes before
approval, booking can fail safely. A successful booking displays its saved ID.

## Synthetic data and business rules

There is no real customer dataset or imported order history. The catalogue is
defined in `shop.services`; repeatable `storage.initialize_database()` creates
tables and inserts missing initial slots without resetting existing capacity.
Orders start empty in a fresh database and are generated during demonstrations.

| Service code | Display name | Price per garment | Estimated turnaround |
|---|---|---:|---:|
| `wash_and_fold` | Wash and Fold | Rs 30 | 2 days |
| `wash_and_iron` | Wash and Iron | Rs 50 | 3 days |

Quotes multiply the unit price by a positive whole garment quantity. The agent
restricts quantity to 1–1,000. Five wash-and-iron garments cost Rs 250.

| Pickup code | Display window (India time) | Initial capacity per date |
|---|---|---:|
| `morning` | 9 AM–12 PM | 2 |
| `afternoon` | 12 PM–4 PM | 0 |
| `evening` | 4 PM–8 PM | 3 |

**One new booking consumes one pickup-capacity unit**, regardless of garment
quantity. Rejection and proposal preparation consume none. Identical successful
request retries return the original booking and consume none again. Changing
commercial details requires a new proposal and fresh approval.

New pickups require a calendar date on or after today in India time. Capacity
is tracked independently for each date/window. A confirmed pickup date does not
create a promised delivery date; turnaround remains an estimate. No address collection, tax, payment or delivery
scheduling workflow is implemented.

## Business records versus conversation checkpoints

The UI defaults to project-relative, gitignored files:

- `runtime/ui_business.db`: orders, pickup slots, immutable proposals, decisions
  and completed-request mappings, including date-specific pickup capacity.
- `runtime/ui_checkpoints.db`: LangGraph messages, state and pending graph steps.

The UI refuses the original project `laundry.db`. To isolate another demo, set
`LAUNDRY_BUSINESS_DB` and `LAUNDRY_CHECKPOINT_DB` to separate paths before launch;
see [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for fresh per-recording paths.

Save the displayed session ID to resume. The URL also retains it for refresh.
New conversation starts independent history, never resets business data, and
does not restore consumed capacity. Session IDs are not authentication; customer
identity and ownership checks are not implemented. Do not use real personal data.

The CLI defaults differ: `chat.py` uses `laundry.db` and `agent_state.db` unless
you supply path flags. For a synthetic CLI session from the project root:

```bash
mkdir -p runtime/cli-demo
python chat.py --session cli-demo --business-db runtime/cli-demo/business.db --checkpoint-db runtime/cli-demo/checkpoints.db
```

## Failure handling and limits

Validation errors go back to the model for clarification. Full/empty availability
is a valid result; technical failures are labeled differently. A technical read
failure stops the turn and skips remaining calls in that batch. It is not
automatically retried. In Streamlit, an ended read-failure turn allows a new
explicit customer question; in the CLI it exits, and the customer restarts with
the same session/database paths. Earlier saved messages and bookings persist.

Interrupted supported draft/booking steps can recover using stable IDs and
stored outcomes. Streamlit requires an explicit Recover saved outcome click;
the CLI attempts supported recovery at startup. General interrupted network
steps are not automatically recoverable. Never assume an uncertain booking
failed and create another request before checking its saved outcome.

Each graph turn permits at most four model calls and eight tool attempts, with
four tool calls per model response and a recursion guard. Model requests use
20-second timeouts and no automatic SDK retries. History and output are bounded.
The CLI has a 100-model-call process budget; Streamlit's lazy client is per
action, so that is not a persistent browser-session quota.

## Tests and evaluation

From the project root with the environment active:

```bash
python -B -m unittest discover -s . -p 'test_*.py' -v
python -m pip check
```

The reusable tests use fake model responses and temporary business/checkpoint
databases, including subprocess restart and concurrency checks. Do not substitute
real database paths in tests. The full-suite command does not need a `.env`.

For focused UI tests: `python -B -m unittest -v test_streamlit_app`.
For the current measured result, environment and evidence limits, see
[docs/EVALUATION.md](docs/EVALUATION.md). Historical runs are not added together
as unique test cases. Live examples are demonstrations, not accuracy benchmarks.

## Architecture and guides

`app.py` / `chat.py` → `laundry_agent.py` → validated `shop.py` functions →
`storage.py`. The graph has its own SQLite checkpointer. The model cannot call
approval, booking, superseding, arbitrary code, SQL or general order lookup tools.
`check_order.py` remains a separate trusted local lookup CLI.

- [Graph, persistence, approval and CLI guide](LANGGRAPH_CHAT.md)
- [Streamlit reruns, sessions and controls](STREAMLIT_DEMO.md)
- [One-off Nebius connectivity/tool protocol](NEBIUS_SETUP.md)
- [Development prompts, iterations and learning evidence](docs/DEVELOPMENT_JOURNEY.md)
- [Pickup dates, migration and daily capacity](docs/PICKUP_DATES.md)
- [Evaluation](docs/EVALUATION.md)
- [Planned demo under five minutes](docs/DEMO_SCRIPT.md)
- [Case-study source for a Google Doc](docs/CASE_STUDY.md)

## Limitations and submission status

This is a local synthetic-data learning prototype, not a production system.
There is no authentication, customer ownership enforcement, multi-user service
design, payments, voice, external messaging, general agent order lookup, data
retention policy or encrypted checkpoint store. Avoid simultaneous processes
using the same thread. No model reliability, latency, cost or time-saving
benchmark has been measured. A live model can misunderstand requests; Python
validation and trusted approval still constrain execution.

Git ignores credentials, environments, runtime databases/sidecars, generated
artifacts and recordings. `.env.example` contains placeholders only. Local Git
preparation does not publish anything. Recording, personal review, Google Doc
creation and any later commit/remote/push are separate manual submission steps.

Pickup-date migration preserves older undated orders without inventing dates.
Old pending undated proposals need replacement and fresh approval. See the
[pickup-date guide](docs/PICKUP_DATES.md) for same-day and calendar limitations.
