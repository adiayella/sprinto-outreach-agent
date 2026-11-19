"""Storage: SQLite locally (one file per mode), or Postgres (Neon) when DATABASE_URL is set — one schema per mode.

On Vercel there is no lasting local disk, so DATABASE_URL is required there.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, mode TEXT, versions TEXT, label TEXT
);
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, domain TEXT, stage TEXT, step INTEGER DEFAULT 0,
  outcome TEXT DEFAULT '', outcome_reason TEXT DEFAULT '', busy INTEGER DEFAULT 0,
  data TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT, UNIQUE(run_id, domain)
);
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER, pid TEXT, url TEXT, ok INTEGER, status INTEGER,
  method TEXT, category TEXT, round INTEGER, reason TEXT, title TEXT, text TEXT, error TEXT, thin INTEGER,
  source_type TEXT DEFAULT 'company_site', extra TEXT DEFAULT '{}', fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER, eid TEXT, field TEXT, url TEXT, excerpt TEXT,
  source_type TEXT, category TEXT, retrieved_at TEXT, round INTEGER
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER, run_id INTEGER, ts TEXT, step INTEGER,
  kind TEXT, message TEXT, detail TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sends (
  op_id TEXT PRIMARY KEY, prospect_id INTEGER, status TEXT, mode TEXT, message_id TEXT, to_addr TEXT,
  subject TEXT, body TEXT, error TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS hunter_cache (domain TEXT PRIMARY KEY, ts TEXT, json TEXT);
"""
MIGRATIONS = [("evidence", "status TEXT DEFAULT 'verified'"), ("evidence", "flags TEXT DEFAULT '[]'"),
              ("prospects", "lease_until TEXT DEFAULT ''")]
