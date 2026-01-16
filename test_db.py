# test_db.py
import sqlite3
from datetime import date

DBPATH = "hotelfoTEST.db"  # or "hotelfo.db" – match your app

def show_counts():
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    for table in ["reservations", "stays", "rooms", "no_shows", "spare_rooms"]:
        c.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
        row = c.fetchone()
        print(f"{table}: {row['cnt']}")

    conn.close()

def sample_arrival_dates(limit=5):
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT arrival_date, COUNT(*) AS cnt
        FROM reservations
        GROUP BY arrival_date
        ORDER BY arrival_date DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()

    print("\nSample arrival_date values:")
    for r in rows:
        print(r["arrival_date"], "->", r["cnt"], "reservations")

    conn.close()

def test_arrivals_for_date(target_str):
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print(f"\nTesting arrivals for {target_str}:")
    c.execute("""
        SELECT id, reservation_no, guest_name, room_number
        FROM reservations
        WHERE arrival_date = ?
        ORDER BY guest_name
        """,
        (target_str,),
    )
    rows = c.fetchall()
    print("Found", len(rows), "rows")
    for r in rows[:10]:  # print first 10
        print(r["id"], r["reservation_no"], r["guest_name"], r["room_number"])

    conn.close()

if __name__ == "__main__":
    show_counts()
    sample_arrival_dates()

    # take one of the printed dates and plug it here to test:
    # e.g. target_date = "2025-12-20"
    target_date = input("\nEnter an arrival_date to test (YYYY-MM-DD): ").strip()
    if target_date:
        test_arrivals_for_date(target_date)
