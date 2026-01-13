import sqlite3
import pandas as pd
from sqlalchemy import create_engine

# Your Neon connection
NEON_URL = "postgresql://neondb_owner:npg_93mwOyNgQIHX@ep-calm-forest-abplqcg0-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Production SQLite backup
SQLITE_FILE = "hotel_PRODUCTION_20260113_1500.db"

print("Migrating production data to Neon...")

sqlite_conn = sqlite3.connect(SQLITE_FILE)
neon_engine = create_engine(NEON_URL)

tables = {
    "reservations": ["arrival_date", "depart_date"],
    "stays": ["checkin_planned", "checkout_planned", "checkin_actual", "checkout_actual"],
    "rooms": [],
    "tasks": ["task_date"],
    "no_shows": ["arrival_date"],
    "spare_rooms": ["target_date"]
}

for table, date_cols in tables.items():
    print(f"\n{table}...")
    try:
        df = pd.read_sql_query(f"SELECT * FROM {table}", sqlite_conn)
        
        if df.empty:
            print(f"  Empty")
            continue
        
        # Convert date columns
        for col in date_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce')
        
        # Remove id column (PostgreSQL will auto-generate)
        if 'id' in df.columns:
            df = df.drop('id', axis=1)
        
        # Clean and insert
        df = df.where(pd.notna(df), None)
        df.to_sql(table, neon_engine, if_exists="append", index=False, method='multi')
        
        print(f"  ✅ {len(df)} rows")
    except Exception as e:
        print(f"  ❌ {e}")

sqlite_conn.close()
print("\n✅ Production data migrated to Neon!")
