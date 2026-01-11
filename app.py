import os
from glob import glob
from contextlib import closing
from datetime import date, datetime
from io import BytesIO

import pandas as pd
import sqlite3
import streamlit as st


TEST_MODE = True  # set False for live system

if TEST_MODE:
    DB_PATH = "hotel_fo_TEST.db"
    ARRIVALS_ROOT = "data/arrivals-test"   
else:
    DB_PATH = "hotel_fo.db"
    ARRIVALS_ROOT = "data/arrivals"        

# Fixed room inventory blocks: inclusive ranges (whole numbers)
ROOM_BLOCKS = [
    (100, 115),
    (300, 313),
    (400, 413),
    (500, 513),
    (600, 613),
    (700, 710),
    (800, 810),
    (900, 910),
    (1000, 1010),
    (1100, 1110),
    (1200, 1210),
    (1300, 1310),
    (1400, 1410),
    (1500, 1510),
    (1600, 1610),
    (1700, 1705),
]


# =========================
# Database layer class
# =========================

class FrontOfficeDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
        if self.reservations_empty():
            self.import_all_arrivals_from_fs()
        # ensure room inventory exists and status aligns with stays
        self.seed_rooms_from_blocks()
        self.sync_room_status_from_stays()

    # ---- internal helpers ----

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()

            # reservations – arrivals data
            c.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount_pending REAL,
                arrival_date TEXT,
                depart_date TEXT,
                room_number TEXT,
                room_type_code TEXT,
                adults INTEGER,
                children INTEGER,
                total_guests INTEGER,
                reservation_no TEXT,
                voucher TEXT,
                related_reservation TEXT,
                crs_code TEXT,
                crs_name TEXT,
                guest_id_raw TEXT,
                guest_name TEXT,
                vip_flag TEXT,
                client_id TEXT,
                main_client TEXT,
                nights INTEGER,
                meal_plan TEXT,
                rate_code TEXT,
                channel TEXT,
                cancellation_policy TEXT,
                main_remark TEXT,
                contact_name TEXT,
                contact_phone TEXT,
                contact_email TEXT,
                total_remarks TEXT,
                source_of_business TEXT,
                stay_option_desc TEXT,
                remarks_by_chain TEXT,
                reservation_group_id TEXT,
                reservation_group_name TEXT,
                company_name TEXT,
                company_id_raw TEXT,
                country TEXT,
                reservation_status TEXT DEFAULT 'CONFIRMED',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """)

            # stays – in-house & departures + parking
            c.execute("""
            CREATE TABLE IF NOT EXISTS stays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER,
                room_number TEXT,
                status TEXT DEFAULT 'EXPECTED',
                checkin_planned TEXT,
                checkout_planned TEXT,
                checkin_actual TEXT,
                checkout_actual TEXT,
                breakfast_code TEXT,
                comment TEXT,
                parking_space TEXT,
                parking_plate TEXT,
                parking_notes TEXT,
                FOREIGN KEY (reservation_id) REFERENCES reservations(id)
            )
            """)

            # rooms – inventory & twin info
            c.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_number TEXT PRIMARY KEY,
                room_type TEXT,
                floor INTEGER,
                status TEXT DEFAULT 'VACANT',
                is_twin INTEGER DEFAULT 0
            )
            """)

            # tasks / handover
            c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_date TEXT,
                title TEXT,
                created_by TEXT,
                assigned_to TEXT,
                comment TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """)

            # no-shows
            c.execute("""
            CREATE TABLE IF NOT EXISTS no_shows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arrival_date TEXT,
                guest_name TEXT,
                main_client TEXT,
                charged INTEGER,
                comment TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """)

            # spare rooms per date (includes spare twin list)
            c.execute("""
            CREATE TABLE IF NOT EXISTS spare_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_date TEXT,
                room_number TEXT
            )
            """)

    def reservations_empty(self) -> bool:
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) AS cnt FROM reservations")
            row = c.fetchone()
            return row["cnt"] == 0

    def _build_reservations_from_df(self, df: pd.DataFrame) -> pd.DataFrame:
        df.columns = [str(c).strip() for c in df.columns]

        col_map = {
            "Amount Pending": "amount_pending",
            "Arrival Date": "arrival_date",
            "Room": "room_number",
            "Room type": "room_type_code",
            "AD": "adults",
            "Tot. guests": "total_guests",
            "Reservation No.": "reservation_no",
            "Voucher": "voucher",
            "Related reservat": "related_reservation",
            "CRS": "crs_code",
            "CRS Name": "crs_name",
            "Guest ID": "guest_id_raw",
            "Guest or Group's name": "guest_name",
            "VIP": "vip_flag",
            "client Id.": "client_id",
            "Main client": "main_client",
            "Nights": "nights",
            "Depart": "depart_date",
            "Meal Plan": "meal_plan",
            "Rate": "rate_code",
            "Chanl": "channel",
            "Cancellation Penalty": "cancellation_policy",
            "Main Rem.": "main_remark",
            "Contact person": "contact_name",
            "Contact Telephone No": "contact_phone",
            "E-mail": "contact_email",
            "Total Remarks": "total_remarks",
            "Source of Business": "source_of_business",
            "Stay Options Detail and Remarks": "stay_option_desc",
            "Remarks by Hotel Chain": "remarks_by_chain"
        }

        df_db = pd.DataFrame()
        for src, dest in col_map.items():
            df_db[dest] = df[src] if src in df.columns else None

        df_db["children"] = 0
        df_db["company_name"] = None
        df_db["company_id_raw"] = None
        df_db["country"] = None

        for col in ["arrival_date", "depart_date"]:
            df_db[col] = pd.to_datetime(df_db[col], errors="coerce").dt.date.astype(str)

        return df_db

    def import_arrivals_file(self, path: str) -> int:
        try:
            df = pd.read_excel(path)
        except Exception:
            return 0
        df_db = self._build_reservations_from_df(df)
        with closing(self._get_conn()) as conn, conn:
            df_db.to_sql("reservations", conn, if_exists="append", index=False)
        return len(df_db)

    def import_all_arrivals_from_fs(self) -> int:
        pattern = os.path.join(ARRIVALS_ROOT, "**", "Arrivals *.XLSX")
        files = sorted(glob(pattern, recursive=True))
        total = 0
        for path in files:
            total += self.import_arrivals_file(path)
        return total

    def get_arrivals_for_date(self, d: date):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT * FROM reservations
            WHERE arrival_date = ?
            ORDER BY COALESCE(room_number, '') ASC, guest_name ASC
            """, (d.isoformat(),))
            return c.fetchall()

    def update_reservation_room(self, res_id: int, room_number: str):
        rn = room_number.strip() if room_number else None
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("""
            UPDATE reservations
            SET room_number = ?, updated_at = datetime('now')
            WHERE id = ?
            """, (rn, res_id))

    # ---- rooms / stays ----

    def ensure_room_exists(self, room_number: str):
        if not room_number:
            return
        rn = room_number.strip()
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO rooms (room_number, status) VALUES (?, 'VACANT')", (rn,))

    def check_room_conflict(self, room_number: str, d: date):
        rn = room_number.strip()
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT s.id, s.room_number, r.guest_name, s.checkin_planned, s.checkout_planned
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE s.room_number = ?
              AND s.status = 'CHECKED_IN'
              AND DATE(s.checkin_planned) <= DATE(?)
              AND DATE(s.checkout_planned) > DATE(?)
            """, (rn, d.isoformat(), d.isoformat()))
            return c.fetchall()

    def checkin_reservation(self, res_id: int):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("SELECT * FROM reservations WHERE id = ?", (res_id,))
            res = c.fetchone()
            if not res:
                return False, "Reservation not found"

            room = res["room_number"]
            if not room:
                return False, "Assign a room first"

            rn = room.strip()
            self.ensure_room_exists(rn)

            arr = datetime.strptime(res["arrival_date"], "%Y-%m-%d").date()
            dep = datetime.strptime(res["depart_date"], "%Y-%m-%d").date()

            conflicts = self.check_room_conflict(rn, arr)
            if conflicts:
                conflict = conflicts[0]
                return False, f"Room {rn} occupied by {conflict['guest_name']} until {conflict['checkout_planned']}"

            c.execute("""
            INSERT INTO stays (reservation_id, room_number, status,
                               checkin_planned, checkout_planned,
                               checkin_actual)
            VALUES (?, ?, 'CHECKED_IN', ?, ?, datetime('now'))
            """, (res_id, rn, arr.isoformat(), dep.isoformat()))

            c.execute("UPDATE rooms SET status = 'OCCUPIED' WHERE room_number = ?", (rn,))
            return True, "Checked in"

    def get_inhouse(self):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT s.*, r.guest_name, r.arrival_date, r.depart_date
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE s.status = 'CHECKED_IN'
            ORDER BY CAST(s.room_number AS INTEGER)
            """)
            return c.fetchall()

    def get_departures_for_date(self, d: date):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT s.*, r.guest_name
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE s.status = 'CHECKED_IN'
              AND DATE(s.checkout_planned) = DATE(?)
            ORDER BY CAST(s.room_number AS INTEGER)
            """, (d.isoformat(),))
            return c.fetchall()

    def checkout_stay(self, stay_id: int):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("SELECT * FROM stays WHERE id = ?", (stay_id,))
            s = c.fetchone()
            if not s:
                return False, "Stay not found"
            room = s["room_number"]
            c.execute("""
            UPDATE stays
            SET status = 'CHECKED_OUT', checkout_actual = datetime('now')
            WHERE id = ?
            """, (stay_id,))
            c.execute("UPDATE rooms SET status = 'VACANT' WHERE room_number = ?", (room,))
            return True, "Checked out"

    def seed_rooms_from_blocks(self):
        """Create full room inventory from fixed numeric ranges."""
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            for start, end in ROOM_BLOCKS:
                for rn in range(start, end + 1):
                    rn_str = str(rn)
                    c.execute(
                        "INSERT OR IGNORE INTO rooms (room_number, status) VALUES (?, 'VACANT')",
                        (rn_str,),
                    )

    def sync_room_status_from_stays(self):
        """Mark rooms as OCCUPIED if there is any CHECKED_IN stay, else VACANT."""
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("UPDATE rooms SET status = 'VACANT'")
            c.execute("""
            SELECT DISTINCT room_number
            FROM stays
            WHERE status = 'CHECKED_IN'
            """)
            for row in c.fetchall():
                rn = row["room_number"]
                c.execute("UPDATE rooms SET status = 'OCCUPIED' WHERE room_number = ?", (rn,))

    # ---- parking helpers ----

    def update_parking_for_stay(self, stay_id: int, space: str | None,
                                plate: str | None, notes: str | None):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("""
            UPDATE stays
            SET parking_space = ?, parking_plate = ?, parking_notes = ?
            WHERE id = ?
            """, (space or None, plate or None, notes or None, stay_id))

    def get_parking_overview_for_date(self, d: date):
        """All stays with any parking info on selected date."""
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT s.*, r.guest_name
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE DATE(s.checkin_planned) <= DATE(?)
              AND DATE(s.checkout_planned) > DATE(?)
              AND (s.parking_space IS NOT NULL OR s.parking_plate IS NOT NULL)
            ORDER BY s.parking_space, CAST(s.room_number AS INTEGER)
            """, (d.isoformat(), d.isoformat()))
            return c.fetchall()


    def add_task(self, task_date: date, title: str, created_by: str, assigned_to: str, comment: str):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO tasks (task_date, title, created_by, assigned_to, comment)
            VALUES (?, ?, ?, ?, ?)
            """, (task_date.isoformat(), title, created_by, assigned_to, comment))

    def get_tasks_for_date(self, d: date):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT * FROM tasks
            WHERE DATE(task_date) = DATE(?)
            ORDER BY created_at
            """, (d.isoformat(),))
            return c.fetchall()

    # ---- no-shows ----

    def add_no_show(self, arrival_date: date, guest_name: str, main_client: str, charged: bool, comment: str):
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO no_shows (arrival_date, guest_name, main_client, charged, comment)
            VALUES (?, ?, ?, ?, ?)
            """, (arrival_date.isoformat(), guest_name, main_client, int(charged), comment))

    def get_no_shows_for_date(self, d: date):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT * FROM no_shows
            WHERE DATE(arrival_date) = DATE(?)
            ORDER BY created_at
            """, (d.isoformat(),))
            return c.fetchall()



    def get_twin_rooms(self):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("SELECT room_number FROM rooms WHERE is_twin = 1 ORDER BY CAST(room_number AS INTEGER)")
            return [r["room_number"] for r in c.fetchall()]

    def get_all_rooms(self):
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("SELECT room_number FROM rooms ORDER BY CAST(room_number AS INTEGER)")
            return [r["room_number"] for r in c.fetchall()]



    def set_spare_rooms_for_date(self, target_date: date, rooms: list[str]):
        t = target_date.isoformat()
        with closing(self._get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute("DELETE FROM spare_rooms WHERE DATE(target_date) = DATE(?)", (t,))
            for rn in rooms:
                c.execute(
                    "INSERT INTO spare_rooms (target_date, room_number) VALUES (?, ?)",
                    (t, rn),
                )

    def get_spare_rooms_for_date(self, target_date: date):
        t = target_date.isoformat()
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT room_number FROM spare_rooms
                WHERE DATE(target_date) = DATE(?)
                ORDER BY CAST(room_number AS INTEGER)
            """, (t,))
            return [r["room_number"] for r in c.fetchall()]



    def search_reservations(self, q: str):
        like = f"%{q}%"
        with closing(self._get_conn()) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT * FROM reservations
            WHERE guest_name LIKE ?
               OR room_number LIKE ?
               OR reservation_no LIKE ?
               OR main_client LIKE ?
               OR channel LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """, (like, like, like, like, like))
            return c.fetchall()

    def read_table(self, name: str) -> pd.DataFrame:
        with closing(self._get_conn()) as conn:
            return pd.read_sql_query(f"SELECT * FROM {name}", conn)


    # def export_arrivals_excel(self, d: date):
    #     rows = self.get_arrivals_for_date(d)
    #     if not rows:
    #         return None
    #     df = pd.DataFrame([dict(r) for r in rows])
    #     preferred_order = [
    #         "amount_pending", "arrival_date", "room_number", "room_type_code",
    #         "adults", "total_guests", "reservation_no", "voucher",
    #         "related_reservation", "crs_code", "crs_name", "guest_id_raw",
    #         "guest_name", "vip_flag", "client_id", "main_client", "nights",
    #         "depart_date", "meal_plan", "rate_code", "channel",
    #         "cancellation_policy", "main_remark", "contact_name",
    #         "contact_phone", "contact_email", "total_remarks",
    #         "source_of_business", "stay_option_desc", "remarks_by_chain"
    #     ]
    #     cols = [c for c in preferred_order if c in df.columns] + [
    #         c for c in df.columns if c not in preferred_order
    #     ]
    #     df = df[cols]
    #     output = BytesIO()
    #     with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
    #         df.to_excel(writer, index=False, sheet_name="Arrivals")
    #     output.seek(0)
    #     return output

    # def export_inhouse_excel(self, d: date):
    #     inhouse_rows = self.get_inhouse()
    #     dep_rows = self.get_departures_for_date(d)
    #     df_inhouse = pd.DataFrame([dict(r) for r in inhouse_rows]) if inhouse_rows else pd.DataFrame()
    #     df_dep = pd.DataFrame([dict(r) for r in dep_rows]) if dep_rows else pd.DataFrame()
    #     output = BytesIO()
    #     with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
    #         df_inhouse.to_excel(writer, index=False, sheet_name="InHouse")
    #         df_dep.to_excel(writer, index=False, sheet_name="Departures")
    #     output.seek(0)
    #     return output


