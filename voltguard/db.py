"""SQLite storage + seeded demo sites."""
import json
import random
import sqlite3
import threading

from . import sim

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sites(id INTEGER PRIMARY KEY, name TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS circuits(id INTEGER PRIMARY KEY, site_id INT, name TEXT, rated_a REAL, kf REAL,
  profile TEXT, criticality INT, insp_days_ago INT, prior_alerts INT);
CREATE TABLE IF NOT EXISTS readings(circuit_id INT, hour INT, voltage REAL, current REAL, temp REAL, ambient REAL,
  PRIMARY KEY(circuit_id, hour));
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY, circuit_id INT, status TEXT, severity TEXT, pattern TEXT,
  confidence REAL, excess REAL, slope REAL, opened_min INT, ack_min INT, question TEXT, answer TEXT,
  explanation TEXT, guidance TEXT, escalation INT DEFAULT 0, due_min INT, closed_min INT, note TEXT);
CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY, case_id INT, t_min INT, tool TEXT, summary TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS work_orders(id INTEGER PRIMARY KEY, case_id INT, code TEXT, title TEXT, body TEXT,
  priority TEXT, status TEXT, created_min INT);
"""

# (site name, kind, [(circuit, rated A, profile, criticality 1-3, inspection days ago or None, prior alerts)])
SEED = [
    ("Marina Tower B", "Residential building", [
        ("Lift motor feeder", 63, "residential", 3, 40, 0),
        ("Podium distribution board", 100, "residential", 2, 95, 1),
        ("Pool pump circuit", 32, "residential", 1, 200, 0)]),
    ("Camp Site 3", "Labour accommodation", [
        ("Block A distribution board", 100, "camp", 3, 60, 0),
        ("Block C distribution board", 100, "camp", 3, None, 4),
        ("Kitchen feeder", 63, "camp", 2, 120, 1)]),
    ("Mall A", "Shopping mall", [
        ("Chiller plant feeder", 125, "mall", 3, 150, 2),
        ("Food court distribution board", 100, "mall", 2, 80, 0),
        ("Parking lighting circuit", 40, "mall", 1, 30, 0)]),
    ("Data Room 1", "Small data room", [
        ("UPS output circuit", 63, "datacenter", 3, 90, 0),
        ("Cooling unit feeder", 40, "datacenter", 2, 45, 1)]),
]


class DB:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- helpers
    def q(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql, *args):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def meta(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", k)
        return json.loads(r["v"]) if r else default

    def set_meta(self, k, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))

    @property
    def now_min(self):
        return self.meta("clock_min", 0)

    # -- seeding / reset
    def reset(self, seed=7):
        with self.lock:
            for t in ("meta", "sites", "circuits", "readings", "cases", "steps", "work_orders"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
        rng = random.Random(seed)
        for sname, kind, circs in SEED:
            sid = self.x("INSERT INTO sites(name,kind) VALUES(?,?)", sname, kind)
            for name, rated, profile, crit, insp, prior in circs:
                kf = round(rng.uniform(0.85, 1.25), 2)
                cid = self.x("INSERT INTO circuits(site_id,name,rated_a,kf,profile,criticality,insp_days_ago,prior_alerts)"
                             " VALUES(?,?,?,?,?,?,?,?)", sid, name, rated, kf, profile, crit, insp, prior)
                c = {"profile": profile, "rated_a": rated, "kf": kf}
                hist = sim.make_history(c, rng)
                with self.lock:
                    self.conn.executemany(
                        "INSERT INTO readings VALUES(?,?,?,?,?,?)",
                        [(cid, r["hour"], r["voltage"], r["current"], r["temp"], r["ambient"]) for r in hist])
                    self.conn.commit()
        self.set_meta("clock_min", 0)
        self.set_meta("seed", seed)

    # -- domain accessors
    def circuit(self, cid):
        return self.one("SELECT c.*, s.name AS site, s.kind AS site_kind FROM circuits c JOIN sites s ON s.id=c.site_id WHERE c.id=?", cid)

    def readings(self, cid, n=None):
        rows = self.q("SELECT hour, voltage, current, temp, ambient FROM readings WHERE circuit_id=? ORDER BY hour", cid)
        return rows[-n:] if n else rows

    def append_readings(self, cid, rows):
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?)",
                                  [(cid, r["hour"], r["voltage"], r["current"], r["temp"], r["ambient"]) for r in rows])
            self.conn.commit()
