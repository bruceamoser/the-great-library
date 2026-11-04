"""Manual catalog choices survive every filesystem scan."""
import json

def initialize(con):
    con.execute('CREATE TABLE IF NOT EXISTS metadata_overrides(rel_path TEXT PRIMARY KEY, data TEXT NOT NULL)')

def get(con, path):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_overrides'").fetchone():return {}
    row=con.execute('SELECT data FROM metadata_overrides WHERE rel_path=?',(path,)).fetchone()
    return json.loads(row[0]) if row else {}
