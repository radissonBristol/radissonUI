import os
from glob import glob
from datetime import date, datetime, timedelta
from io import BytesIO
import pandas as pd
import streamlit as st
import sqlite3
from contextlib import closing
import time


# Initialize database AFTER set_page_config in main()
db = None

def format_date(date_str):
    """Format date string, removing time portion"""
    if not date_str:
        return ''
    try:
        # Parse the datetime string
        dt = datetime.fromisoformat(str(date_str))
        # Return formatted date (e.g., "15 January 2026")
        return dt.strftime("%d %B %Y")
    except (ValueError, TypeError):
        # Fallback: just remove time if simple string
        return str(date_str).split()[0] if ' ' in str(date_str) else str(date_str)
    
def format_room_number(room_number):
    """Format room number, removing .0 decimals"""
    if not room_number:
        return ''
    try:
        num = float(room_number)
        if num.is_integer():
            return str(int(num))
        return str(num)
    except (ValueError, TypeError):
        return str(room_number)


def clean_numeric_columns(df: pd.DataFrame, cols: list):
    """Convert numeric columns to whole numbers for display"""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: int(float(x)) if pd.notna(x) and str(x) not in ['', 'None', 'nan'] else x
            )
    return df


# PostgreSQL configuration
TEST_MODE = False  # set False for live system

if TEST_MODE:
    DBPATH = "hotelfoTEST.db"
    ARRIVALS_ROOT = "data/arrivals-test"
else:
    DBPATH = "hotelfo.db"
    ARRIVALS_ROOT = "data/arrivals"

       

# Fixed room inventory blocks: inclusive ranges (whole numbers)
ROOM_BLOCKS = [
    (100, 115),
    (300, 313),
    (400, 413),
    (500, 513),
    (600, 613),
    (700, 709),
    (800, 809),
    (900, 909),
    (1000, 1009),
    (1100, 1109),
    (1200, 1209),
    (1300, 1309),
    (1400, 1409),
    (1500, 1509),
    (1600, 1609),
    (1700, 1705),
]


def clean_numeric_columns(df: pd.DataFrame, cols: list):
    """Convert numeric columns to whole numbers for display"""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: int(float(x)) if pd.notna(x) and str(x).strip() not in ['', 'None'] else x)
    return df