# =========================
# Streamlit UI
# =========================

db = FrontOfficeDB(DB_PATH)


def page_arrivals():
    st.header("Arrivals")
    d = st.date_input("Arrival date", value=date.today(), key="arrivals_date")

    if db.reservations_empty():
        st.error("No arrivals data loaded. Check ARRIVALS_ROOT path and restart the app.")
        return
    else:
        st.caption("Arrivals loaded from filesystem. Use this page for room allocation and check-in.")

    rows = db.get_arrivals_for_date(d)
    if not rows:
        st.info("No arrivals for this date.")
        return

    st.subheader("Arrivals list")
    for r in rows:
        with st.expander(f"{r['guest_name']} – Res {r['reservation_no']}"):
            col1, col2, col3 = st.columns(3)
            col1.write(f"Room type: {r['room_type_code']}")
            col2.write(f"Channel: {r['channel']}")
            col3.write(f"Rate: {r['rate_code']}")

            st.write(r["main_remark"] or "")
            if r["total_remarks"]:
                st.write(r["total_remarks"])

            current_room = r["room_number"] or ""
            room = st.text_input("Room", value=current_room, key=f"room_{r['id']}")
            if st.button("Save room", key=f"save_room_{r['id']}"):
                db.update_reservation_room(r["id"], room)
                st.success(f"Room {room} saved.")

            if st.button("Check-in", key=f"checkin_{r['id']}"):
                success, msg = db.checkin_reservation(r["id"])
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

    # st.subheader("Export")
    # excel_bytes = db.export_arrivals_excel(d)
    # if excel_bytes:
    #     st.download_button(
    #         "Download Arrivals Excel",
    #         data=excel_bytes,
    #         file_name=f"Arrivals-{d.isoformat()}.xlsx",
    #         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    #     )


