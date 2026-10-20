"""SQLite storage: the single source of truth for runs, data and quota use.

Layout: a *run* is a frozen config; each execution of it is a *snapshot*
(the first collection, then every `refresh`). Containers and items belong to a
snapshot. Every page of items is committed together with the checkpoint that
follows it, so an interrupted collection resumes where it stopped.

One connection is shared by all threads behind a lock; WAL mode lets the
dashboard read while a collection writes from another process.
"""

import contextlib
import datetime as dt
import gzip
import json
import sqlite3
import threading
from pathlib import Path

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
  created_at TEXT NOT NULL, config TEXT NOT NULL, tool_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs,
  seq INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  message TEXT, UNIQUE (run_id, seq));
CREATE TABLE IF NOT EXISTS containers (
  snapshot_id INTEGER NOT NULL REFERENCES snapshots, container_id TEXT NOT NULL,
  position INTEGER NOT NULL, platform TEXT NOT NULL, availability TEXT NOT NULL,
  title TEXT, author_id TEXT, author_name TEXT, published_at TEXT,
  comment_count INTEGER, stats TEXT, resolved_at TEXT,
  phase TEXT NOT NULL DEFAULT 'pending', checkpoint TEXT, error TEXT,
  threads INTEGER NOT NULL DEFAULT 0, replies INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id, container_id));
CREATE TABLE IF NOT EXISTS items (
  snapshot_id INTEGER NOT NULL, item_id TEXT NOT NULL, container_id TEXT NOT NULL,
  thread_id TEXT NOT NULL, parent_id TEXT, depth INTEGER NOT NULL,
  author_key TEXT, author_id TEXT, author_name TEXT, text_raw TEXT NOT NULL,
  like_count INTEGER, reply_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT, collected_at TEXT NOT NULL,
  selected INTEGER NOT NULL DEFAULT 1, replies_done INTEGER NOT NULL DEFAULT 0,
  text_norm TEXT, text_clean TEXT, lang TEXT, lang_conf REAL, excluded_reason TEXT,
  extra TEXT, PRIMARY KEY (snapshot_id, item_id));
CREATE INDEX IF NOT EXISTS items_thread ON items (snapshot_id, thread_id);
CREATE INDEX IF NOT EXISTS items_container ON items (snapshot_id, container_id, depth);
CREATE TABLE IF NOT EXISTS raw (
  snapshot_id INTEGER NOT NULL, container_id TEXT, fetched_at TEXT NOT NULL, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  ts TEXT NOT NULL, platform TEXT NOT NULL, endpoint TEXT NOT NULL,
  units INTEGER NOT NULL, snapshot_id INTEGER);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger (platform, ts);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT NOT NULL, run_id INTEGER, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gone (
  snapshot_id INTEGER NOT NULL, item_id TEXT NOT NULL, container_id TEXT NOT NULL,
  prev_snapshot_id INTEGER NOT NULL, PRIMARY KEY (snapshot_id, item_id));