class FrontOfficeDB:
    def init_db(self):
            with closing(self.get_conn()) as conn, conn:
                c = conn.cursor()
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

                c.execute("""
                    CREATE TABLE IF NOT EXISTS rooms (
                        room_number TEXT PRIMARY KEY,
                        room_type TEXT,
                        floor INTEGER,
                        status TEXT DEFAULT 'VACANT',
                        is_twin INTEGER DEFAULT 0
                    )
                """)

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

                c.execute("""
                    CREATE TABLE IF NOT EXISTS no_shows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        arrival_date TEXT,
                        guest_name TEXT,
                        main_client TEXT,
                        charged INTEGER,
                        amount_charged REAL,
                        amount_pending REAL,
                        comment TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    )
                """)

                c.execute("""
                    CREATE TABLE IF NOT EXISTS spare_rooms (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        target_date TEXT,
                        room_number TEXT
                    )
                """)
                c.execute("""
    CREATE TABLE IF NOT EXISTS hsk_task_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_date TEXT,
        room_number TEXT,
        task_type TEXT,
        status TEXT DEFAULT 'PENDING',
        notes TEXT,
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(task_date, room_number, task_type)
    )
""")

    def __init__(self, dbpath: str):
        self.dbpath = dbpath
        self.init_db()
        if self.reservations_empty():
            self.import_all_arrivals_from_fs()
            self.seed_rooms_from_blocks()
            self.sync_room_status_from_stays()
    def get_hsk_task_status(self, task_date: date, room_number: str, task_type: str):
        return self.fetch_one(
            "SELECT status, notes FROM hsk_task_status WHERE task_date = ? AND room_number = ? AND task_type = ?",
            (task_date.isoformat(), room_number, task_type)
        )

    def update_hsk_task_status(self, task_date: date, room_number: str, task_type: str, status: str, notes: str = ""):
        self.execute(
            """
            INSERT INTO hsk_task_status (task_date, room_number, task_type, status, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(task_date, room_number, task_type)
            DO UPDATE SET status = ?, notes = ?, updated_at = datetime('now')
            """,
            (task_date.isoformat(), room_number, task_type, status, notes, status, notes)
        )



    def get_potential_no_shows(self, d: date):
        """Get arrivals who didn't check in - potential no-shows"""
        return self.fetch_all(
            """
            SELECT r.id, r.guest_name, r.reservation_no, r.main_client, r.room_number
            FROM reservations r
            WHERE date(r.arrival_date) = date(?)
            AND NOT EXISTS (
                SELECT 1 FROM stays s
                WHERE s.reservation_id = r.id
                    AND s.status = 'CHECKED_IN'
            )
            ORDER BY r.guest_name
            """,
            (d.isoformat(),),
        )


    def get_conn(self):
            conn = sqlite3.connect(self.dbpath, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def execute(self, query, params=None):
        with closing(self.get_conn()) as conn, conn:
            c = conn.cursor()
            if params is None:
                c.execute(query)
            else:
                c.execute(query, params)
            return c

    def fetch_all(self, query, params=None):
        with closing(self.get_conn()) as conn:
            c = conn.cursor()
            if params is None:
                c.execute(query)
            else:
                c.execute(query, params)
            rows = c.fetchall()
            return [dict(row) for row in rows]

    def fetch_one(self, query, params=None):
        with closing(self.get_conn()) as conn:
            c = conn.cursor()
            if params is None:
                c.execute(query)
            else:
                c.execute(query, params)
            row = c.fetchone()
            return dict(row) if row else None


    def get_breakfast_list_for_date(self, target_date: date):
        return self.fetch_all(
            """
            SELECT r.room_number, r.guest_name, r.adults, r.children, r.total_guests, r.meal_plan
            FROM reservations r
            LEFT JOIN stays s ON s.reservation_id = r.id
            WHERE date(r.arrival_date) <= date(?)
            AND date(r.depart_date) > date(?)
            AND r.room_number IS NOT NULL AND r.room_number != ''
            AND (s.status IS NULL OR s.status != 'CHECKEDOUT')
            AND (r.meal_plan LIKE '%BB%' OR r.meal_plan LIKE '%Breakfast%')
            ORDER BY CAST(r.room_number AS INTEGER)
            """,
            (target_date.isoformat(), target_date.isoformat()),
        )

    def generate_hsk_tasks_for_date(self, target_date: date):
        """Auto-generate housekeeping tasks for the day"""
        tasks = []
        
        conn = self.get_conn()
        c = conn.cursor()
        
        try:
            # Use LIKE to match the date portion of timestamp
            target_str = target_date.isoformat()  # e.g., "2026-08-16"
            
            # 1. Checkouts - ALL guests departing today (checked in OR checked out)
            c.execute("""
                SELECT s.room_number, r.guest_name, r.main_remark, r.total_remarks, s.status
                FROM stays s
                JOIN reservations r ON r.id = s.reservation_id
                WHERE s.checkout_planned LIKE ?
                AND s.room_number IS NOT NULL AND s.room_number != ''
                ORDER BY 
                    CASE WHEN s.status = 'CHECKED_OUT' THEN 0 ELSE 1 END,
                    CAST(s.room_number AS INTEGER)
            """, (f"{target_date.isoformat()}%",))

            checkouts = c.fetchall()

            for co in checkouts:
                co_dict = dict(co)
                room = co_dict["room_number"]
                guest = co_dict["guest_name"]
                
                # Mark already checked-out rooms as URGENT
                if co_dict.get("status") == "CHECKED_OUT":
                    priority = "URGENT"
                else:
                    priority = "HIGH"
                
                task = {
                    "room": room,
                    "tasktype": "CHECKOUT",
                    "priority": priority,
                    "description": f"Clean room {room} - {guest} checkout",
                    "notes": []
                }
                
                remarks = f"{co_dict.get('main_remark') or ''} {co_dict.get('total_remarks') or ''}".lower()
                if '2t' in remarks:
                    task["notes"].append("2 TWIN BEDS")
                if 'vip' in remarks or 'birthday' in remarks:
                    task["priority"] = "URGENT"
                    task["notes"].append("VIP/SPECIAL")
                
                # Add note if already checked out
                if co_dict.get("status") == "CHECKED_OUT":
                    task["notes"].append("✓ CHECKED OUT - CLEAN NOW")
                
                tasks.append(task)

            
            # 2. Stayovers
            c.execute("""
                SELECT DISTINCT s.room_number, r.guest_name
                FROM stays s
                JOIN reservations r ON r.id = s.reservation_id
                WHERE s.status = 'CHECKED_IN'
                AND s.checkin_planned LIKE ?
                AND s.checkout_planned LIKE ?
                ORDER BY CAST(s.room_number AS INTEGER)
            """, (f"%{target_str.split('-')[0]}-{target_str.split('-')[1]}%", 
                f"%{target_str.split('-')[0]}-{target_str.split('-')[1]}%"))
            
            stayovers = c.fetchall()
            
            for so in stayovers:
                so_dict = dict(so)
                tasks.append({
                    "room": so_dict["room_number"],
                    "tasktype": "STAYOVER",
                    "priority": "MEDIUM",
                    "description": f"Refresh room {so_dict['room_number']} - {so_dict['guest_name']} stayover",
                    "notes": []
                })
            
            # 3. Arrivals
            c.execute("""
                SELECT r.room_number, r.guest_name, r.main_remark, r.total_remarks
                FROM reservations r
                WHERE r.arrival_date LIKE ?
                AND r.room_number IS NOT NULL AND r.room_number != ''
                ORDER BY CAST(r.room_number AS INTEGER)
            """, (f"{target_str}%",))
            
            arrivals = c.fetchall()
            
            for arr in arrivals:
                arr_dict = dict(arr)
                room = arr_dict["room_number"]
                guest = arr_dict["guest_name"]
                
                task = {
                    "room": room,
                    "tasktype": "ARRIVAL",
                    "priority": "HIGH",
                    "description": f"Prepare room {room} for {guest} arrival",
                    "notes": []
                }
                
                remarks = f"{arr_dict.get('main_remark') or ''} {arr_dict.get('total_remarks') or ''}".lower()
                if '2t' in remarks:
                    task["notes"].append("2 TWIN BEDS")
                if 'accessible' in remarks or 'disabled' in remarks:
                    task["notes"].append("ACCESSIBLE ROOM")
                
                tasks.append(task)
        
        finally:
            conn.close()
        
        return tasks


    def cancel_checkin(self, stay_id: int):
        stay = self.fetch_one("SELECT * FROM stays WHERE id = ?", (stay_id,))
        if not stay:
            return False, "Stay not found"
        
        self.execute("DELETE FROM stays WHERE id = ?", (stay_id,))
        self.execute(
            "UPDATE rooms SET status = 'VACANT' WHERE room_number = ?",
            (stay["room_number"],),
        )
        return True, "Check-in cancelled successfully"


    def cancel_checkout(self, stay_id: int):
        stay = self.fetch_one("SELECT * FROM stays WHERE id = ?", (stay_id,))
        if not stay:
            return False, "Stay not found"
        if stay["status"] != "CHECKED_OUT":
            return False, "Not checked out"
        
        self.execute("UPDATE stays SET status = 'CHECKED_IN', checkout_actual = NULL WHERE id = ?", (stay_id,))
        self.execute("UPDATE rooms SET status = 'OCCUPIED' WHERE room_number = ?", (stay["room_number"],))
        return True, f"Check-out cancelled - room {stay['room_number']} back to in-house"



    def is_valid_room_number(self, room_number: str) -> tuple[bool, str]:
        """Check if room number is valid (integer within ROOM_BLOCKS)"""
        if not room_number or not room_number.strip():
            return False, "Room number cannot be empty"
        
        # Try to parse as integer (reject decimals)
        try:
            # First check if it contains a decimal point
            if '.' in room_number.strip():
                return False, "Room number cannot have decimals. Use whole numbers only"
            
            room_int = int(room_number.strip())
        except:
            return False, "Room number must be a valid whole number"
        
        # Check if it's in valid ranges
        for start, end in ROOM_BLOCKS:
            if start <= room_int <= end:
                return True, str(room_int)
        
        # Not in any valid range
        valid_ranges = ", ".join([f"{s}-{e}" for s, e in ROOM_BLOCKS])
        return False, f"Room {room_int} not in valid ranges: {valid_ranges}"

    def check_room_available_for_assignment(self, room_number: str, arrival_date: date, depart_date: date, exclude_reservation_id: int = None):
        if not room_number or not room_number.strip():
            return True, ""
        try:
            rn = str(int(float(room_number.strip())))
        except:
            return False, "Invalid room number format"
        
        params = [rn, depart_date.isoformat(), arrival_date.isoformat()]
        sql = """
            SELECT r.id, r.guest_name, r.arrival_date, r.depart_date, r.reservation_no
            FROM reservations r
            WHERE r.room_number = ?
            AND r.arrival_date < ?
            AND r.depart_date > ?
        """

        if exclude_reservation_id is not None:
            sql += " AND r.id != ?"
            params.append(exclude_reservation_id)

        conflict = self.fetch_one(sql, tuple(params))
       
        if conflict:
            return False, f"Room {rn} occupied by {conflict['guest_name']} (Res #{conflict['reservation_no']})"
        return True, ""


   
    def reservations_empty(self):
        result = self.fetch_one("SELECT COUNT(*) as cnt FROM reservations")
        return result["cnt"] == 0 if result else True


    def build_reservations_from_df(self, df: pd.DataFrame):
           df.columns = [str(c).strip() for c in df.columns]
           
           # Simple mapping - keep everything as strings initially
           df_clean = pd.DataFrame({
               "arrival_date": pd.to_datetime(df.get("Arrival Date"), errors='coerce'),
               "depart_date": pd.to_datetime(df.get("Depart"), errors='coerce'),
               "room_number": df.get("Room"),
               "room_type_code": df.get("Room type"),
               "adults": pd.to_numeric(df.get("AD"), errors='coerce').fillna(1).astype(int),
               "children": 0,
               "total_guests": pd.to_numeric(df.get("Tot. guests"), errors='coerce').fillna(1).astype(int),
               "reservation_no": df.get("Reservation No.").astype(str),
               "voucher": df.get("Voucher").astype(str) if "Voucher" in df.columns else None,
               "guest_name": df.get("Guest or Group's name"),
               "main_client": df.get("Main client"),
               "nights": pd.to_numeric(df.get("Nights"), errors='coerce'),
               "meal_plan": df.get("Meal Plan"),
               "rate_code": df.get("Rate"),
               "channel": df.get("Chanl"),
               "main_remark": df.get("Main Rem."),
               "contact_name": df.get("Contact person"),
               "contact_email": df.get("E-mail"),
               "source_of_business": df.get("Source of Business"),
           })
           
           # Drop rows with invalid dates
           df_clean = df_clean.dropna(subset=["arrival_date", "depart_date"])
           
           # Replace remaining NaT/NaN with None
           df_clean = df_clean.where(pd.notna(df_clean), None)
           
           return df_clean



    def import_arrivals_file(self, path: str):
        try:
            df = pd.read_excel(path)
            df_db = self.build_reservations_from_df(df)
            from contextlib import closing
            with closing(self.get_conn()) as conn:
                df_db.to_sql("reservations", conn, if_exists="append", index=False)
            return len(df_db)
        except Exception as e:
            st.error(f"Import error: {e}")
            return 0



    def import_all_arrivals_from_fs(self) -> int:
        pattern = os.path.join(ARRIVALS_ROOT, "**", "Arrivals *.XLSX")
        files = sorted(glob(pattern, recursive=True))
        total = 0
        for path in files:
            total += self.import_arrivals_file(path)
        return total

    def get_arrivals_for_date(self, d: date):
        return self.fetch_all(
            """
            SELECT r.*
            FROM reservations r
            WHERE date(r.arrival_date) = date(?)
            AND NOT EXISTS (
                SELECT 1 FROM stays s
                WHERE s.reservation_id = r.id
                    AND s.status IN ('CHECKED_IN','CHECKED_OUT')
            )
            ORDER BY COALESCE(r.room_number, ''), r.guest_name
            """,
            (d.isoformat(),),
        )



    def update_reservation_room(self, resid: int, room_number: str):
        if not room_number or not room_number.strip():
            return False, "Room number cannot be empty"

        isvalid, result = self.is_valid_room_number(room_number)
        if not isvalid:
            return False, result

        with closing(self.get_conn()) as conn:
            c = conn.cursor()
            c.execute("SELECT arrival_date, depart_date FROM reservations WHERE id = ?", (resid,))
            res = c.fetchone()

        if not res:
            return False, "Reservation not found"

        arr = datetime.fromisoformat(res["arrival_date"]).date()
        dep = datetime.fromisoformat(res["depart_date"]).date()


        available, msg = self.check_room_available_for_assignment(result, arr, dep, resid)
        if not available:
            return False, msg

        with closing(self.get_conn()) as conn, conn:
            c = conn.cursor()
            c.execute(
                "UPDATE reservations SET room_number = ?, updated_at = datetime('now') WHERE id = ?",
                (result, resid),
            )

        return True, f"Room {result} assigned successfully"



    
    def get_checked_out_for_date(self, d: date):
        return self.fetch_all(
            """
            SELECT s.*, r.guest_name, r.reservation_no
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE s.status = 'CHECKED_OUT'
            AND date(s.checkout_actual) = date(?)
            ORDER BY CAST(s.room_number AS INTEGER)
            """,
            (d.isoformat(),),
        )




    # ---- rooms / stays ----

    def ensure_room_exists(self, room_number: str):
        if not room_number:
            return
        self.execute("""
            INSERT INTO rooms (room_number, status) VALUES (:room, 'VACANT')
            ON CONFLICT (room_number) DO NOTHING
        """, {"room": room_number.strip()})

    def check_room_conflict(self, room_number: str, d: date):
    # This method is no longer needed - already handled by check_room_available_for_assignment
        return []


    def checkin_reservation(self, res_id: int):
        res = self.fetch_one("SELECT * FROM reservations WHERE id = ?", (res_id,))

        if not res:
            return False, "Reservation not found"
        if not res["room_number"]:
            return False, "Assign a room first"
        
        is_valid, result = self.is_valid_room_number(res["room_number"])
        if not is_valid:
            return False, result
        
        # if res["arrival_date"] < date.today():
        #     return False, f"Cannot check in for past date"
        
        self.ensure_room_exists(result)
        
        self.execute("""
            INSERT INTO stays (reservation_id, room_number, status, checkin_planned, checkout_planned, checkin_actual)
            VALUES (:res_id, :room, 'CHECKED_IN', :arr, :dep, CURRENT_TIMESTAMP)
        """, {"res_id": res_id, "room": result, "arr": res["arrival_date"], "dep": res["depart_date"]})
        
        self.execute(
    "UPDATE rooms SET status = 'OCCUPIED' WHERE room_number = ?",
    (result,),
)

        return True, "Checked in successfully"
    
    def checkout_stay(self, stay_id: int):
        # Try to find existing stay by stay_id
        stay = self.fetch_one("SELECT * FROM stays WHERE id = ?", (stay_id,))
        
        if stay:
            # Existing stay: mark as checked out
            self.execute(
                "UPDATE stays SET status = 'CHECKED_OUT', checkout_actual = datetime('now') WHERE id = ?",
                (stay_id,),
            )
            self.execute(
                "UPDATE rooms SET status = 'VACANT' WHERE room_number = ?",
                (stay["room_number"],),
            )
        else:
            # No stay row: treat stay_id as reservation_id
            res = self.fetch_one("SELECT * FROM reservations WHERE id = ?", (stay_id,))
            if not res or not res["room_number"]:
                return False, "Reservation not found or no room assigned"
            
            self.execute(
                """
                INSERT INTO stays (
                    reservation_id, room_number, status,
                    checkin_planned, checkout_planned,
                    checkin_actual, checkout_actual
                )
                VALUES (?, ?, 'CHECKED_OUT', ?, ?, datetime('now'), datetime('now'))
                """,
                (stay_id, res["room_number"], res["arrival_date"], res["depart_date"]),
            )
            self.execute(
                "UPDATE rooms SET status = 'VACANT' WHERE room_number = ?",
                (res["room_number"],),
            )

        return True, "Checked out successfully"





    def get_inhouse(self, target_date: date = None):
        """Get only CHECKED_IN guests who are actually in the hotel"""
        if not target_date:
            target_date = date.today()
        
        return self.fetch_all(
        """
        SELECT 
            s.id AS stay_id,
            s.reservation_id AS id,
            r.reservation_no,
            r.guest_name,
            s.room_number,
            s.checkin_planned,
            s.checkout_planned,
            r.meal_plan AS breakfast_code,
            r.main_remark AS comment,
            COALESCE(s.parking_space, '') AS parking_space,
            COALESCE(s.parking_plate, '') AS parking_plate,
            s.status
        FROM stays s
        JOIN reservations r ON r.id = s.reservation_id
        WHERE s.status = 'CHECKED_IN'
        AND date(s.checkin_planned) <= date(?)
        AND date(s.checkout_planned) >= date(?)
        ORDER BY s.room_number
        """,
        (target_date.isoformat(), target_date.isoformat()),
    )






    def get_departures_for_date(self, d: date):
        return self.fetch_all(
            """
            SELECT s.id AS stay_id, r.id, r.reservation_no, r.guest_name, s.room_number,
                s.checkin_planned, s.checkout_planned, s.status
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE s.status = 'CHECKED_IN'
            AND date(s.checkout_planned) = date(?)
            ORDER BY CAST(s.room_number AS INTEGER)
            """,
            (d.isoformat(),),
        )




    def checkout_stay(self, stay_id: int):
        """Checkout a guest - handles both stay IDs and reservation IDs"""
    
        # Try to find existing stay
        stay = self.fetch_one("SELECT * FROM stays WHERE id = ?", (stay_id,))
        
        if stay:
            # Actual stay exists - update it
            self.execute(
            "UPDATE stays SET status = 'CHECKED_OUT', checkout_actual = datetime('now') WHERE id = ?",
            (stay_id,),
        )
            self.execute(
            "UPDATE rooms SET status = 'VACANT' WHERE room_number = ?",
            (stay["room_number"],),
)
        else:
            # No stay exists - create one as checked out
            res = self.fetch_one("SELECT * FROM reservations WHERE id = ?", (stay_id,))

            
            if not res or not res["room_number"]:
                return False, "Reservation not found or no room assigned"
            
            self.execute("""
                INSERT INTO stays (reservation_id, room_number, status, 
                                checkin_planned, checkout_planned, 
                                checkin_actual, checkout_actual)
                VALUES (:res_id, :room, 'CHECKED_OUT', :arr, :dep, 
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, {
                "res_id": stay_id, 
                "room": res["room_number"], 
                "arr": res["arrival_date"], 
                "dep": res["depart_date"]
            })
            
            self.execute(
    "UPDATE rooms SET status = 'VACANT' WHERE room_number = ?",
    (res["room_number"],),
)

        
        return True, "Checked out successfully"



    from contextlib import closing

    def seed_rooms_from_blocks(self):
        with closing(self.get_conn()) as conn, conn:
            c = conn.cursor()
            for start, end in ROOM_BLOCKS:
                for rn in range(start, end + 1):
                    c.execute(
                        "INSERT OR IGNORE INTO rooms (room_number, status) VALUES (?, 'VACANT')",
                        (str(rn),),
                    )


    def sync_room_status_from_stays(self):
        self.execute("UPDATE rooms SET status = 'VACANT'")
        occupied = self.fetch_all("SELECT DISTINCT room_number FROM stays WHERE status = 'CHECKED_IN'")
        for row in occupied:
            self.execute(
    "UPDATE rooms SET status = 'OCCUPIED' WHERE room_number = ?",
    (row["room_number"],),
)

    
    def update_parking_for_stay(self, stay_id: int, space: str, plate: str, notes: str):
        self.execute(
    """
    UPDATE stays
    SET parking_space = ?, parking_plate = ?, parking_notes = ?
    WHERE id = ?
    """,
    (space, plate, notes, stay_id),
)

    # ---- parking helpers ----

    def get_parking_overview_for_date(self, d: date):
        return self.fetch_all(
            """
            SELECT s.*, r.guest_name
            FROM stays s
            JOIN reservations r ON r.id = s.reservation_id
            WHERE date(s.checkin_planned) <= date(?)
            AND date(s.checkout_planned) > date(?)
            AND (s.parking_space IS NOT NULL OR s.parking_plate IS NOT NULL)
            ORDER BY s.parking_space, CAST(s.room_number AS INTEGER)
            """,
            (d.isoformat(), d.isoformat()),
        )




    def add_task(self, task_date: date, title: str, created_by: str, assigned_to: str, comment: str):
        self.execute("""
            INSERT INTO tasks (task_date, title, created_by, assigned_to, comment)
            VALUES (:date, :title, :by, :to, :comment)
        """, {"date": task_date, "title": title, "by": created_by, "to": assigned_to, "comment": comment})
    
    def get_tasks_for_date(self, d: date):
        return self.fetch_all("SELECT * FROM tasks WHERE task_date = :date ORDER BY created_at", {"date": d})
    
    def add_no_show(self, arrival_date: date, guest_name: str, main_client: str, charged: bool, 
                amount_charged: float, amount_pending: float, comment: str):
        # Check if already exists
        existing = self.fetch_one("""
            SELECT id FROM no_shows 
            WHERE guest_name = :guest AND arrival_date = :date
        """, {"guest": guest_name, "date": arrival_date})
        
        if existing:
           # Update existing
            self.execute(
                """
                UPDATE no_shows SET
                    main_client = ?,
                    charged = ?,
                    amount_charged = ?,
                    amount_pending = ?,
                    comment = ?
                WHERE id = ?
                """,
                (
                    main_client,
                    int(charged),
                    amount_charged or 0,
                    amount_pending or 0,
                    comment,
                    existing["id"],
                ),
            )

        else:
            # Insert new
            self.execute("""
                INSERT INTO no_shows (arrival_date, guest_name, main_client, charged, 
                                    amount_charged, amount_pending, comment)
                VALUES (:date, :guest, :client, :charged, :amt_charged, :amt_pending, :comment)
            """, {
                "date": arrival_date,
                "guest": guest_name,
                "client": main_client,
                "charged": int(charged),
                "amt_charged": amount_charged or 0,
                "amt_pending": amount_pending or 0,
                "comment": comment
            })


    def get_no_shows_for_date(self, d: date):
        return self.fetch_all("SELECT * FROM no_shows WHERE arrival_date = :date ORDER BY created_at", {"date": d})
    
    def get_twin_rooms(self):
        rows = self.fetch_all("SELECT room_number FROM rooms WHERE is_twin = 1 ORDER BY CAST(room_number AS INTEGER)")
        return [r["room_number"] for r in rows]
    
    def get_all_rooms(self):
        rows = self.fetch_all("SELECT room_number FROM rooms ORDER BY CAST(room_number AS INTEGER)")
        return [r["room_number"] for r in rows]
    
    def set_spare_rooms_for_date(self, target_date: date, rooms: list):
        self.execute("DELETE FROM spare_rooms WHERE target_date = :date", {"date": target_date})
        for rn in rooms:
            self.execute("INSERT INTO spare_rooms (target_date, room_number) VALUES (:date, :room)", 
                         {"date": target_date, "room": rn})
    
    def get_spare_rooms_for_date(self, target_date: date):
        rows = self.fetch_all("""
            SELECT room_number FROM spare_rooms WHERE target_date = :date
            ORDER BY CAST(room_number AS INTEGER)
        """, {"date": target_date})
        return [r["room_number"] for r in rows]
    
    def search_reservations(self, q: str):
        like_pattern = f"%{q}%"
        return self.fetch_all(
            """
            SELECT * FROM reservations
            WHERE guest_name LIKE ?
            OR room_number LIKE ?
            OR reservation_no LIKE ?
            OR main_client LIKE ?
            OR channel LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern, like_pattern, like_pattern, like_pattern, like_pattern),
        )

    
    def read_table(self, name: str):
        from contextlib import closing
        with closing(self.get_conn()) as conn:
            return pd.read_sql_query(f"SELECT * FROM {name}", conn)



    def export_arrivals_excel(self, d: date):
        rows = self.get_arrivals_for_date(d)
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        preferred_order = [
            "amount_pending", "arrival_date", "room_number", "room_type_code",
            "adults", "total_guests", "reservation_no", "voucher",
            "related_reservation", "crs_code", "crs_name", "guest_id_raw",
            "guest_name", "vip_flag", "client_id", "main_client", "nights",
            "depart_date", "meal_plan", "rate_code", "channel",
            "cancellation_policy", "main_remark", "contact_name",
            "contact_phone", "contact_email", "total_remarks",
            "source_of_business", "stay_option_desc", "remarks_by_chain"
        ]
        cols = [c for c in preferred_order if c in df.columns] + [
            c for c in df.columns if c not in preferred_order
        ]
        df = df[cols]
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Arrivals")
        output.seek(0)
        return output

    def export_inhouse_excel(self, d: date):
        inhouse_rows = self.get_inhouse()
        dep_rows = self.get_departures_for_date(d)
        df_inhouse = pd.DataFrame([dict(r) for r in inhouse_rows]) if inhouse_rows else pd.DataFrame()
        df_dep = pd.DataFrame([dict(r) for r in dep_rows]) if dep_rows else pd.DataFrame()
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_inhouse.to_excel(writer, index=False, sheet_name="InHouse")
            df_dep.to_excel(writer, index=False, sheet_name="Departures")
        output.seek(0)
        return output