def page_inhouse():
    st.header("In House & Departures")
    today = st.date_input("Date", value=date.today(), key="inhouse_date")

    st.subheader("In House List")
    inhouse_rows = db.get_inhouse()
    df_inhouse = pd.DataFrame([dict(r) for r in inhouse_rows]) if inhouse_rows else pd.DataFrame()
    if df_inhouse.empty:
        st.info("No in-house guests.")
    else:
        display_cols = ["guest_name", "room_number", "status",
                        "checkin_planned", "checkout_planned", "breakfast_code", "comment",
                        "parking_space", "parking_plate"]
        for col in display_cols:
            if col not in df_inhouse.columns:
                df_inhouse[col] = ""
        st.dataframe(df_inhouse[display_cols])

        st.subheader("Quick checkout")
        for _, row in df_inhouse.iterrows():
            col1, col2 = st.columns([3, 1])
            col1.write(f"Room {row['room_number']} – {row['guest_name']} (Dep {row['checkout_planned']})")
            if col2.button("Check-out", key=f"co_{row['id']}"):
                success, msg = db.checkout_stay(int(row["id"]))
                if success:
                    st.success(msg)
                else:
                    st.error(msg)

    st.subheader("Today’s Check-out list")
    dep_rows = db.get_departures_for_date(today)
    df_dep = pd.DataFrame([dict(r) for r in dep_rows]) if dep_rows else pd.DataFrame()
    if df_dep.empty:
        st.info("No departures for this date.")
    else:
        st.dataframe(df_dep[["guest_name", "room_number", "checkout_planned", "status"]])

    # st.subheader("Export In-House/Departures")
    # inhouse_bytes = db.export_inhouse_excel(today)
    # if inhouse_bytes:
    #     st.download_button(
    #         "Download In-House & Departures Excel",
    #         data=inhouse_bytes,
    #         file_name=f"InHouse-{today.isoformat()}.xlsx",
    #         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    #     )


