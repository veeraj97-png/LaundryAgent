# Evaluation: current reusable tests and observed demonstrations

## Pickup date extension

The dated-pickup implementation passed the full reusable offline suite in an
isolated project copy: **125 cases passed, 0 failures, 0 errors, 0 skips**.
The new total includes 10 date-specific cases and 21 UI cases (three additional
calendar tests), with the earlier regression scenarios adapted to explicit dates.
It replaces the earlier 112-case snapshot as the current total; runs are not added.

Date tests verify strict/non-past dates, read-only date-specific availability,
independent capacity on different days, date changes requiring fresh approval,
rollback of newly materialized capacity rows, saved retries after a date passes,
expiry of unbooked approvals, clarification when date is missing, restart and
duplicate resume, additive legacy migration, and refusal of new undated bookings.
Calendar tests verify date-only reruns make no model calls, the chosen date reaches
review/order/receipt, and clearing the calendar prevents an undated proposal.

An offline Safari fixture approved five wash-and-iron garments for 2026-11-02,
morning. The saved order included that date and the dated row's capacity became
1 from the initial 2. No other date was consumed. Tests and that visual fixture
used temporary databases, with no live Nebius calls. Before activating the local
app's additive migration, hash/mtime checks confirmed existing databases were
untouched. See [PICKUP_DATES.md](PICKUP_DATES.md) for current policies and limits.

## Live pickup-date verification — 2026-10-31

The configured Nebius model `Qwen/Qwen3-30B-A3B-Instruct-2507` passed a real API verification using
**six model calls**, a ten-call hard cap, 20-second request timeouts, and no
SDK retries. A sandbox-blocked connection attempt preceded the successful run;
it produced a connection failure and was not counted as a successful API call.
No fake model responses were used in this live check.

| Check | Observed evidence | Result |
| --- | --- | --- |
| Quote | Actual `calculate_quote` call: five wash-and-iron garments, Rs 250; orders 0 → 0 | PASS |
| Missing date | Model asked for a YYYY-MM-DD date; no proposal/order/capacity change | PASS |
| Dated proposal | Actual `prepare_booking_proposal` call for morning, 2026-11-02; price Rs 250; awaiting approval, orders 0, capacity 2 | PASS |
| Checkpoint reopen and trusted approval | Same stored proposal recovered; harness invoked trusted approval; orders 0 → 1, dated capacity 2 → 1; other date capacity stayed 2 | PASS |
| Duplicate resume | Same saved booking returned; business rows unchanged | PASS |
| Fresh availability | Actual `check_pickup_slot` call returned 1 remaining on 2026-11-02; business rows unchanged | PASS |

The temporary fixture booking ID was
`e9cd6694-21d3-447a-a5fe-55b04c627d75`. Approval was performed by the trusted
verification harness, not the model. Checkpoint reopen used a new connection in
the same harness process; separate-process restart remains covered offline.
All business/checkpoint files for this live run were temporary. SHA-256 comparisons
confirmed the original `laundry.db` and existing runtime database files were unchanged.
The live run exercised the backend graph, not a live browser click workflow.

The calendar was subsequently placed inside the “Start with your laundry” card,
above Service, while remaining outside the submission form so date changes refresh
availability immediately. All 21 interface cases passed after that layout change.

## Customer interface refinement

After the packaging baseline below, the customer interface was redesigned with
a guided service/quantity/pickup builder, starter prompts, readable proposal
review and verified saved receipts. The existing graph, business functions and
approval transaction were retained. Five focused AppTest cases were added, and
the empty-conversation assertion now accounts for its welcome message.

The isolated offline suite for this refinement ran **112 cases: 112 passed,
0 failures, 0 errors, 0 skips**. This consists of the same 94 non-UI cases and
18 UI cases, not 112 plus the historical 107. Fake model responses and temporary
business/checkpoint databases were used; no live Nebius calls were made. The
18 UI cases also passed after final presentation adjustments.

New checks cover customer-selected form values, approval after form preparation,
receipt display on later turns, quotes when pickup is full, click-only shortcuts,
and refusal to show an unsaved model claim as a booking receipt. Source/runtime
database hash comparisons confirmed existing databases remained unchanged.

## Packaging baseline before the interface refinement

Verified on **2026-10-31 (Asia/Kolkata)** with CPython **3.12.10**, macOS arm64.
The pinned `requirements.txt` installed successfully into a newly created,
disposable virtual environment. `python -m pip check` reported no broken
requirements. All installed distributions' declared Python-version requirements
accepted that interpreter. Other Python versions/platforms were not tested.

The then-current reusable suite was run **once** for that packaging step, using a
temporary copy of the Python files and temporary business/checkpoint databases.
The copy contained no `.env` or original database. The parent test runner blocked
outbound socket connections; subprocess cases use their existing fake-model
programs. No live Nebius calls were made.

| Result | Packaging baseline count |
|---|---:|
| Test cases run | 107 |
| Passed | 107 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |

These are unittest cases; individual cases may contain several subtests. Earlier
107-test milestone runs and the later 31-check readiness audit overlap this suite
and must not be added as unique coverage. The readiness audit's two supplemental
continuation checks were temporary, not added to the reusable suite.

## Commands and isolation

Normal reusable-suite command from the project root, with the environment active:

```bash
python -B -m unittest discover -s . -p 'test_*.py' -v
python -m pip check
```

For packaging, the same discovery was executed from a disposable source copy in
the fresh environment with a guard around the existing tests. The command was
`../fresh-venv/bin/python -B -` with a Python discovery runner on standard input.
The following reproduces its guard and discovery (summary output is simplified;
paths are relative to that temporary `project/` directory):

```bash
../fresh-venv/bin/python -B - <<'PY'
import os, socket, unittest, json, pathlib
from unittest.mock import patch
for name in ('NEBIUS_API_KEY', 'NEBIUS_MODEL', 'NEBIUS_BASE_URL',
             'LANGCHAIN_API_KEY', 'LANGSMITH_API_KEY'):
    os.environ.pop(name, None)
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
suite = unittest.defaultTestLoader.discover('.', pattern='test_*.py')
with open('../suite.log', 'w') as log:
    with patch('socket.create_connection', side_effect=AssertionError('OFFLINE network forbidden')), \
         patch('socket.socket.connect', side_effect=AssertionError('OFFLINE network forbidden')):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
summary = {'tests': result.testsRun, 'failures': len(result.failures),
           'errors': len(result.errors), 'skips': len(result.skipped),
           'passed': result.wasSuccessful()}
print(json.dumps(summary))
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
```

The log/summary files are temporary verification artifacts, not application data
or submitted customer records. Hash and modification-time comparisons verified
that the original application/test Python files and existing project databases
remained unchanged. Documentation edits are separate from those protected files.

## Reusable suite inventory at packaging baseline

| File | Cases | Main coverage |
|---|---:|---|
| [test_nebius_check.py](../test_nebius_check.py) | 25 | Configuration, secret-safe errors, dispatch validation, bounded fake tool protocol, silent imports. |
| [test_laundry_agent.py](../test_laundry_agent.py) | 22 | Model/tool routing, multiple calls, clarification, follow-ups, isolation, distinct failure results, limits and safe activity logs. |
| [test_laundry_persistence.py](../test_laundry_persistence.py) | 9 | Real subprocess restart, fresh availability, no duplicate history, per-turn counter reset, unfinished-turn handling and connection closure. |
| [test_approval_integrity.py](../test_approval_integrity.py) | 20 | Immutable terms, stored decisions, direct-storage enforcement, rollback, idempotency, concurrency, legacy data and read-only lookup. |
| [test_agent_approval.py](../test_agent_approval.py) | 18 | Interrupts, rejection, forged/stale decisions, proposal replay, pending restart, duplicate resumes and post-commit recovery. |
| [test_streamlit_app.py](../test_streamlit_app.py) | 13 | Quote/review UI, rejection, booking, session/URL restoration, rerun safety, stale actions, configuration failures and explicit recovery. |

## Scenario evidence

All PASS entries below refer to deterministic offline behaviour, unless explicitly
identified as session observations in the next section.

| Scenario | Expected behaviour | Actual reusable evidence/result |
|---|---|---|
| Quote five wash-and-iron garments | Existing arithmetic returns Rs 250; tool call ID is preserved. | `test_model_tool_model_loop_preserves_call_id`; fake protocol executes real quote function. PASS. |
| Missing quantity | Structured validation result goes to model; no guessed quantity or booking. | `test_validation_result_returns_to_model_for_clarification`. PASS. |
| Several read calls | Each ID receives a result before the next model call. | `test_multiple_tool_calls_are_all_answered_before_model_runs`. PASS. |
| Persistent follow-up | Same thread retains messages across process exit; new availability is read afresh. | `test_checkpoint_survives_process_exit_follow_up_is_fresh_and_not_duplicated`. PASS. |
| Prepare and reject | Stored proposal becomes rejected; orders and capacity do not change. | `test_reject_and_duplicate_reject_create_no_order` and UI rejection test. PASS. |
| Restart while pending | Same request, snapshot and terms remain; approval books once. | `test_quit_pending_then_restart_and_approve_uses_same_proposal`. PASS. |
| Forged/local approval | Model text, raw resume or changed in-memory status cannot authorize new orders. | Forged-resume, local-status and direct-storage tests. PASS. |
| Alter commercial terms | Service code/name, quantity, unit/total price, turnaround and slot code/window changes are refused. | `test_all_commercial_fields_are_enforced_before_and_after_booking`. PASS, including pre/post-booking subtests. |
| Retry a completed request | Same result/ID; no second order/capacity consumption, including at zero capacity. | `test_last_slot_retry_and_restart_return_original_booking`, duplicate-resume tests. PASS. |
| Concurrent writes | Identical requests book once; two distinct requests cannot both claim the last slot. | Both concurrency tests in approval-integrity suite. PASS. |
| Insert fails after update | Error propagates; capacity, order, mapping and proposal state roll back. | Duplicate-order-ID and forced-mapping-trigger tests. PASS. |
| Slot fills before approval | Do not overbook or print confirmation; stored approval can remain without a booking. | `test_slot_fills_before_approval_no_false_confirmation_and_approval_remains`. PASS. |
| Technical read error | Stop, skip remaining batch, preserve call sequencing; do not say no slots. | `test_technical_tool_failure_stops_and_skips_rest_of_batch`; distinct full/empty/technical test. PASS. |
| Model/step failure | Bound calls and stop with safe messages instead of claiming success. | Model/tool-limit, malformed/network-response and adapter-budget tests. PASS. |
| Error/crash after commit | Retrieve saved booking and show its actual ID without another booking. | Post-commit process-restart, uncertain-error and UI saved-confirmation tests. PASS. |
| Browser redraw/refresh | Restore history without replaying submission or approval. | UI quote-redraw, stale-action and session/URL restoration tests. PASS. |