# =========================
# Streamlit UI
# =========================

db = None  # Will be initialized in main()
 # Use cached connection


def page_breakfast():
    st.header("Breakfast List")
    today = st.date_input("Date", value=date.today(), key="breakfast_date")
    
    breakfast_rows = db.get_breakfast_list_for_date(today)
    
    if not breakfast_rows:
        st.info("No guests with breakfast for this date.")
        return
    
    df_breakfast = pd.DataFrame([dict(r) for r in breakfast_rows])
    
    # Calculate totals
    total_rooms = len(df_breakfast)
    total_adults = df_breakfast["adults"].sum()
    total_children = df_breakfast["children"].sum()
    total_guests = total_adults + total_children
    
    # Summary at top
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Rooms", total_rooms)
    col2.metric("Adults", int(total_adults))
    col3.metric("Children", int(total_children))
    col4.metric("Total Guests", int(total_guests))
    
    st.subheader(f"Breakfast for {today}")
    
    # Prepare display
    df_display = df_breakfast[["room_number", "guest_name", "adults", "children", "total_guests", "meal_plan"]].copy()
    df_display = clean_numeric_columns(df_display, ["room_number", "adults", "children", "total_guests"])
    df_display.columns = ["Room", "Guest Name", "Adults", "Children", "Total", "Meal Plan"]
    df_display.insert(0, '#', range(1, len(df_display) + 1))
    df_display = clean_numeric_columns(df_display, ["Room"]) 
    st.dataframe(df_display, use_container_width=True, hide_index=True)

    
    st.caption(f"Print this list for the kitchen: {total_rooms} rooms, {int(total_guests)} guests with breakfast")

