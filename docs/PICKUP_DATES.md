# Pickup dates and daily capacity

New proposals and bookings require a pickup date as well as a window. Dates are
stored as `YYYY-MM-DD` and interpreted in `Asia/Kolkata` (India time). The UI
calendar blocks past dates, and Python validates real calendar dates and rejects
past dates at tool, proposal, approval and booking boundaries. A price quote
alone does not need a date. No delivery date is calculated or guaranteed.

## Customer flow

Choose Pickup date above the request builder, then choose service, quantity and
an available window. Changing the calendar refreshes only that date's capacity;
it does not call the model or reserve a place. Review proposal submits all four
values through the existing graph. The review panel and saved receipt include
the date. The selected date stays remembered while review hides the calendar.

In chat, say for example: “Prepare a proposal to wash and iron five garments,
morning pickup tomorrow.” The model receives the current India business date to
resolve an explicit relative date. If no date is supplied or established, it
must ask; the dated tools do not supply a default. The date appears in safe tool
activity arguments. Clarifying a date does not record approval.

The direct `shop.py` CLI asks for a date before its pickup-window loop. The
conversational CLI uses the same dated tools as Streamlit. Local order lookup
also displays the recorded pickup date.

## Storage and approvals

- `orders.pickup_date` is a nullable additive column. New orders require a date;
  old orders remain NULL and display “Not recorded (legacy booking)”.
- `dated_pickup_slots` has primary key `(pickup_date, slot_code)` and nonnegative
  remaining capacity. Booking on one date does not consume capacity on another.
- Each untouched date starts with morning 2, afternoon 2 and evening 3, using
  fictional demo capacities. Existing date/window capacity rows are preserved,
  including full slots. Reads calculate unmaterialized defaults
  without writing rows. Booking inserts/updates the selected row transactionally.
- The old `pickup_slots` rows remain intact as legacy data and window names.
  Their historical undated consumption is not assigned to any invented date.
- The date is included in canonical proposal terms and therefore in the snapshot
  identifier and idempotency comparison. Changing it needs a new proposal and
  fresh approval. A completed request with a different date is a conflict.
- Capacity reduction, order, booked state and mapping still use one serialized
  SQLite transaction. Failure also rolls back any newly materialized date row.
- Completed retries are checked before current-date/capacity restrictions, so a
  saved booking can still be retrieved after its pickup date has passed.
- An unbooked proposal whose date has passed cannot book. Old pending proposals
  with no date must be rejected/replaced; the UI disables Approve for them. Old
  completed mappings remain retrievable and do not create new orders.

Initialization is repeatable. It adds the nullable order column and the new table
without deleting orders, changing old canonical snapshots, resetting capacity or
inventing approvals. Conversation checkpoints remain in their separate file.

## Verification and boundaries

Reusable date-specific tests are in `test_pickup_dates.py`, with calendar/form
tests in `test_streamlit_app.py`. Existing regression fixtures now supply explicit
future dates, and their subprocess copies include `scheduling.py` and test helpers.
All fixtures use temporary databases. No live Nebius call is needed for tests.

```bash
python -B -m unittest -v test_pickup_dates test_streamlit_app
python -B -m unittest discover -s . -p 'test_*.py' -v
```

Same-day cutoff times, holidays, business opening days, delivery scheduling,
customer timezones, cancellation and rescheduling of a confirmed order are not
implemented. This step establishes pickup-date identity and capacity, not a full
logistics calendar. Session IDs still are not authentication. Existing synthetic
prices, approval controls, checkpoint behaviour and execution limits remain.