The booking transaction test observes the intermediate booked state before a
forced mapping insert failure, then verifies all rows match the pre-attempt
snapshot after rollback. Rollback is not itself proof of retry safety: successful
retry protection is separately tested through request mappings.

## Focused readiness-audit evidence, separate from this suite run

The preceding audit ran 29 selected existing cases plus two temporary checks,
all passing. The new checks exercised a temporary read failure after an earlier
confirmed booking and before another proposal. There was one failed read attempt
and zero automatic retries. An explicit follow-up after reopening the graph
performed one fresh read. Orders remained 1 → 1 → 1; capacities remained morning
1, evening 3, afternoon 0. Earlier confirmed messages and all business rows stayed
unchanged. A separate AppTest confirmed chat remained enabled and retained the
earlier confirmation. Those checks are session evidence, not additional reusable
test files or unique tests to add to the 107 total.

## Observed live demonstrations: examples only

Available session evidence records:

- A terminal screenshot reporting `LIVE TOOL ROUND TRIP PASS`, with validated
  calculate_quote arguments for five wash-and-iron garments, actual Python total
  250, and a final customer response of Rs 250.
- A conversational availability example used actual evening and morning tool
  calls, including the follow-up “What about morning?”. These are snapshots from
  that demo, not guaranteed current availability.
- A user-approved terminal proposal returned saved booking ID
  `2e4bfd53-a7a8-42e7-b4b3-2f290926dc21`; a subsequent read-only inspection in that
  session found one confirmed order and morning capacity 1 in temporary files.
- User-supplied Streamlit screenshots showed a Rs 250 quote and a booking
  confirmation with ID `527ba28a-4971-4f11-8d67-004178942777`. The user explicitly
  confirmed clicking Approve and refreshing; the later screenshot retained the
  same booking and showed morning capacity 1.

These observations establish example success, not an accuracy rate, cost/latency
benchmark, independent database audit of every screenshot, or a recorded timed
demo. The original screenshot files are not packaged here; the author should
select sanitized evidence for submission. No live request was repeated during
documentation/repository preparation. The locally inspected configuration used
`Qwen/Qwen3-30B-A3B-Instruct-2507`; the repository deliberately sets no model default.

Separate earlier Safari inspection used a fake-model seeded proposal and verified
rendering, approval and refresh in temporary databases. It must not be counted as
a live model test.

## Limits of the evaluation

Repository packaging checks also passed: a local `main` repository was initialized
at the confirmed project root with no commit, staged files or remote. The proposed
publishable set contained 25 files; none exceeded 1 MiB (largest about 31 KB).
A scan for the local API credential and recognizable credential patterns found
no matches in that proposed set; no secret values were printed. `.env.example`
retained empty secret/model placeholders. Git exclusion checks covered `.env`,
environment directories, runtime databases and SQLite sidecars, secrets files,
recordings, screenshots, caches and generated output. All local Markdown file
links resolved. These checks do not replace author review of future screenshots,
recordings or files added after packaging.

- Fake model responses test execution and safeguards, not model intent accuracy,
  hallucination rate, adversarial robustness, or reliability across a prompt set.
- No fresh live calls, performance, cost, throughput or customer time savings were
  measured during packaging. There is no production-readiness claim.
- No authentication/ownership, payments, real customer data, calendar scheduling,
  public hosting or automated human-support escalation is implemented or tested.
- Arbitrary interrupted network steps are not generally recoverable; checkpoints
  do not prove exactly-once provider execution. Technical reads do not auto-retry.
- Desktop Safari was observed previously. Mobile browsers, other operating
  systems/Python versions and sustained shared multi-user use are untested.
- Direct dependencies are pinned; transitive dependencies are resolved by pip,
  not frozen in a complete lockfile. The fresh install verifies this resolution
  on the stated date/environment, not every future resolution.
- The timed demo is a plan in [DEMO_SCRIPT.md](DEMO_SCRIPT.md), not a completed
  recording or rehearsal.