def page_housekeeping():
    st.header("Housekeeping Task List")
    today = st.date_input("Date", value=date.today(), key="hsk_date")
    
    tasks = db.generate_hsk_tasks_for_date(today)
    
    if not tasks:
        st.info("No housekeeping tasks for this date.")
        return
    
    # Get existing statuses and merge with tasks
    for task in tasks:
        status_data = db.get_hsk_task_status(today, task["room"], task["tasktype"])
        if status_data:
            task["Status"] = status_data["status"]
            task["HSK Notes"] = status_data["notes"] or ""
        else:
            task["Status"] = "PENDING"
            task["HSK Notes"] = ""
    
    # Summary metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Tasks", len(tasks))
    col2.metric("Checkouts", len([t for t in tasks if t['tasktype'] == 'CHECKOUT']))
    col3.metric("Stayovers", len([t for t in tasks if t['tasktype'] == 'STAYOVER']))
    col4.metric("Arrivals", len([t for t in tasks if t['tasktype'] == 'ARRIVAL']))
    completed_count = len([t for t in tasks if t.get('Status') == 'DONE'])
    col5.metric("Completed", completed_count)
    
    # Create editable DataFrame
    df_tasks = pd.DataFrame([
        {
            "#": idx,
            "Room": format_room_number(t["room"]),
            "Type": t["tasktype"],
            "Priority": t["priority"],
            "Task": t["description"],
            "Notes": " | ".join(t["notes"]) if t["notes"] else "",
            "Status": t["Status"],
            "HSK Notes": t["HSK Notes"]
        }
        for idx, t in enumerate(tasks, 1)
    ])
    
    # Display editable table
    st.subheader("Task Tracking")
    edited_df = st.data_editor(
        df_tasks,
        use_container_width=True,
        hide_index=True,
        disabled=["#", "Room", "Type", "Priority", "Task", "Notes"],  # Only Status and HSK Notes are editable
        column_config={
            "Status": st.column_config.SelectboxColumn(
                "Status",
                options=["PENDING", "DONE"],
                required=True
            ),
            "HSK Notes": st.column_config.TextColumn(
                "HSK Notes",
                help="Add notes here (e.g., 'Cleaned', 'Extra towels needed')",
                max_chars=200
            )
        }
    )
    
    # Save button
    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("Save", type="primary", use_container_width=True):
            # Save each task status
            for idx, row in edited_df.iterrows():
                original_task = tasks[idx]
                db.update_hsk_task_status(
                    today,
                    original_task["room"],
                    original_task["tasktype"],
                    row["Status"],
                    row["HSK Notes"]
                )
            st.success("✅ All changes saved!")
            st.rerun()
    
    # Download CSV
    csv = edited_df.to_csv(index=False)
    st.download_button(
        label="Download HSK List",
        data=csv,
        file_name=f"HSK_{today.strftime('%Y%m%d')}.csv",
        mime="text/csv",
        use_container_width=True
    )
    
    # Summary
    pending = len(edited_df[edited_df["Status"] == "PENDING"])
    completed = len(edited_df[edited_df["Status"] == "DONE"])
    st.caption(f"Total: {len(tasks)} tasks | ✅ Completed: {completed} | ⏳ Pending: {pending}")


