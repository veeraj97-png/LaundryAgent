import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path


DATABASE_PATH = Path(__file__).parent / "laundry.db"


def initialize_database():
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        # 1. Create the orders table.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                booking_id TEXT PRIMARY KEY,
                service_code TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                total_price INTEGER NOT NULL CHECK (total_price >= 0),
                slot_code TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)

        # 2. Create the pickup slots table before inserting rows.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS pickup_slots (
                slot_code TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                remaining_capacity INTEGER NOT NULL
                    CHECK (remaining_capacity >= 0)
            )
        """)

        # Keep completed requests separate so legacy orders need no changes.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS booking_requests (
                request_id TEXT PRIMARY KEY NOT NULL,
                booking_id TEXT NOT NULL UNIQUE REFERENCES orders(booking_id),
                business_details TEXT NOT NULL,
                booking_snapshot TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                request_id TEXT PRIMARY KEY NOT NULL,
                snapshot_id TEXT NOT NULL,
                business_details TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'awaiting_approval', 'approved', 'rejected', 'superseded', 'booked'
                )),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                superseded_at TEXT,
                booked_at TEXT
            )
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS immutable_proposal_terms
            BEFORE UPDATE OF request_id, snapshot_id, business_details, created_at
            ON proposals
            BEGIN
                SELECT RAISE(ABORT, 'Stored proposal terms are immutable.');
            END
        """)

        # 3. Add initial slots only if they do not already exist.
        initial_slots = [
            ("morning", "9 AM–12 PM", 2),
            ("afternoon", "12 PM–4 PM", 0),
            ("evening", "4 PM–8 PM", 3),
        ]

        connection.executemany("""
            INSERT INTO pickup_slots (
                slot_code,
                display_name,
                remaining_capacity
            )
            VALUES (?, ?, ?)
            ON CONFLICT(slot_code) DO NOTHING
        """, initial_slots)

        connection.commit()

    finally:
        connection.close()

def read_pickup_slots():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute("""
            SELECT slot_code, display_name, remaining_capacity
            FROM pickup_slots
            ORDER BY slot_code
        """).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()

def _canonical_booking_details(booking):
    """Compare approved terms, excluding capacity and workflow state."""
    details = {
        "quote": {
            key: booking["quote"][key]
            for key in (
                "service_code", "service_name", "quantity", "unit_price",
                "total_price", "turnaround_days",
            )
        },
        "pickup": {
            key: booking["pickup"][key]
            for key in ("slot_code", "display_name")
        },
    }
    return json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _proposal_from_row(row):
    proposal = json.loads(row["business_details"])
    proposal.update({
        "request_id": row["request_id"],
        "snapshot_id": row["snapshot_id"],
        "status": row["state"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "superseded_at": row["superseded_at"],
        "booked_at": row["booked_at"],
    })
    return proposal


def save_proposal(proposal):
    """Store fixed terms once; supplying an existing ID never resets its state."""
    request_id = proposal["request_id"]
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("A non-empty request ID is required.")
    details = _canonical_booking_details(proposal)
    snapshot_id = sha256(f"{request_id}\n{details}".encode("utf-8")).hexdigest()
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
        ).fetchone()
        if existing is not None:
            if existing["business_details"] != details:
                raise ValueError("Proposal terms conflict: use a new proposal ID for different terms.")
            connection.commit()
            return _proposal_from_row(existing)

        completed = connection.execute(
            "SELECT request_id FROM booking_requests WHERE request_id = ?", (request_id,),
        ).fetchone()
        if completed is not None:
            raise ValueError("A completed legacy request cannot become a new proposal.")

        connection.execute("""
            INSERT INTO proposals (
                request_id, snapshot_id, business_details, state, created_at
            ) VALUES (?, ?, ?, 'awaiting_approval', ?)
        """, (request_id, snapshot_id, details, _utc_now()))
        row = connection.execute(
            "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
        ).fetchone()
        connection.commit()
        return _proposal_from_row(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_proposal(request_id):
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
        ).fetchone()
        return _proposal_from_row(row) if row is not None else None
    finally:
        connection.close()