def page_tasks_handover():
    st.header("Tasks & Handover")
    d = st.date_input("Date", value=date.today(), key="tasks_date")

    st.subheader("Add task / handover item")
    col1, col2 = st.columns(2)
    title = col1.text_input("Task")
    created_by = col2.text_input("By")
    assigned_to = col1.text_input("To")
    comment = col2.text_input("Comment")
    if st.button("Add task"):
        if title:
            db.add_task(d, title, created_by, assigned_to, comment)
            st.success("Task added.")
        else:
            st.error("Task title required.")

    st.subheader("Tasks for this date")
    rows = db.get_tasks_for_date(d)
    df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if df.empty:
        st.info("No tasks.")
    else:
        st.dataframe(df[["task_date", "title", "created_by", "assigned_to", "comment"]])


def page_no_shows():
    st.header("No Shows")
    d = st.date_input("Arrival date", value=date.today(), key="noshow_date")

    st.subheader("Add no-show")
    guest_name = st.text_input("Guest Name")
    main_client = st.text_input("Main Client")
    charged = st.checkbox("Charged")
    comment = st.text_input("Comment")
    if st.button("Add no-show"):
        if guest_name:
            db.add_no_show(d, guest_name, main_client, charged, comment)
            st.success("No-show added.")
        else:
            st.error("Guest name required.")

    st.subheader("No-shows for this date")
    rows = db.get_no_shows_for_date(d)
    df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if df.empty:
        st.info("No no-shows recorded.")
    else:
        st.dataframe(df[["arrival_date", "guest_name", "main_client", "charged", "comment"]])