def page_arrivals():
    st.header("Arrivals")
    d = st.date_input("Arrival date", value=date.today(), key="arrivals_date")
    
    rows = db.get_arrivals_for_date(d)
    if not rows:
        st.info("No arrivals for this date.")
        return
    
    st.subheader(f"Arrivals list ({len(rows)} reservations)")
    
    for idx, r in enumerate(rows, 1):
        res_no = int(float(r['reservation_no'])) if r.get('reservation_no') and str(r['reservation_no']) not in ['None', ''] else r.get('reservation_no', '')
        with st.expander(f"{idx} - {r['guest_name']} |  Reservation No.: {res_no}", expanded=True):
            # Add check-in/checkout dates at top
            col1, col2, col3, col4 = st.columns(4)
            col1.write(f"Arrival: {format_date(r['arrival_date'])}")
            col2.write(f"Departure: {format_date(r['depart_date'])}")

            col3.write(f"**Nights:** {r.get('nights', '')}")
            col4.write(f"**Guests:** {r.get('total_guests', '')}")
            
            col1, col2, col3 = st.columns(3)
            col1, col2, col3 = st.columns(3)
            col1.write(f"**Room type:** {r['room_type_code']}")
            col2.write(f"**Channel:** {r['channel']}")
            col3.write(f"**Meal Plan:** {r.get('meal_plan', 'RO')}")

            
            if r["main_remark"]:
                st.info(r["main_remark"])
            if r["total_remarks"]:
                st.caption(r["total_remarks"])
            
            current_room = r["room_number"] or ""
            room = st.text_input("Room Number", value=current_room, key=f"room_{r['id']}", 
                                placeholder="Enter room number")
            
            col_btn1, col_btn2 = st.columns(2)
            
            with col_btn1:
                if st.button("Save Room", key=f"save_{r['id']}", type="primary", use_container_width=True):
                    if room and room.strip():
                        success, msg = db.update_reservation_room(r["id"], room)
                        if success:
                            st.success(msg)
                        else:
                            st.error(msg)
                    else:
                        st.warning("Please enter a room number")

            with col_btn2:
                if st.button("Check-in", key=f"checkin_{r['id']}", type="secondary", use_container_width=True):
                    success, msg = db.checkin_reservation(r["id"])
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)




def page_inhouse_list():
    st.header("In-House List")
    today = st.date_input("Date", value=date.today(), key="inhouse_list_date")
    
    st.subheader(f"Guests in hotel on {today.strftime('%d %B %Y')}")
    inhouse_rows = db.get_inhouse(today)
    
    if not inhouse_rows:
        st.info("No guests scheduled for this date.")
    else:
        # Build clean DataFrame with proper column names
        df_inhouse = pd.DataFrame([{
            "Room": r["room_number"],
            "Guest Name": r["guest_name"],
            "Status": r["status"],
            "Arrival": r["checkin_planned"],
            "Departure": r["checkout_planned"],
            "Meal Plan": r["breakfast_code"] if r["breakfast_code"] else "",
            "Parking": r["parking_space"] if r["parking_space"] else "",
            "Notes": r["comment"] if r["comment"] else ""
        } for r in inhouse_rows])
        df_inhouse = clean_numeric_columns(df_inhouse, ["Room"])
        st.dataframe(df_inhouse, use_container_width=True, hide_index=True)
        st.caption(f"{len(df_inhouse)} guests in-house")
        
        # Cancel check-in section
        checked_in_guests = [dict(r) for r in inhouse_rows if r["status"] == "CHECKED_IN"]
        
        if checked_in_guests:
            st.divider()
            st.subheader("Cancel check-in")
            for idx, guest in enumerate(checked_in_guests, 1):
                col1, col2 = st.columns([4, 1])
                col1.write(f"**{idx}.** Room {guest['room_number']} - {guest['guest_name']}")
                if col2.button("Cancel", key=f"cancel_{idx}_{guest['id']}", use_container_width=True):
                    success, msg = db.cancel_checkin(guest["stay_id"])
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

def page_checkout_list():
    st.header("Check-out List")
    today = st.date_input("Date", value=date.today(), key="checkout_date")
    
    st.subheader(f"Guests checking out on {today.strftime('%d %B %Y')}")
    dep_rows = db.get_departures_for_date(today)
    
    if not dep_rows:
        st.info("No departures scheduled for this date.")
    else:
        st.caption(f"{len(dep_rows)} departures scheduled")
        df_dep = pd.DataFrame([{
            "Room": r["room_number"],
            "Guest Name": r["guest_name"],
            "Arrival": r["checkin_planned"],
            "Departure": r["checkout_planned"],
            "Status": r["status"]
        } for r in dep_rows])
        df_dep = clean_numeric_columns(df_dep, ["Room"])
        st.dataframe(df_dep, use_container_width=True, hide_index=True)
        
        st.subheader("Quick checkout")
        for idx, row_data in enumerate(dep_rows, 1):
            row_dict = dict(row_data)
            
            # Create a bordered card for each guest
            with st.container():
                col1, col2 = st.columns([5, 1])
                
                with col1:
                    st.markdown(f"""
                    <div style="
                        border: 2px solid #e0e0e0; 
                        border-radius: 8px; 
                        padding: 12px; 
                        background-color: #f9f9f9;
                        margin-bottom: 8px;
                    ">
                        <strong style="font-size: 16px;">{idx}. Room {format_room_number(row_dict['room_number'])} - {row_dict['guest_name']}</strong>
                    </div>
                    """, unsafe_allow_html=True)
                
                with col2:
                    if st.button("Check-out", key=f"co_{idx}_{row_dict['stay_id']}", use_container_width=True):
                        success, msg = db.checkout_stay(int(row_dict["stay_id"]))
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

                
            
    st.divider()
    st.subheader(f"Already checked out on {today.strftime('%d %B %Y')}")
    checkout_rows = db.get_checked_out_for_date(today)
    
    if not checkout_rows:
        st.info("No check-outs completed for this date.")
    else:
        df_checkout = pd.DataFrame([{
            "Room": r["room_number"],
            "Guest Name": r["guest_name"],
            "Planned": r["checkout_planned"],
            "Actual": r["checkout_actual"]
        } for r in checkout_rows])
        df_checkout = clean_numeric_columns(df_checkout, ["Room"])
        st.dataframe(df_checkout, use_container_width=True, hide_index=True)
        st.caption(f"{len(df_checkout)} completed check-outs")
        
        st.subheader("Cancel check-out")
        for idx, row_data in enumerate(checkout_rows, 1):
            row_dict = dict(row_data)
            col1, col2 = st.columns([4, 1])
            col1.write(f"**{idx}.** Room {row_dict['room_number']} - {row_dict['guest_name']}")
            if col2.button("Undo", key=f"undo_{row_dict['id']}", use_container_width=True):
                success, msg = db.cancel_checkout(row_dict["id"])
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)