"""

ITEM_COLS = ("item_id", "container_id", "thread_id", "parent_id", "depth", "author_key",
             "author_id", "author_name", "text_raw", "like_count", "reply_count",
             "created_at", "updated_at", "collected_at", "selected", "extra",
             "text_norm", "text_clean", "lang", "lang_conf", "excluded_reason")
TERMINAL = ("done", "unavailable", "error")


def now() -> str:
    """Current UTC time as 'YYYY-MM-DDTHH:MM:SSZ'."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """Thread-safe access to the project database."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def tx(self):
        """Serializes access and wraps the block in one transaction."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def q(self, sql: str, args=()) -> list[dict]:
        """Runs a read query and returns rows as dicts."""
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    # -- runs and snapshots ----------------------------------------------------

    def create_run(self, cfg: Config, tool_version: str) -> int:
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO runs (name, platform, created_at, config, tool_version) VALUES (?,?,?,?,?)",
                (cfg.name, cfg.source, now(), json.dumps(cfg.to_dict()), tool_version))
            return cur.lastrowid

    def check_new_name(self, name: str) -> None:
        """Raises ValueError if a run with this name exists."""
        if self.q("SELECT 1 FROM runs WHERE name = ?", (name,)):
            raise ValueError(f"a run named {name!r} already exists: pick another name, "
                             "or resume/refresh the existing run")

    def run(self, ref: str | int) -> dict:
        """Looks a run up by name or numeric id.

        Raises:
            KeyError: If no such run exists.
        """
        rows = self.q("SELECT * FROM runs WHERE name = ? OR run_id = ?", (str(ref), str(ref)))
        if not rows:
            raise KeyError(f"no run named {ref!r}")
        return rows[0]

    def runs(self) -> list[dict]:
        return self.q("""
            SELECT r.run_id, r.name, r.platform, r.created_at, s.snapshot_id, s.seq,
                   s.status, s.started_at, s.finished_at, s.message,
                   (SELECT COALESCE(SUM(units), 0) FROM ledger l JOIN snapshots s2
                      ON l.snapshot_id = s2.snapshot_id WHERE s2.run_id = r.run_id) AS units
            FROM runs r LEFT JOIN snapshots s ON s.snapshot_id =
              (SELECT MAX(snapshot_id) FROM snapshots WHERE run_id = r.run_id)
            ORDER BY r.run_id DESC""")

    def new_snapshot(self, run_id: int) -> int:
        with self.tx() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM snapshots WHERE run_id = ?",
                             (run_id,)).fetchone()[0]
            return db.execute(
                "INSERT INTO snapshots (run_id, seq, status, started_at) VALUES (?,?,?,?)",
                (run_id, seq, "created", now())).lastrowid

    def snapshots(self, run_id: int) -> list[dict]:
        return self.q("""SELECT s.*, (SELECT COALESCE(SUM(units), 0) FROM ledger l
                           WHERE l.snapshot_id = s.snapshot_id) AS units
                         FROM snapshots s WHERE run_id = ? ORDER BY seq""", (run_id,))

    def snapshot(self, snapshot_id: int) -> dict:
        return self.q("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))[0]

    def set_snapshot(self, snapshot_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE snapshots SET {sets} WHERE snapshot_id = ?",
                       (*fields.values(), snapshot_id))

    # -- containers --------------------------------------------------------------

    def add_containers(self, snapshot_id: int, containers) -> None:
        ts = now()
        with self.tx() as db:
            for pos, c in enumerate(containers):
                phase = "pending" if c.availability == "ok" else "unavailable"
                db.execute(
                    """INSERT OR IGNORE INTO containers (snapshot_id, container_id, position,
                       platform, availability, title, author_id, author_name, published_at,
                       comment_count, stats, resolved_at, phase, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, c.container_id, pos, c.platform, c.availability, c.title,
                     c.author_id, c.author_name, c.published_at, c.comment_count,
                     json.dumps(c.stats), ts, phase,
                     None if c.availability == "ok" else c.availability))

    def containers(self, snapshot_id: int) -> list[dict]:
        return self.q("SELECT * FROM containers WHERE snapshot_id = ? ORDER BY position",
                      (snapshot_id,))

    def set_container(self, snapshot_id: int, container_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE containers SET {sets} WHERE snapshot_id = ? AND container_id = ?",
                       (*fields.values(), snapshot_id, container_id))

    # -- items -----------------------------------------------------------------

    def _upsert(self, db, snapshot_id: int, rows: list[dict]) -> None:
        cols = ", ".join(ITEM_COLS)
        marks = ", ".join("?" * (len(ITEM_COLS) + 1))
        upd = ", ".join(f"{c} = excluded.{c}" for c in ITEM_COLS if c not in ("item_id", "selected"))
        db.executemany(
            f"INSERT INTO items (snapshot_id, {cols}) VALUES ({marks}) "
            f"ON CONFLICT (snapshot_id, item_id) DO UPDATE SET {upd}",
            [(snapshot_id, *(r[c] for c in ITEM_COLS)) for r in rows])

    def _raw(self, db, snapshot_id, container_id, raw: list[dict]) -> None:
        ts = now()
        db.executemany("INSERT INTO raw VALUES (?,?,?,?)",
                       [(snapshot_id, container_id, ts, gzip.compress(json.dumps(r).encode()))
                        for r in raw])

    def _count(self, db, snapshot_id, container_id) -> None:
        db.execute("""UPDATE containers SET
            threads = (SELECT COUNT(*) FROM items WHERE snapshot_id = :sid AND container_id = :cid
                       AND depth = 0),
            replies = (SELECT COUNT(*) FROM items WHERE snapshot_id = :sid AND container_id = :cid
                       AND depth > 0 AND selected = 1)
            WHERE snapshot_id = :sid AND container_id = :cid""", {"sid": snapshot_id, "cid": container_id})

    def save_page(self, snapshot_id: int, container_id: str, rows: list[dict],
                  raw: list[dict] | None, checkpoint: dict | None) -> None:
        """Stores one page of items and the checkpoint after it, atomically.

        The last page (checkpoint None) also moves the container to the 'select'
        phase in the same transaction, so a crash can never restart a finished crawl.
        """
        with self.tx() as db:
            self._upsert(db, snapshot_id, rows)
            if raw:
                self._raw(db, snapshot_id, container_id, raw)
            db.execute("""UPDATE containers SET checkpoint = ?,
                          phase = CASE WHEN ? IS NULL THEN 'select' ELSE phase END
                          WHERE snapshot_id = ? AND container_id = ?""",
                       (json.dumps(checkpoint) if checkpoint else None,
                        json.dumps(checkpoint) if checkpoint else None, snapshot_id, container_id))
            self._count(db, snapshot_id, container_id)

    def select_oldest(self, snapshot_id: int, container_id: str, n: int) -> None:
        """Keeps only the n oldest threads (and their replies) as the sample."""
        with self.tx() as db:
            db.execute("""UPDATE items SET selected = (thread_id IN (
                SELECT item_id FROM items WHERE snapshot_id = :sid AND container_id = :cid AND depth = 0
                ORDER BY created_at, item_id LIMIT :n))
                WHERE snapshot_id = :sid AND container_id = :cid""",
                       {"sid": snapshot_id, "cid": container_id, "n": n if n else -1})

    def threads_needing_replies(self, snapshot_id: int, container_id: str,
                                max_replies: int) -> list[dict]:
        """Selected threads whose stored replies are fewer than min(reported, cap)."""
        return self.q("""
            SELECT t.item_id, t.reply_count FROM items t
            WHERE t.snapshot_id = :sid AND t.container_id = :cid AND t.depth = 0
              AND t.selected = 1 AND t.replies_done = 0
              AND MIN(t.reply_count, :n) > (SELECT COUNT(*) FROM items r
                    WHERE r.snapshot_id = :sid AND r.thread_id = t.item_id AND r.depth > 0)
            ORDER BY t.rowid""", {"sid": snapshot_id, "cid": container_id, "n": max_replies})

    def save_replies(self, snapshot_id: int, container_id: str, thread_id: str,
                     rows: list[dict], raw: list[dict] | None) -> None:
        """Stores a thread's full reply list; inline replies outside it leave the sample."""
        with self.tx() as db:
            self._upsert(db, snapshot_id, rows)
            ids = [r["item_id"] for r in rows]
            db.execute(f"""UPDATE items SET selected = 0 WHERE snapshot_id = ? AND thread_id = ?
                           AND depth > 0 AND item_id NOT IN ({','.join('?' * len(ids))})""",
                       (snapshot_id, thread_id, *ids))
            db.execute("UPDATE items SET replies_done = 1 WHERE snapshot_id = ? AND item_id = ?",
                       (snapshot_id, thread_id))
            if raw:
                self._raw(db, snapshot_id, container_id, raw)
            self._count(db, snapshot_id, container_id)

    def texts(self, snapshot_id: int, container_id: str | None = None) -> list[dict]:
        sql = "SELECT item_id, text_raw FROM items WHERE snapshot_id = ?"
        args = [snapshot_id]
        if container_id:
            sql += " AND container_id = ?"
            args.append(container_id)
        return self.q(sql, args)

    def save_processed(self, snapshot_id: int, rows: list[tuple]) -> None:
        """rows: (text_norm, text_clean, lang, lang_conf, excluded_reason, item_id)."""
        with self.tx() as db:
            db.executemany("""UPDATE items SET text_norm = ?, text_clean = ?, lang = ?,
                lang_conf = ?, excluded_reason = ? WHERE snapshot_id = ? AND item_id = ?""",
                           [(*r[:5], snapshot_id, r[5]) for r in rows])

    def report(self, snapshot_id: int) -> dict:
        """Exclusion report over the sample: counts per kind and reason.

        Returns:
            {"comments": {...}, "replies": {...}} where each dict maps
            "collected", "kept", "unprocessed" and every exclusion reason to a
            count; collected == kept + unprocessed + sum(reasons).
        """
        out = {}
        for kind, cond in (("comments", "depth = 0"), ("replies", "depth > 0")):
            rows = self.q(f"""SELECT CASE WHEN text_norm IS NULL THEN 'unprocessed'
                                  ELSE COALESCE(excluded_reason, 'kept') END AS k, COUNT(*) AS n
                              FROM items WHERE snapshot_id = ? AND selected = 1 AND {cond}
                              GROUP BY k""", (snapshot_id,))
            d = {r["k"]: r["n"] for r in rows}
            d["collected"] = sum(d.values())
            d.setdefault("kept", 0)
            out[kind] = d
        return out

    def coverage(self, snapshot_id: int, reply_cap: int, exhaustive: bool) -> dict:
        """Where the comments of each video went, from the platform's count to the kept set.

        For every video with a known platform count:
            on_platform = collected + not_sampled + over_reply_cap + not_returned
            collected   = kept + filtered_out
        where not_sampled covers threads outside the sample (beyond the comment cap
        or date window) and, on an exhaustive crawl, comments the platform counts
        but never returned. The identity is approximate: comments posted or deleted
        between the metadata check and the crawl shift not_sampled.

        Args:
            snapshot_id: Snapshot to describe.
            reply_cap: Replies requested per thread (0 if replies were not collected).
            exhaustive: True when every thread was requested (no cap, no date window),
                so that not_sampled can only mean "not returned by the API".

        Returns:
            {"videos": [per-video dict], "total": dict, "skipped": {status: n},
             "exhaustive": bool}.
        """
        threads = {r["container_id"]: r for r in self.q("""
            WITH got AS (SELECT thread_id, COUNT(*) AS n FROM items
                         WHERE snapshot_id = :s AND depth > 0 AND selected = 1 GROUP BY thread_id)
            SELECT t.container_id, COUNT(*) AS threads,
                   SUM(t.reply_count) AS replies_reported,
                   SUM(MAX(0, t.reply_count - :cap)) AS over_reply_cap,
                   SUM(MAX(0, MIN(t.reply_count, :cap) - COALESCE(g.n, 0))) AS not_returned
            FROM items t LEFT JOIN got g ON g.thread_id = t.item_id
            WHERE t.snapshot_id = :s AND t.depth = 0 AND t.selected = 1
            GROUP BY t.container_id""", {"s": snapshot_id, "cap": reply_cap})}
        kept = {r["container_id"]: r for r in self.q("""
            SELECT container_id, COUNT(*) AS collected,
                   SUM(excluded_reason IS NOT NULL) AS filtered_out
            FROM items WHERE snapshot_id = ? AND selected = 1 GROUP BY container_id""",
                                                     (snapshot_id,))}
        videos, skipped = [], {}
        for c in self.containers(snapshot_id):
            if c["phase"] == "unavailable":
                skipped[c["error"] or "unavailable"] = skipped.get(c["error"] or "unavailable", 0) + 1
                continue
            t, k = threads.get(c["container_id"], {}), kept.get(c["container_id"], {})
            collected, done = k.get("collected", 0), c["phase"] == "done"
            on = c["comment_count"]
            reported = (t.get("threads") or 0) + (t.get("replies_reported") or 0)
            videos.append({
                "container_id": c["container_id"], "title": c["title"], "done": done,
                "on_platform": on, "collected": collected,
                "kept": collected - (k.get("filtered_out") or 0),
                "filtered_out": k.get("filtered_out") or 0,
                "not_sampled": max(0, on - reported) if on is not None and done else None,
                "over_reply_cap": t.get("over_reply_cap") or 0,
                # replies are fetched after the comments: only final once the video is done
                "not_returned": (t.get("not_returned") or 0) if done else None})
        keys = ("on_platform", "collected", "kept", "filtered_out", "not_sampled",
                "over_reply_cap", "not_returned")
        final = [v for v in videos if v["done"] and v["on_platform"] is not None]
        total = {k: sum(v[k] for v in final) for k in keys}
        total["videos"] = len(final)
        return {"videos": videos, "total": total, "skipped": skipped, "exhaustive": exhaustive}

    # -- snapshot diffs ----------------------------------------------------------

    def missing_since(self, snapshot_id: int, prev_id: int) -> list[dict]:
        """Items sampled in prev_id that the new snapshot did not see."""
        return self.q("""SELECT p.item_id, p.container_id FROM items p
            WHERE p.snapshot_id = ? AND p.selected = 1 AND NOT EXISTS (
              SELECT 1 FROM items n WHERE n.snapshot_id = ? AND n.item_id = p.item_id)""",
                      (prev_id, snapshot_id))

    def mark_gone(self, snapshot_id: int, prev_id: int, rows: list[dict]) -> None:
        with self.tx() as db:
            db.executemany("INSERT OR IGNORE INTO gone VALUES (?,?,?,?)",
                           [(snapshot_id, r["item_id"], r["container_id"], prev_id) for r in rows])

    # -- quota ledger and events ---------------------------------------------------

    def charge(self, platform: str, endpoint: str, units: int, snapshot_id: int | None) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO ledger VALUES (?,?,?,?,?)",
                       (now(), platform, endpoint, units, snapshot_id))

    def units_since(self, platform: str, since: str) -> int:
        return self.q("SELECT COALESCE(SUM(units), 0) AS n FROM ledger WHERE platform = ? AND ts >= ?",
                      (platform, since))[0]["n"]

    def ledger_since(self, platform: str, since: str) -> list[dict]:
        """Ledger rows since a UTC timestamp, with the run each call was made for."""
        return self.q("""SELECT l.ts, l.endpoint, l.units, r.name AS run FROM ledger l
                         LEFT JOIN snapshots s ON s.snapshot_id = l.snapshot_id
                         LEFT JOIN runs r ON r.run_id = s.run_id
                         WHERE l.platform = ? AND l.ts >= ? ORDER BY l.ts""", (platform, since))

    def log(self, run_id: int | None, level: str, message: str) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?)", (now(), run_id, level, message))

    def events(self, run_id: int, limit: int = 50) -> list[dict]:
        return self.q("SELECT * FROM events WHERE run_id = ? ORDER BY rowid DESC LIMIT ?",
                      (run_id, limit))