def page_search():
    st.header("Search")
    q = st.text_input("Search (guest, room, reservation no., company, channel)")
    if not q:
        st.info("Enter a search term.")
        return
    rows = db.search_reservations(q)
    df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if df.empty:
        st.warning("No matches.")
    else:
        st.dataframe(df[["arrival_date", "depart_date", "guest_name", "room_number",
                         "reservation_no", "channel", "rate_code", "main_remark"]])


def page_room_list():
    st.header("Room list")
    st.caption("Manage room inventory and twin flags (used for Spare Twin List).")

    df = db.read_table("rooms")
    if df.empty:
        st.info("No rooms yet (fixed ranges should have seeded them).")
        return

    st.subheader("Rooms")
    df_display = df[["room_number", "room_type", "floor", "status", "is_twin"]].copy()
    df_display = df_display.sort_values(by="room_number", key=lambda s: s.astype(float).astype(int))


    edited = st.data_editor(
        df_display,
        num_rows="dynamic",
        key="rooms_editor"
    )

    if st.button("Save room changes"):
        with closing(db._get_conn()) as conn, conn:
            c = conn.cursor()
            for _, row in edited.iterrows():
                c.execute("""
                    INSERT INTO rooms (room_number, room_type, floor, status, is_twin)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(room_number) DO UPDATE SET
                        room_type=excluded.room_type,
                        floor=excluded.floor,
                        status=excluded.status,
                        is_twin=excluded.is_twin
                """, (
                    str(row["room_number"]),
                    row.get("room_type"),
                    row.get("floor"),
                    row.get("status", "VACANT"),
                    int(row.get("is_twin") or 0),
                ))
        st.success("Room list updated.")