def page_tasks_handover():
    st.header("Handover")
    d = st.date_input("Date", value=date.today(), key="tasks_date")

    st.subheader("Add task")
    col1, col2 = st.columns(2)
    title = col1.text_input("Task")
    created_by = col2.text_input("By")
    assigned_to = col1.text_input("To")
    comment = col2.text_input("Comment")
    if st.button("Add Handover"):
        if title:
            db.add_task(d, title, created_by, assigned_to, comment)
            st.success("Handover added.")
        else:
            st.error("Handover title required.")

    st.subheader("Handover for this date")
    rows = db.get_tasks_for_date(d)
    df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    if df.empty:
        st.info("No Handovers.")
    else:
        st.dataframe(df[["task_date", "title", "created_by", "assigned_to", "comment"]],hide_index=True)


def page_no_shows():
    st.header("No Shows")
    d = st.date_input("Arrival date", value=date.today(), key="no_show_date")
    
    st.subheader("Add no-show")
    
    # Get potential no-shows
    potential = db.get_potential_no_shows(d)
    
    with st.form("no_show_form", clear_on_submit=True):
        if potential:
            guest_options = ["Select a guest..."] + [
                f"{g['guest_name']} (Res {g['reservation_no']}) - {g.get('main_client', '')}" 
                for g in potential
            ]
            
            selected_idx = st.selectbox("Guest who didn't show up", options=range(len(guest_options)),
                                       format_func=lambda x: guest_options[x])
            
            if selected_idx > 0:
                guest_data = potential[selected_idx - 1]
                guest_name = guest_data['guest_name']
                main_client = guest_data.get('main_client', '')
                # st.info(f"Selected: {guest_name}")
            else:
                guest_name = st.text_input("Guest Name (manual-optional)")
                main_client = st.text_input("Main Client")
        else:
            st.info("No expected arrivals for this date")
            guest_name = st.text_input("Guest Name")
            main_client = st.text_input("Main Client")
        
        col1, col2 = st.columns(2)
        amount_charged = col1.number_input("Amount Charged (£)", min_value=0.0, step=0.01, format="%.2f")
        amount_pending = col2.number_input("Amount Pending (£)", min_value=0.0, step=0.01, format="%.2f")
        
        charged = st.checkbox("Payment Received")
        comment = st.text_area("Comment")
        
        submitted = st.form_submit_button("Add No-Show", type="primary", use_container_width=True)
        
        if submitted and guest_name:
            # Add to database
            db.add_no_show(d, guest_name, main_client, charged, amount_charged, amount_pending, comment)
            st.success(f"✓ No-show added: {guest_name}")
    
    st.divider()
    st.subheader(f"No-shows for {d.strftime('%d %B %Y')}")
    rows = db.get_no_shows_for_date(d)
    
    if not rows:
        st.info("No no-shows recorded.")
    else:
        df = pd.DataFrame([{
            "Guest": r["guest_name"],
            "Client": r["main_client"] if r.get("main_client") else "",
            "Charged": f"£{float(r['amount_charged']):.2f}" if r.get('amount_charged') is not None else "£0.00",
            "Pending": f"£{float(r['amount_pending']):.2f}" if r.get('amount_pending') is not None else "£0.00",
            "Paid": "✓" if r["charged"] else "✗",
            "Comment": r.get("comment", "")
        } for r in rows])
        
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"{len(rows)} no-shows")



def page_search():
    st.header("Search Reservations")
    
    col1, col2 = st.columns([1, 3])
    
    with col1:
        search_type = st.selectbox(
            "Search by",
            [
                
                "Room Number",
                "Guest Name",
                "Reservation No",
                "Main Client",
                "Channel",
                "All Fields"
            ]
        )
    
    with col2:
        q = st.text_input(
            "Search term",
            placeholder=f"Enter {search_type.lower()}...",
            key="search_input"
        )
    
    if not q:
        st.info("Enter a search term to find reservations.")
        return
    
    # Build query based on search type
    like_pattern = f"%{q}%"
    
    if search_type == "Room Number":
        # Exact or partial room match
        rows = db.fetch_all(
            """
            SELECT * FROM reservations
            WHERE room_number LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern,),
        )
    elif search_type == "Guest Name":
        rows = db.fetch_all(
            """
            SELECT * FROM reservations
            WHERE guest_name LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern,),
        )
    elif search_type == "Reservation No":
        rows = db.fetch_all(
            """
            SELECT * FROM reservations
            WHERE reservation_no LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern,),
        )
    elif search_type == "Main Client":
        rows = db.fetch_all(
            """
            SELECT * FROM reservations
            WHERE main_client LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern,),
        )
    elif search_type == "Channel":
        rows = db.fetch_all(
            """
            SELECT * FROM reservations
            WHERE channel LIKE ?
            ORDER BY arrival_date DESC
            LIMIT 500
            """,
            (like_pattern,),
        )
    else:  # All Fields
        rows = db.search_reservations(q)
    
    # Display results
    if not rows:
        st.warning(f"No reservations found matching '{q}' in {search_type}.")
        return
    
    st.success(f"Found {len(rows)} reservation(s)")
    
    # Create display DataFrame
    df = pd.DataFrame([dict(r) for r in rows])
    df_clean = clean_numeric_columns(df, ["room_number", "reservation_no"])
    
    # Select and format columns for display
    display_cols = [
        "arrival_date", "depart_date", "guest_name", "room_number",
        "reservation_no", "channel", "rate_code", "main_client", "main_remark"
    ]
    
    # Only show columns that exist
    display_cols = [col for col in display_cols if col in df_clean.columns]
    
    st.dataframe(
        df_clean[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "arrival_date": st.column_config.TextColumn("Arrival"),
            "depart_date": st.column_config.TextColumn("Departure"),
            "guest_name": st.column_config.TextColumn("Guest Name"),
            "room_number": st.column_config.TextColumn("Room"),
            "reservation_no": st.column_config.TextColumn("Res No"),
            "channel": st.column_config.TextColumn("Channel"),
            "rate_code": st.column_config.TextColumn("Rate"),
            "main_client": st.column_config.TextColumn("Client"),
            "main_remark": st.column_config.TextColumn("Remarks"),
        }
    )
    
    # Show detailed view option
    with st.expander("📋 View Full Details"):
        for idx, row in enumerate(rows, 1):
            with st.container():
                st.markdown(f"### {idx}. {row['guest_name']} - Room {format_room_number(row.get('room_number', 'Not assigned'))}")
                
                col1, col2, col3, col4 = st.columns(4)
                col1.write(f"**Arrival:** {format_date(row['arrival_date'])}")
                col2.write(f"**Departure:** {format_date(row['depart_date'])}")
                col3.write(f"**Nights:** {row.get('nights', 'N/A')}")
                col4.write(f"**Guests:** {row.get('total_guests', 'N/A')}")
                
                col1, col2, col3 = st.columns(3)
                col1.write(f"**Res No:** {row.get('reservation_no', 'N/A')}")
                col2.write(f"**Channel:** {row.get('channel', 'N/A')}")
                col3.write(f"**Meal Plan:** {row.get('meal_plan', 'N/A')}")
                
                if row.get('main_remark'):
                    st.info(f"📝 {row['main_remark']}")
                
                if row.get('main_client'):
                    st.caption(f"Client: {row['main_client']}")
                
                st.divider()

