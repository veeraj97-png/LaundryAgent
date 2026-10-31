# Planned recording: 4 minutes 30 seconds

This is a recording plan, not evidence of a timed rehearsal or completed video.
Use synthetic requests only. The live model can take longer or choose different
wording; rehearse once and leave the final 30 seconds of the five-minute limit
as contingency. Do not expose .env, account pages, API keys or unrelated windows.

## Prepare isolated capacity before each rehearsal/take

From the project root after README setup, with the virtual environment active:

```bash
DEMO_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/laundry-recording.XXXXXX")"
export LAUNDRY_BUSINESS_DB="$DEMO_RUN_DIR/business.db"
export LAUNDRY_CHECKPOINT_DB="$DEMO_RUN_DIR/checkpoints.db"
printf 'Demo files: %s\n' "$DEMO_RUN_DIR"
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8502 --browser.gatherUsageStats false
```

Open `http://127.0.0.1:8502`. If another process uses that port, stop that process
or choose a different local port. This server is separate from any existing demo
on 8501. `open_demo()` initializes fresh business/checkpoint files on startup;
morning starts at 2, afternoon at 0, evening at 3. Save the printed paths and
session ID for the resume segment. A new conversation alone does not reset
capacity. For a new take, stop this server and rerun the preparation block to
create a new directory; never delete or reset an existing database.

Locally configure the model already verified for your account. The model/key
are not included in the repository. This planned live demonstration can incur
provider usage; submission packaging did not make those live calls.

## Shot-by-shot script

| Time | Show/do | Suggested narration |
|---|---|---|
| 0:00–0:25 | Main screen and synthetic-data label. | “LaundryAgent is a local learning prototype for a laundry customer asking about prices and pickup, then approving a proposal. It uses fictional prices and undated capacity.” |
| 0:25–0:55 | Architecture diagram in `LANGGRAPH_CHAT.md`, then return to UI. | “Codex helped build and test small Python steps. Streamlit calls one existing LangGraph agent; validated business functions read SQLite. The model cannot approve a booking. Conversation checkpoints are stored separately.” |
| 0:55–1:30 | Submit **What would it cost to wash and iron five garments?** Expand Tool activity. | “The model asks for calculate_quote. Python calculates five times Rs 50: Rs 250, with an estimated three-day turnaround.” |
| 1:30–1:55 | Submit **Is morning pickup available?** Show tool result and sidebar. | “This is a fresh undated-capacity lookup. This new demo database starts with two morning places; it does not promise a particular date.” |
| 1:55–2:50 | Submit **Prepare a proposal to wash and iron five garments, morning pickup.** Review the panel before clicking Approve. | “The stored review includes service code/name, quantity, unit and total price, turnaround estimate, slot code/window, request ID and snapshot ID. A draft reserves nothing. I explicitly approve these exact terms with the trusted button.” |
| 2:50–3:15 | Show saved confirmation and morning capacity 1. | “The booking has a real saved ID. One booking consumes one capacity unit, not five units. The order, capacity change, booked proposal state and request mapping commit together.” |
| 3:15–3:55 | Copy session ID, refresh, then use New conversation and resume the copied ID. | “History returns from persistent checkpoints without resubmitting old messages. The same saved booking remains; capacity stays one. New conversation changes history, not business data. Session IDs are not authentication.” |
| 3:55–4:30 | Briefly show `docs/EVALUATION.md` result and failure scenarios. | “Offline fake-model tests exercise rejection, rollback, stale terms, duplicate retries, concurrency and post-commit recovery. Technical reads stop safely rather than reporting no slots; customers explicitly retry. There is no automatic support handoff, authentication, payment or dated scheduling. This live example is not a model-accuracy benchmark.” |

Do not submit a second booking to demonstrate refresh. If the model requests
clarification, supply the already intended service, five garments and morning
slot; do not disguise it as a successful one-shot response. If a technical failure
occurs, show the safe stop honestly. In Streamlit, an ended read failure permits
a new explicit question. For interrupted booking work, check Recover saved
outcome before creating another proposal. Never fabricate a confirmation.

## Recording and submission checklist

- Rehearse the plan, then record a take under five minutes; neither has been
  claimed completed by this document.
- Keep recordings/screenshots in gitignored `recordings/` or `screenshots/`.
  Check that all visible data is synthetic before sharing through your course.
- Verify the saved ID and unchanged capacity after refresh in the actual take.
- Add the final video link and any required author reflection manually.