LEASE_MINUTES = 6  # a crashed/timed-out worker's claim expires after this


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None, pg_url: str = "", schema: str = "public"):
        self.pg_url, self.schema, self.path = pg_url, schema, path
        self._local = threading.local()
        self._lock = threading.RLock()
        if pg_url:
            c = self.conn()
            c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            c.execute(f'SET search_path TO "{schema}"')
            for stmt in SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").split(";"):
                if stmt.strip():
                    c.execute(stmt)
            for table, col in MIGRATIONS:
                c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}")
            c.execute("CREATE TABLE IF NOT EXISTS public.app_state (key TEXT PRIMARY KEY, value TEXT)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.conn() as c:
                c.executescript(SCHEMA)
                c.execute("PRAGMA journal_mode=WAL")
                for table, col in MIGRATIONS:  # migration for older DBs
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                    except sqlite3.OperationalError:
                        pass

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is not None and self.pg_url and c.closed:
            c = None
        if c is None:
            if self.pg_url:
                import psycopg
                from psycopg.rows import dict_row
                c = psycopg.connect(self.pg_url, autocommit=True, row_factory=dict_row, connect_timeout=15)
                c.execute(f'SET search_path TO "{self.schema}"')
            else:
                c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg_url else sql

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(self._sql(sql), args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        """Execute a write; returns the number of rows affected."""
        with self._lock:
            c = self.conn()
            cur = c.execute(self._sql(sql), args)
            if not self.pg_url:
                c.commit()
            return cur.rowcount

    # ---- shared app state (Postgres only; e.g. the current mode across serverless instances) ---------------
    def get_state(self, key: str) -> str | None:
        if not self.pg_url:
            return None
        rows = self.q("SELECT value FROM public.app_state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_state(self, key: str, value: str) -> None:
        if self.pg_url:
            self.x("INSERT INTO public.app_state(key, value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                   (key, value))

    # ---- runs & prospects ----------------------------------------------------
    def create_run(self, mode: str, versions: dict, domains: list[str], label: str = "") -> int:
        rid = self.q("INSERT INTO runs(created_at, mode, versions, label) VALUES (?,?,?,?) RETURNING id",
                     (now(), mode, json.dumps(versions), label))[0]["id"]
        if not self.pg_url:
            self.conn().commit()
        for d in domains:
            self.x("INSERT INTO prospects(run_id, domain, stage, data, created_at, updated_at) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT DO NOTHING", (rid, d, "QUEUED", "{}", now(), now()))
        return rid

    def prospect(self, pid: int) -> dict:
        rows = self.q("SELECT * FROM prospects WHERE id=?", (pid,))
        if not rows:
            raise KeyError(pid)
        p = rows[0]
        p["data"] = json.loads(p["data"] or "{}")
        return p

    def save(self, p: dict, **fields) -> None:
        p.update(fields)
        self.x("UPDATE prospects SET stage=?, step=?, outcome=?, outcome_reason=?, data=?, updated_at=? WHERE id=?",
               (p["stage"], p["step"], p["outcome"], p["outcome_reason"], json.dumps(p["data"]), now(), p["id"]))

    def set_busy(self, pid: int, busy: bool) -> bool:
        """Lease: only one worker handles a prospect at a time; a crashed worker's lease expires."""
        if busy:
            until = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
            return self.x("UPDATE prospects SET busy=1, lease_until=? WHERE id=? AND "
                          "(busy=0 OR lease_until IS NULL OR lease_until='' OR lease_until<?)", (until, pid, now())) == 1
        return self.x("UPDATE prospects SET busy=0, lease_until='' WHERE id=?", (pid,)) == 1

    def log(self, p: dict, step: int, kind: str, message: str, **detail) -> None:
        self.x("INSERT INTO activity(prospect_id, run_id, ts, step, kind, message, detail) VALUES (?,?,?,?,?,?,?)",
               (p["id"], p["run_id"], now(), step, kind, message, json.dumps(detail, default=str)))

    # ---- pages & evidence ---------------------------------------------------
    def pages(self, pid: int, with_text: bool = True) -> list[dict]:
        cols = "*" if with_text else "id, pid, url, ok, status, method, category, round, reason, title, error, thin, source_type, extra, fetched_at, length(text) AS chars"
        rows = self.q(f"SELECT {cols} FROM pages WHERE prospect_id=? ORDER BY id", (pid,))
        for r in rows:
            r["extra"] = json.loads(r.get("extra") or "{}")
        return rows

    def add_page(self, pid: int, page: dict) -> None:
        self.x("""INSERT INTO pages(prospect_id, pid, url, ok, status, method, category, round, reason, title, text, error,
                  thin, source_type, extra, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (pid, page["pid"], page["url"], int(page["ok"]), page.get("status", 0), page.get("method", ""),
                page.get("category", ""), page.get("round", 0), page.get("reason", ""), page.get("title", ""),
                page.get("text", ""), page.get("error", ""), int(page.get("thin", False)),
                page.get("source_type", "company_site"), json.dumps(page.get("extra", {})), now()))

    def evidence(self, pid: int) -> list[dict]:
        rows = self.q("SELECT eid, field, url, excerpt, source_type, category, retrieved_at, round, status, flags "
                      "FROM evidence WHERE prospect_id=? ORDER BY id", (pid,))
        for r in rows:
            r["status"] = r.get("status") or "verified"
            r["flags"] = json.loads(r.get("flags") or "[]")
        return rows

    def add_evidence(self, pid: int, items: list[dict]) -> None:
        for e in items:
            self.x("INSERT INTO evidence(prospect_id, eid, field, url, excerpt, source_type, category, retrieved_at, round, "
                   "status, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (pid, e["eid"], e["field"], e["url"], e["excerpt"], e["source_type"], e["category"],
                    e["retrieved_at"], e["round"], e.get("status", "verified"), json.dumps(e.get("flags", []))))

    # ---- sends (operation ledger) -----------------------------------------
    def send_op(self, op_id: str) -> dict | None:
        rows = self.q("SELECT * FROM sends WHERE op_id=?", (op_id,))
        return rows[0] if rows else None

    def put_send(self, op_id: str, pid: int, **f) -> None:
        self.x("""INSERT INTO sends(op_id, prospect_id, status, mode, message_id, to_addr, subject, body, error, ts)
                  VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(op_id) DO UPDATE SET status=excluded.status,
                  message_id=excluded.message_id, error=excluded.error, ts=excluded.ts, subject=excluded.subject, body=excluded.body""",
               (op_id, pid, f.get("status"), f.get("mode"), f.get("message_id", ""), f.get("to", ""),
                f.get("subject", ""), f.get("body", ""), f.get("error", ""), now()))

    # ---- hunter cache ------------------------------------------------------
    def hunter_cached(self, domain: str) -> dict | None:
        rows = self.q("SELECT json FROM hunter_cache WHERE domain=?", (domain,))
        return json.loads(rows[0]["json"]) if rows else None

    def hunter_put(self, domain: str, data: dict) -> None:
        self.x("INSERT INTO hunter_cache(domain, ts, json) VALUES (?,?,?) ON CONFLICT (domain) DO UPDATE "
               "SET ts=excluded.ts, json=excluded.json", (domain, now(), json.dumps(data)))