def page_room_list():
    st.header("Room List")
    st.caption("Manage room inventory and twin flags")
    
    df = db.read_table("rooms")
    if df.empty:
        st.info("No rooms yet (should have been seeded).")
        return
    
    st.subheader("Rooms")
    
    # Display only room_number and status
    df_display = df[["room_number", "status"]].copy()
    df_display = df_display.sort_values(by="room_number", key=lambda s: pd.to_numeric(s, errors='coerce'))
    df_display.columns = ["Room", "Status"]
    
    st.dataframe(df_display, use_container_width=True, hide_index=True)
    st.caption(f"Total: {len(df)} rooms")


def page_spare_rooms():
    st.header("Spare Twin rooms")
    st.caption("Mark rooms as spare twins for a specific date (e.g. Spare Twin List).")

    target = st.date_input("Date", value=date.today(), key="spare_date")

    all_rooms = db.get_all_rooms()
    if not all_rooms:
        st.info("No rooms in inventory yet. Fill the Room list first.")
        return

    current_spare = db.get_spare_rooms_for_date(target)
    twin_rooms = set(db.get_twin_rooms())

    st.subheader("Select spare twins rooms")
    selected = st.multiselect(
        "spare twins rooms for this date",
        options=all_rooms,
        default=current_spare,
        help="Include twin and non-twin rooms; twin rooms are listed below.",
    )

    if twin_rooms:
        st.caption(
            "Twin rooms (for reference): " +
            ", ".join(sorted(twin_rooms, key=int))
        )

    if st.button("Save spare twins rooms"):
        db.set_spare_rooms_for_date(target, selected)
        st.success(f"Saved {len(selected)} spare twins rooms for {target}.")

    saved = db.get_spare_rooms_for_date(target)
    st.subheader("Saved spare twins rooms")
    if not saved:
        st.info("No spare twins rooms saved for this date.")
    else:
        st.write(", ".join(saved))


def page_parking():
    st.header("Parking Overview")
    today = st.date_input("Date", value=date.today(), key="parking_date")
    
    inhouse = db.get_inhouse(today)
    
    if not inhouse:
        st.info("No in-house guests for this date.")
        return
    
    inhouse_dicts = [dict(r) for r in inhouse]
    
    # Filter: has parking_space OR "parking" mentioned in notes
    guests_with_parking = [r for r in inhouse_dicts if r.get("parking_space") or (r.get("comment") and ("parking" in r.get("comment", "").lower() or "poa" in r.get("comment", "").lower()))]
    guests_without_parking = [r for r in inhouse_dicts if r not in guests_with_parking]
    
    col1, col2 = st.columns(2)
    col1.metric("Total In-House", len(inhouse_dicts))
    col2.metric("With Parking", len(guests_with_parking))
    
    if guests_with_parking:
        st.subheader("Parking Assigned")
        df_parking = pd.DataFrame([{
            "Space": r.get("parking_space", "See notes"),
            "Room": r["room_number"],
            "Guest Name": r["guest_name"],
            "Plate": r.get("parking_plate", ""),
            "Notes": r.get("comment", "") or r.get("parking_notes", "")
        } for r in guests_with_parking])
        df_parking = clean_numeric_columns(df_parking, ["Room"]) 
        st.dataframe(df_parking, use_container_width=True, hide_index=True)
    else:
        st.info("No parking spaces assigned yet.")
    
    if guests_without_parking:
        st.divider()
        st.subheader("Guests without parking")
        
        for idx, guest in enumerate(guests_without_parking, 1):
            with st.expander(f"{idx}. Room {guest['room_number']} - {guest['guest_name']}", expanded=False):
                col1, col2, col3 = st.columns(3)
                space = col1.text_input("Space", key=f"space_{guest['stay_id']}")
                plate = col2.text_input("Plate", key=f"plate_{guest['stay_id']}")
                notes = col3.text_input("Notes", key=f"notes_{guest['stay_id']}")
                
                if st.button("Assign Parking", key=f"assign_{guest['stay_id']}"):
                    if space:
                        db.update_parking_for_stay(guest["stay_id"], space, plate, notes)
                        st.success(f"Parking {space} assigned to room {guest['room_number']}")
                        st.rerun()
                    else:
                        st.warning("Enter parking space number")


def page_db_viewer():
    

    st.header("Database Viewer")
    
    
    # Database statistics
    st.subheader("Database Overview")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    
    reservations_count = db.fetch_one("SELECT COUNT(*) as cnt FROM reservations")
    stays_count = db.fetch_one("SELECT COUNT(*) as cnt FROM stays")
    rooms_count = db.fetch_one("SELECT COUNT(*) as cnt FROM rooms")
    tasks_count = db.fetch_one("SELECT COUNT(*) as cnt FROM tasks")
    no_shows_count = db.fetch_one("SELECT COUNT(*) as cnt FROM no_shows")
    spare_count = db.fetch_one("SELECT COUNT(*) as cnt FROM spare_rooms")
    
    col1.metric("Reservations", reservations_count['cnt'])
    col2.metric("Stays", stays_count['cnt'])
    col3.metric("Rooms", rooms_count['cnt'])
    col4.metric("Tasks", tasks_count['cnt'])
    col5.metric("No Shows", no_shows_count['cnt'])
    col6.metric("Spare Rooms", spare_count['cnt'])
    
    st.divider()
    
    # Table viewer with filters
    st.subheader("View & Search Tables")
    
    col_table, col_limit = st.columns([3, 1])
    table = col_table.selectbox("Select table", 
                                ["reservations", "stays", "rooms", "tasks", "no_shows", "spare_rooms"])
    limit = col_limit.number_input("Rows to show", min_value=10, max_value=1000, value=100, step=10)
    
    # Search box
    search = st.text_input(f"Search in {table}", placeholder="Enter search term...")
    
    # Fetch data
    if search:
        # Simple search across all text columns
        df = db.read_table(table)
        mask = df.astype(str).apply(lambda row: row.str.contains(search, case=False).any(), axis=1)
        df = df[mask].head(limit)
    else:
        df = db.read_table(table)
        if limit:
            df = df.head(limit)

    
    if df.empty:
        st.info(f"No rows in {table}")
    else:
        # Clean numeric columns
        if table == "reservations":
            df = clean_numeric_columns(df, ["id", "reservation_no", "adults", "children", "total_guests", "nights"])
        elif table == "stays":
            df = clean_numeric_columns(df, ["id", "reservation_id"])
        elif table == "tasks":
            df = clean_numeric_columns(df, ["id"])
        elif table == "no_shows":
            df = clean_numeric_columns(df, ["id"])
        
        st.caption(f"Showing {len(df)} of {reservations_count['cnt'] if table == 'reservations' else '...'} total rows")
        st.dataframe(df, use_container_width=True, height=500)
        
        # Export button
        csv = df.to_csv(index=False)
        st.download_button(
            f"Download {table} as CSV",
            data=csv,
            file_name=f"{table}_{date.today().isoformat()}.csv",
            mime="text/csv"
        )
    
    DB_PATH = "hotel_fo.db"  # or hotel_fo_TEST.db
    
    if os.path.exists(DB_PATH):
        with open(DB_PATH, 'rb') as f:
            backup_data = f.read()
        
        st.download_button(
            "⬇ DOWNLOAD LIVE DATABASE NOW",
            data=backup_data,
            file_name=f"hotel_PRODUCTION_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
            mime="application/octet-stream",
            type="primary"
        )
        st.success(f"Database size: {len(backup_data)/1024:.1f} KB")
    