def _transition_proposal(request_id, snapshot_id, new_state):
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown proposal ID.")
        if row["snapshot_id"] != snapshot_id:
            raise ValueError("Stale or mismatched proposal snapshot.")
        if row["state"] == "booked":
            raise ValueError("A booked proposal cannot be changed.")
        if row["state"] == "superseded" and new_state != "superseded":
            raise ValueError("This proposal has been superseded.")
        if row["state"] == "rejected" and new_state == "approved":
            raise ValueError("A rejected proposal needs a new proposal and fresh approval.")

        if row["state"] != new_state:
            # Column names come only from these two fixed choices, never user input.
            timestamp_column = "superseded_at" if new_state == "superseded" else "decided_at"
            connection.execute(
                f"UPDATE proposals SET state = ?, {timestamp_column} = ? WHERE request_id = ?",
                (new_state, _utc_now(), request_id),
            )
            row = connection.execute(
                "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
            ).fetchone()
        connection.commit()
        return _proposal_from_row(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_proposal_decision(request_id, snapshot_id, decision):
    """Trusted application action, not an agent/LLM tool or authentication check."""
    if decision not in ("approved", "rejected"):
        raise ValueError("The decision must be approved or rejected.")
    return _transition_proposal(request_id, snapshot_id, decision)


def supersede_proposal(request_id, snapshot_id):
    """Disable an unbooked proposal before preparing replacement terms."""
    return _transition_proposal(request_id, snapshot_id, "superseded")


def save_booking(booking, *, allow_new=True):
    """Save a new request once, or return its original completed booking."""
    request_id = booking["request_id"]
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("A non-empty request ID is required.")

    business_details = _canonical_booking_details(booking)
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # Reserve the writer before checking the request or capacity.
        connection.execute("BEGIN IMMEDIATE")
        completed = connection.execute("""
            SELECT booking_id, business_details, booking_snapshot
            FROM booking_requests
            WHERE request_id = ?
        """, (request_id,)).fetchone()

        if completed is not None:
            if completed["business_details"] != business_details:
                raise ValueError(
                    "Request ID conflict: this request was already booked "
                    "with different details."
                )
            order = connection.execute(
                "SELECT booking_id FROM orders WHERE booking_id = ?",
                (completed["booking_id"],),
            ).fetchone()
            if order is None:
                raise sqlite3.IntegrityError(
                    "The completed request refers to a missing booking."
                )
            # Return the original response, rather than rebuilding it from live capacity.
            saved_booking = json.loads(completed["booking_snapshot"])
            connection.commit()
            return saved_booking

        if not allow_new:
            raise ValueError("The proposal must be approved before booking.")

        proposal = connection.execute(
            "SELECT * FROM proposals WHERE request_id = ?", (request_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError("Unknown proposal ID.")
        if (proposal["business_details"] != business_details
                or proposal["snapshot_id"] != booking.get("snapshot_id")):
            raise ValueError("Booking terms do not match the stored proposal snapshot.")
        if proposal["state"] != "approved":
            raise ValueError(f"The stored proposal is {proposal['state']}, not approved.")

        slot = connection.execute("""
            SELECT remaining_capacity FROM pickup_slots WHERE slot_code = ?
        """, (booking["pickup"]["slot_code"],)).fetchone()
        if slot is None or slot["remaining_capacity"] <= 0:
            raise ValueError("The pickup slot is unavailable.")

        saved_booking = deepcopy(booking)
        saved_booking["pickup"]["remaining_capacity"] = slot["remaining_capacity"]
        saved_booking["pickup"]["available"] = True

        cursor = connection.execute("""
            UPDATE pickup_slots
            SET remaining_capacity = remaining_capacity - 1
            WHERE slot_code = ? AND remaining_capacity > 0
        """, (booking["pickup"]["slot_code"],))

        if cursor.rowcount != 1:
            raise ValueError("The pickup slot is unavailable.")

        connection.execute("""
            INSERT INTO orders (
                booking_id, service_code, quantity,
                total_price, slot_code, status
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            booking["booking_id"],
            booking["quote"]["service_code"],
            booking["quote"]["quantity"],
            booking["quote"]["total_price"],
            booking["pickup"]["slot_code"],
            "confirmed",
        ))

        saved_booking["status"] = "confirmed"
        connection.execute("""
            UPDATE proposals SET state = 'booked', booked_at = ?
            WHERE request_id = ?
        """, (_utc_now(), request_id))

        connection.execute("""
            INSERT INTO booking_requests (
                request_id, booking_id, business_details, booking_snapshot
            ) VALUES (?, ?, ?, ?)
        """, (
            request_id,
            saved_booking["booking_id"],
            business_details,
            json.dumps(saved_booking, sort_keys=True, allow_nan=False),
        ))

        connection.commit()
        return saved_booking

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

def get_booking(booking_id):
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
            SELECT booking_id, service_code, quantity,
                   total_price, slot_code, status
            FROM orders
            WHERE booking_id = ?
        """, (booking_id,)).fetchone()

        return dict(row) if row is not None else None

    finally:
        connection.close()


if __name__ == "__main__":
    initialize_database()
    print(f"Database ready: {DATABASE_PATH}")

    for slot in read_pickup_slots():
        print(
            f"{slot['slot_code']}: "
            f"{slot['display_name']} — "
            f"{slot['remaining_capacity']} pickups remaining"
        )
