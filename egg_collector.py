#!/usr/bin/env python3
"""
egg_collector.py — runs on the STORAGE machine (not the Pi)

Owns the SQLite egg database and exposes a tiny HTTP API. SQLite is only ever
opened by this process on the machine holding the file — never over a network
share, which corrupts SQLite databases.

Endpoints:
  GET  /health              -> {"ok": true, "rows": N}
  GET  /eggs                -> {"eggs": {"YYYY-MM-DD": {"hen_id": true, ...}}}
  GET  /eggs?days=30        -> same, limited to the last N days
  POST /eggs                 body: {"date":"YYYY-MM-DD","hen":"grun","laid":true}

Start:  python3 egg_collector.py
        python3 egg_collector.py --port 8090 --db /path/eggs.db --token SECRET

Stdlib only — no pip installs. Runs on Linux, macOS or Windows.
"""

import argparse, json, os, re, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

VERSION  = "1.0.0"
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEN_RE   = re.compile(r"^[a-z0-9_]{1,32}$")

_db_lock = threading.Lock()
_db_path = "eggs.db"
_token   = ""


def db():
    # One connection per call keeps this trivially thread-safe; volume is a
    # handful of writes a day, so the overhead is irrelevant.
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")     # durable + concurrent readers
    conn.execute("PRAGMA synchronous=FULL")     # survive power loss
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eggs (
                date       TEXT NOT NULL,
                hen        TEXT NOT NULL,
                laid       INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date, hen)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eggs_date ON eggs(date)")


def row_count():
    with _db_lock, db() as conn:
        return conn.execute("SELECT COUNT(*) FROM eggs").fetchone()[0]


def get_eggs(days=None):
    q, args = "SELECT date, hen, laid FROM eggs", []
    if days:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        q += " WHERE date >= ?"
        args.append(cutoff)
    out = {}
    with _db_lock, db() as conn:
        for date, hen, laid in conn.execute(q, args):
            if laid:
                out.setdefault(date, {})[hen] = True
    return out


def put_egg(date, hen, laid):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock, db() as conn:
        if laid:
            conn.execute(
                "INSERT INTO eggs(date,hen,laid,updated_at) VALUES(?,?,1,?) "
                "ON CONFLICT(date,hen) DO UPDATE SET laid=1, updated_at=excluded.updated_at",
                (date, hen, ts))
        else:
            conn.execute("DELETE FROM eggs WHERE date=? AND hen=?", (date, hen))


def put_bulk(eggs):
    """Save a whole snapshot. Authoritative *per date present in the payload* —
    dates not mentioned are left untouched, so an empty/partial push can never
    wipe history the sender didn't know about.
    Returns (dates_written, rows_written)."""
    ts    = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows  = 0
    with _db_lock, db() as conn:
        for date, hens in eggs.items():
            if not DATE_RE.match(str(date)) or not isinstance(hens, dict):
                continue
            conn.execute("DELETE FROM eggs WHERE date=?", (date,))
            for hen, laid in hens.items():
                if laid and HEN_RE.match(str(hen)):
                    conn.execute(
                        "INSERT INTO eggs(date,hen,laid,updated_at) VALUES(?,?,1,?)",
                        (date, hen, ts))
                    rows += 1
    return len(eggs), rows


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not _token:
            return True
        return self.headers.get("X-Token", "") == _token

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._json(401, {"error": "bad token"}); return

        if path == "/health":
            self._json(200, {"ok": True, "version": VERSION, "rows": row_count()})
        elif path == "/eggs":
            days = None
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in qs.split("&"):
                if part.startswith("days="):
                    try:
                        days = max(1, min(int(part[5:]), 3650))
                    except ValueError:
                        pass
            self._json(200, {"eggs": get_eggs(days)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            self._json(401, {"error": "bad token"}); return
        path = self.path.split("?")[0]
        if path not in ("/eggs", "/eggs/bulk"):
            self._json(404, {"error": "not found"}); return
        try:
            n    = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n))
        except Exception:
            self._json(400, {"error": "bad json"}); return

        if path == "/eggs/bulk":
            eggs = data.get("eggs")
            if not isinstance(eggs, dict):
                self._json(400, {"error": "eggs must be an object"}); return
            try:
                days, rows = put_bulk(eggs)
            except Exception as e:
                self._json(500, {"error": str(e)}); return
            print(f"[eggs] bulk save: {days} days, {rows} eggs")
            self._json(200, {"ok": True, "days": days, "rows": rows})
            return

        date, hen, laid = data.get("date", ""), data.get("hen", ""), bool(data.get("laid"))
        if not DATE_RE.match(str(date)):
            self._json(400, {"error": "date must be YYYY-MM-DD"}); return
        if not HEN_RE.match(str(hen)):
            self._json(400, {"error": "bad hen id"}); return
        try:
            put_egg(date, hen, laid)
        except Exception as e:
            self._json(500, {"error": str(e)}); return
        print(f"[eggs] {date} {hen} laid={laid}")
        self._json(200, {"ok": True})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "eggs.db"))
    ap.add_argument("--token", default="", help="optional shared secret (X-Token header)")
    a = ap.parse_args()

    _db_path, _token = a.db, a.token
    init_db()
    print(f"[collector] v{VERSION} db={_db_path} rows={row_count()}")
    print(f"[collector] listening on 0.0.0.0:{a.port}"
          + ("  (token required)" if _token else "  (no token)"))
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