def page_admin_upload():
    st.header("Admin: Upload Database Data")
    
    # Add password protection
    password = st.text_input("Admin Password", type="password")
    if password != st.secrets.get("ADMIN_PASSWORD", "Raddison2025#"):
        st.warning("Enter admin password to access this page")
        return
    
    tab1, tab2, tab3 = st.tabs(["Upload Full DB", "Upload Stays CSV", "Download DB"])
    
    with tab1:
        st.subheader("Replace Entire Database")
        st.warning("⚠️ This will replace the entire database file")
        
        uploaded_db = st.file_uploader("Upload SQLite database (.db)", type=['db'], key="db_upload")
        
        if uploaded_db:
            st.info(f"File size: {uploaded_db.size / 1024:.1f} KB")
            
            if st.button("Replace Database", type="primary"):
                try:
                    # Backup current DB first
                    import shutil
                    backup_path = DBPATH + ".backup"
                    shutil.copy2(DBPATH, backup_path)
                    
                    # Replace with uploaded
                    with open(DBPATH, 'wb') as f:
                        f.write(uploaded_db.getbuffer())
                    
                    st.success("✅ Database replaced successfully!")
                    st.info("Reloading app...")
                    time.sleep(1)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error: {str(e)}")
    
    with tab2:
        st.subheader("Import Stays from CSV")
        
        uploaded_csv = st.file_uploader("Upload stays CSV", type=['csv'], key="csv_upload")
        
        if uploaded_csv:
            import pandas as pd
            df = pd.read_csv(uploaded_csv)
            
            st.write(f"**Preview:** {len(df)} rows")
            st.dataframe(df.head(10))
            
            st.write("**Expected columns:** `id, reservation_id, room_number, status, checkin_planned, checkout_planned, checkin_actual, checkout_actual, parking_space, parking_plate, parking_notes`")
            
            if st.button("Import Stays", type="primary"):
                try:
                    with st.spinner("Importing stays..."):
                        count = 0
                        for _, row in df.iterrows():
                            db.execute(
                                """
                                INSERT OR REPLACE INTO stays (
                                    id, reservation_id, room_number, status,
                                    checkin_planned, checkout_planned,
                                    checkin_actual, checkout_actual,
                                    parking_space, parking_plate, parking_notes
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    row.get("id"),
                                    row.get("reservation_id"),
                                    row.get("room_number"),
                                    row.get("status", "CHECKED_IN"),
                                    row.get("checkin_planned"),
                                    row.get("checkout_planned"),
                                    row.get("checkin_actual"),
                                    row.get("checkout_actual"),
                                    row.get("parking_space", ""),
                                    row.get("parking_plate", ""),
                                    row.get("parking_notes", ""),
                                ),
                            )
                            count += 1
                        
                        st.success(f"✅ Imported {count} stays")
                    
                    # 1. Update room statuses based on stays
                    with st.spinner("Syncing room statuses..."):
                        db.sync_room_status_from_stays()
                        st.success("✅ Room statuses synced")
                    
                    # 2. Verify linkage between stays and reservations
                    with st.spinner("Verifying data linkage..."):
                        orphaned = db.fetch_one("""
                            SELECT COUNT(*) as cnt FROM stays s
                            LEFT JOIN reservations r ON s.reservation_id = r.id
                            WHERE r.id IS NULL
                        """)
                        
                        if orphaned['cnt'] > 0:
                            st.warning(f"⚠️ Found {orphaned['cnt']} stays without matching reservations")
                        else:
                            st.success("✅ All stays linked to reservations")
                    
                    # 3. Show summary by status
                    with st.spinner("Verifying data..."):
                        checked_in = db.fetch_one(
                            "SELECT COUNT(*) as cnt FROM stays WHERE status = 'CHECKED_IN'"
                        )
                        checked_out = db.fetch_one(
                            "SELECT COUNT(*) as cnt FROM stays WHERE status = 'CHECKED_OUT'"
                        )
                        occupied = db.fetch_one(
                            "SELECT COUNT(*) as cnt FROM rooms WHERE status = 'OCCUPIED'"
                        )
                        
                        # Check departures for today
                        today = date.today()
                        departures_today = db.fetch_one(
                            """
                            SELECT COUNT(*) as cnt
                            FROM reservations r
                            LEFT JOIN stays s ON s.reservation_id = r.id
                            WHERE date(r.depart_date) = date(?)
                            AND (s.status IS NULL OR s.status != 'CHECKED_OUT')
                            """,
                            (today.isoformat(),)
                        )
                        
                        st.info(f"""
                        **Data Summary:**
                        - Checked-in guests: {checked_in['cnt']}
                        - Already checked out: {checked_out['cnt']}
                        - Occupied rooms: {occupied['cnt']}
                        - Departures today: {departures_today['cnt']}
                        """)
                    
                    st.success("🎉 Stays imported successfully!")
                    time.sleep(2)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ Error importing: {str(e)}")
                    st.exception(e)
    with tab3:
        st.subheader("Download Live Database")
        st.info("Downloads a ZIP file containing the database file and CSV exports of all tables")
        
        if st.button("Generate Download Package", type="primary"):
            try:
                import zipfile
                import io
                import pandas as pd
                
                with st.spinner("Creating download package..."):
                    zip_buffer = io.BytesIO()
                    
                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                        # 1. Add database file
                        with open(DBPATH, 'rb') as db_file:
                            zip_file.writestr("hotelfo.db", db_file.read())
                        
                        # 2. Export tables to CSV
                        tables = ['reservations', 'stays', 'rooms', 'no_shows', 'spare_rooms', 'tasks']
                        
                        for table_name in tables:
                            try:
                                rows = db.fetch_all(f"SELECT * FROM {table_name}")
                                
                                if rows:
                                    df = pd.DataFrame([dict(row) for row in rows])
                                    csv_buffer = io.StringIO()
                                    df.to_csv(csv_buffer, index=False)
                                    zip_file.writestr(f"{table_name}.csv", csv_buffer.getvalue())
                                    st.success(f"✅ Exported {table_name}: {len(rows)} rows")
                                else:
                                    st.info(f"ℹ️ {table_name}: empty")
                                    
                            except Exception as e:
                                st.warning(f"⚠️ Could not export {table_name}: {str(e)}")
                    
                    zip_buffer.seek(0)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"hotelfo_backup_{timestamp}.zip"
                    
                    st.download_button(
                        label="⬇️ Download Database Package",
                        data=zip_buffer,
                        file_name=filename,
                        mime="application/zip",
                        use_container_width=True,
                    )
                    
                    st.success("🎉 Download package ready!")
                    
            except Exception as e:
                st.error(f"❌ Error creating download: {str(e)}")
                st.exception(e)



def main():
    st.set_page_config(
        page_title="Radisson Blu Bristol",
        page_icon="🏨",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    menu_options = {
    "Arrivals": page_arrivals,
    "In-House List": page_inhouse_list,
    "Checkout List": page_checkout_list,
    # ... other pages
    "Admin Upload": page_admin_upload,  # Add this
}

    # Initialize database here (after set_page_config)
    global db
    db = FrontOfficeDB(DBPATH)




    with st.sidebar:
        st.title("YesWeCan! Bristol")
        mode = "NEW LIVE MODE"
        st.markdown(f"**{mode}**")
        page = st.radio(
    "Navigate",
    [
        "Arrivals",
        "In-House List",
        "Check-out List",
        "Housekeeping Task-List",
        "Breakfast List",
        "Search",
        "Handover",
        "No Shows",
        "Room list",
        "Spare Twin rooms",
        "Parking",
        "DB Viewer",
        "Admin"
    ],
)



        st.markdown("---")
        st.caption("Welcome to YesWeCan! v1.0")

    if page == "Arrivals":
        page_arrivals()
    elif page == "In-House List":
        page_inhouse_list()
    elif page == "Check-out List":
        page_checkout_list()
    elif page == "Housekeeping Task-List":
        page_housekeeping()
    elif page == "Breakfast List":
        page_breakfast()
    elif page == "Search":
        page_search()
    elif page == "Handover":
        page_tasks_handover()
    elif page == "No Shows":
        page_no_shows()
    elif page == "Room list":
        page_room_list()
    elif page == "Spare Twin rooms":
        page_spare_rooms()
    elif page == "Parking":
        page_parking()
    elif page == "DB Viewer":
        page_db_viewer()
    elif page == "Admin":
        page_admin_upload()


if __name__ == "__main__":
    main()
