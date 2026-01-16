import sqlite3

DBPATH = "hotelfoTEST.db"  # or "hotelfo.db" – use the one with the wrong tables

conn = sqlite3.connect(DBPATH)
c = conn.cursor()

# Drop the two tables if they exist
c.execute("DROP TABLE IF EXISTS noshows;")
c.execute("DROP TABLE IF EXISTS sparerooms;")

conn.commit()
conn.close()

print("Dropped noshows and sparerooms (if they existed).")
