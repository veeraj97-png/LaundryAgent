"""Dated fixtures for existing regression scenarios; never touch user databases."""
from datetime import timedelta
import sqlite3
from scheduling import business_today

TEST_DATE = (business_today() + timedelta(days=2)).isoformat()


def seed_date(path, pickup_date=TEST_DATE):
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO dated_pickup_slots VALUES (?, ?, ?)",
            [(pickup_date, code, capacity) for code, capacity in
             (("morning", 2), ("afternoon", 0), ("evening", 3))],
        )