def page_spare_rooms():
    st.header("Spare rooms")
    st.caption("Mark rooms as spare for a specific date (e.g. Spare Twin List).")

    target = st.date_input("Date", value=date.today(), key="spare_date")

    all_rooms = db.get_all_rooms()
    if not all_rooms:
        st.info("No rooms in inventory yet. Fill the Room list first.")
        return

    current_spare = db.get_spare_rooms_for_date(target)
    twin_rooms = set(db.get_twin_rooms())

    st.subheader("Select spare rooms")
    selected = st.multiselect(
        "Spare rooms for this date",
        options=all_rooms,
        default=current_spare,
        help="Include twin and non-twin rooms; twin rooms are listed below.",
    )

    if twin_rooms:
        st.caption(
            "Twin rooms (for reference): " +
            ", ".join(sorted(twin_rooms, key=int))
        )

    if st.button("Save spare rooms"):
        db.set_spare_rooms_for_date(target, selected)
        st.success(f"Saved {len(selected)} spare rooms for {target}.")

    saved = db.get_spare_rooms_for_date(target)
    st.subheader("Saved spare rooms")
    if not saved:
        st.info("No spare rooms saved for this date.")
    else:
        st.write(", ".join(saved))


def page_parking():
    st.header("Parking")
    st.caption("Optional parking list to track spaces and registration numbers.")

    target = st.date_input("Date", value=date.today(), key="parking_date")

    inhouse = db.get_inhouse()
    if not inhouse:
        st.info("No in-house guests. Check in guests first.")
        return

    st.subheader("Assign parking to in-house rooms")
    for row in inhouse:
        # Convert sqlite3.Row to dict for easier access
        row_dict = dict(row)
        
        col1, col2, col3, col4 = st.columns([2, 1.2, 1.2, 2])
        col1.write(f"Room {row_dict['room_number']} – {row_dict['guest_name']}")
        
        space = col2.text_input("Space", value=row_dict.get("parking_space") or "",
                                key=f"space_{row_dict['id']}")
        plate = col3.text_input("Plate", value=row_dict.get("parking_plate") or "",
                                key=f"plate_{row_dict['id']}")
        notes = col4.text_input("Notes", value=row_dict.get("parking_notes") or "",
                                key=f"pnotes_{row_dict['id']}")
        
        if st.button("Save", key=f"save_parking_{row_dict['id']}"):
            db.update_parking_for_stay(row_dict["id"], space, plate, notes)
            st.success("Parking updated.")

    st.subheader("Parking overview for this date")
    rows = db.get_parking_overview_for_date(target)
    df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if df.empty:
        st.info("No parking entries for this date.")
    else:
        st.dataframe(df[["parking_space", "parking_plate", "room_number",
                         "guest_name", "parking_notes"]].sort_values(
                             by=["parking_space", "room_number"],
                             key=lambda s: s.astype(str)
                         ))



