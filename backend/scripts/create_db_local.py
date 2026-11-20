import psycopg

conn = psycopg.connect("postgresql://twin@localhost:5434/postgres", autocommit=True)
cur = conn.cursor()
cur.execute("SELECT version()")
print(cur.fetchone()[0][:60])
cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", ("personal_twin",))
if not cur.fetchone():
    cur.execute("CREATE DATABASE personal_twin")
    print("db created")
else:
    print("db exists")
conn.close()