def page_db_viewer():
    st.header("Database viewer")
    table = st.selectbox(
        "Select table",
        ["reservations", "stays", "rooms", "tasks", "no_shows", "spare_rooms"],
    )
    df = db.read_table(table)
    if df.empty:
        st.info(f"No rows in {table}.")
    else:
        st.dataframe(df)


def main():
    st.set_page_config(
        page_title="Front Office Hub",
        page_icon="🏨",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    with st.sidebar:
        st.title("Front Office Hub")
        mode = "TEST MODE" if TEST_MODE else "LIVE MODE"
        st.markdown(f"**{mode}**")
        page = st.radio(
            "Navigate",
            [
                "Arrivals",
                "In-House & Departures",
                "Search",
                "Tasks & Handover",
                "No Shows",
                "Room list",
                "Spare rooms",
                "Parking",
                "DB Viewer",
            ],
        )
        st.markdown("---")
        st.caption("Arrivals load once from filesystem; all updates happen in this app.")

    if page == "Arrivals":
        page_arrivals()
    elif page == "In-House & Departures":
        page_inhouse()
    elif page == "Search":
        page_search()
    elif page == "Tasks & Handover":
        page_tasks_handover()
    elif page == "No Shows":
        page_no_shows()
    elif page == "Room list":
        page_room_list()
    elif page == "Spare rooms":
        page_spare_rooms()
    elif page == "Parking":
        page_parking()
    elif page == "DB Viewer":
        page_db_viewer()


if __name__ == "__main__":
    main()
