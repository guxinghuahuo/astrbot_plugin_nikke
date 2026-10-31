import json
import sqlite3
import threading
from pathlib import Path

from .calendar_store import CalendarStore
from .config import Subscription
from .feed import Entry
from .image_fallback_store import ImageFallbackStore
from .translation_store import TranslationStore


class Store(CalendarStore, ImageFallbackStore, TranslationStore):
    """Transactional per-target outbox. Calls run in worker threads, never on the loop."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                source TEXT PRIMARY KEY, last_success REAL, last_error TEXT DEFAULT '',
                failures INTEGER NOT NULL DEFAULT 0, next_poll REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS relations (
                source TEXT NOT NULL, target TEXT NOT NULL, baseline REAL,
                PRIMARY KEY(source,target),
                FOREIGN KEY(source) REFERENCES sources(source) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                source TEXT NOT NULL, target TEXT NOT NULL, entry_id TEXT NOT NULL,
                payload TEXT NOT NULL, sort_ts REAL NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source,target,entry_id),
                FOREIGN KEY(source,target) REFERENCES relations(source,target) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS delivery_queue ON deliveries(status,next_try,sort_ts);
            CREATE TABLE IF NOT EXISTS filter_results (
                cache_key TEXT PRIMARY KEY, source TEXT NOT NULL,
                category TEXT NOT NULL, reason TEXT NOT NULL,
                FOREIGN KEY(source) REFERENCES sources(source) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS archive_entries (
                source TEXT NOT NULL, entry_id TEXT NOT NULL, payload TEXT NOT NULL,
                effective_time REAL NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                PRIMARY KEY(source,entry_id)
            );
            CREATE INDEX IF NOT EXISTS archive_recent ON archive_entries(source,effective_time);
            CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS jev_results (
                cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, choice TEXT NOT NULL,
                confidence REAL NOT NULL, model TEXT NOT NULL, created_at REAL NOT NULL,
                FOREIGN KEY(source) REFERENCES sources(source) ON DELETE CASCADE
            );
        """)
        if "sent_parts" not in {row[1] for row in self.db.execute("PRAGMA table_info(deliveries)")}:
            self.db.execute(
                "ALTER TABLE deliveries ADD COLUMN sent_parts TEXT NOT NULL DEFAULT '[]'"
            )
            self.db.commit()
        self._backfill_archive()
        self.init_calendar()
        self.init_image_fallback()
        self.init_translation()

    def _backfill_archive(self):
        """Recover available pre-archive bodies once; never alter delivery state."""
        with self.lock, self.db:
            if self.db.execute(
                "SELECT 1 FROM archive_meta WHERE key='delivery_backfill_v1'"
            ).fetchone():
                return
            for row in self.db.execute(
                "SELECT source,entry_id,payload,sort_ts FROM deliveries "
                "WHERE payload<>'{}' ORDER BY sort_ts"
            ).fetchall():
                try:
                    entry = Entry.from_dict(json.loads(row["payload"]))
                    if entry.id != row["entry_id"]:
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
                self._archive_entries(row["source"], [entry], row["sort_ts"], overwrite=False)
            self.db.execute("INSERT INTO archive_meta VALUES('delivery_backfill_v1')")

    def _archive_entries(self, source: str, entries: list[Entry], now: float, *, overwrite=True):
        for entry in entries:
            self.db.execute(
                """INSERT INTO archive_entries VALUES(?,?,?,?,?,?)
                ON CONFLICT(source,entry_id) DO UPDATE SET
                    payload=excluded.payload,last_seen=excluded.last_seen,
                    effective_time=COALESCE(?,archive_entries.first_seen)
                """
                if overwrite
                else "INSERT OR IGNORE INTO archive_entries VALUES(?,?,?,?,?,?)",
                (
                    source,
                    entry.id,
                    json.dumps(entry.to_dict(), ensure_ascii=False),
                    entry.published if entry.published is not None else now,
                    now,
                    now,
                    *([entry.published] if overwrite else []),
                ),
            )

    def archive(self, subscription: Subscription, entries: list[Entry], now: float):
        with self.lock, self.db:
            self._archive_entries(subscription.key, entries, now)

    def recent_entries(
        self, sources: tuple[str, ...], since: float, now: float, limit=50
    ) -> list[dict]:
        if not sources:
            return []
        with self.lock:
            placeholders = ",".join("?" for _ in sources)
            return [
                dict(row)
                for row in self.db.execute(
                    f"""SELECT source,payload,effective_time FROM archive_entries
                WHERE source IN ({placeholders}) AND effective_time>=? AND effective_time<=?
                ORDER BY effective_time DESC,entry_id DESC LIMIT ?""",
                    (*sources, since, now, limit),
                )
            ]

    def archive_status(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT count(*),min(effective_time),max(effective_time) "
                "FROM archive_entries WHERE source=?",
                (key,),
            ).fetchone()
            return {"count": row[0], "oldest": row[1], "newest": row[2]}

    def close(self):
        with self.lock:
            self.db.close()

    def sync(self, subscriptions: tuple[Subscription, ...]):
        """Disabled/deleted relations are removed; re-adding creates a fresh baseline."""
        desired = {(s.key, target) for s in subscriptions for target in s.targets}
        keys = {s.key for s in subscriptions}
        with self.lock, self.db:
            for row in self.db.execute("SELECT source,target FROM relations").fetchall():
                if (row["source"], row["target"]) not in desired:
                    self.db.execute("DELETE FROM relations WHERE source=? AND target=?", tuple(row))
            for row in self.db.execute("SELECT source FROM sources").fetchall():
                if row["source"] not in keys:
                    self.db.execute("DELETE FROM sources WHERE source=?", tuple(row))
            for source in keys:
                self.db.execute("INSERT OR IGNORE INTO sources(source) VALUES(?)", (source,))
            for source, target in desired:
                added = self.db.execute(
                    "INSERT OR IGNORE INTO relations(source,target) VALUES(?,?)", (source, target)
                ).rowcount
                if added:
                    self.db.execute("UPDATE sources SET next_poll=0 WHERE source=?", (source,))

    def source(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM sources WHERE source=?", (key,)).fetchone()
            return dict(row) if row else {}

    def ingest(self, subscription: Subscription, entries: list[Entry], now: float, interval: float):
        # Treat RSS as newest-first when publication time is absent.
        ordered = sorted(reversed(entries), key=lambda e: e.published or now)
        with self.lock, self.db:
            self._archive_entries(subscription.key, entries, now)
            for target in subscription.targets:
                row = self.db.execute(
                    "SELECT baseline FROM relations WHERE source=? AND target=?",
                    (subscription.key, target),
                ).fetchone()
                if row is None:
                    continue
                baseline = row["baseline"]
                for entry in ordered:
                    historic = baseline is None or (
                        entry.published is not None and entry.published <= baseline
                    )
                    self.db.execute(
                        """INSERT OR IGNORE INTO deliveries
                        (source,target,entry_id,payload,sort_ts,status) VALUES(?,?,?,?,?,?)""",
                        (
                            subscription.key,
                            target,
                            entry.id,
                            json.dumps(entry.to_dict(), ensure_ascii=False),
                            entry.published or now,
                            "seen" if historic else "pending",
                        ),
                    )
                if baseline is None:
                    self.db.execute(
                        "UPDATE relations SET baseline=? WHERE source=? AND target=?",
                        (now, subscription.key, target),
                    )
            self.db.execute(
                """UPDATE sources SET last_success=?,last_error='',failures=0,next_poll=?
                WHERE source=?""",
                (now, now + interval, subscription.key),
            )

    def fetch_failed(
        self,
        key: str,
        now: float,
        interval: int,
        error: str,
        jitter: float = 0,
    ):
        with self.lock, self.db:
            row = self.db.execute("SELECT failures FROM sources WHERE source=?", (key,)).fetchone()
            count = row[0] + 1
            base = min(3600, interval * 2 ** min(count - 1, 8))
            delay = max(60, min(3600, base + jitter))
            self.db.execute(
                "UPDATE sources SET failures=?,last_error=?,next_poll=? WHERE source=?",
                (count, error, now + delay, key),
            )

    def due(self, now: float) -> list[dict]:
        with self.lock:
            # Preserve order except for unsent posts backing off after invalid translation.
            # Such a post remains retryable but must not hold every later announcement.
            rows = self.db.execute(
                """
                SELECT d.rowid AS queue_id,d.* FROM deliveries d
                WHERE d.status='pending' AND d.next_try<=?
                AND NOT EXISTS (
                    SELECT 1 FROM deliveries older
                    WHERE older.source=d.source AND older.target=d.target
                    AND older.status='pending'
                    AND NOT (older.last_error LIKE '翻译校验失败：%'
                        AND older.next_try>? AND older.sent_parts='[]')
                    AND (older.sort_ts<d.sort_ts OR
                        (older.sort_ts=d.sort_ts AND older.rowid<d.rowid))
                ) ORDER BY d.sort_ts,d.rowid LIMIT 20
            """,
                (now, now),
            ).fetchall()
            return [dict(row) for row in rows]

    def delivered(self, queue_id: int):
        with self.lock, self.db:
            # Drop large payloads after delivery; retain identifiers for permanent deduplication.
            self.db.execute(
                "UPDATE deliveries SET status='sent',last_error='',payload='{}',sent_parts='[]' "
                "WHERE rowid=?",
                (queue_id,),
            )

    def filtered(self, queue_id: int):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE deliveries SET status='filtered',last_error='',"
                "payload='{}',sent_parts='[]' "
                "WHERE rowid=?",
                (queue_id,),
            )

    def part_sent(self, queue_id: int, part: str):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT sent_parts FROM deliveries WHERE rowid=?", (queue_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("投递记录不存在")
            parts = json.loads(row[0])
            if part not in parts:
                parts.append(part)
            self.db.execute(
                "UPDATE deliveries SET sent_parts=? WHERE rowid=?",
                (json.dumps(parts, ensure_ascii=False), queue_id),
            )

    def filter_result(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT category,reason FROM filter_results WHERE cache_key=?",
                (key,),
            ).fetchone()
            return dict(row) if row else None

    def jev_result(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT choice,confidence,model,created_at FROM jev_results WHERE cache_key=?",
                (key,),
            ).fetchone()
            return dict(row) if row else None

    def save_jev_result(
        self, key: str, source: str, choice: str, confidence: float, model: str, now: float
    ):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO jev_results VALUES(?,?,?,?,?,?)",
                (key, source, choice, confidence, model, now),
            )

    def save_filter_result(self, key: str, source: str, category: str, reason: str):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO filter_results VALUES(?,?,?,?)",
                (key, source, category, reason),
            )

    def delivery_failed(self, queue_id: int, now: float, error: str):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT attempts FROM deliveries WHERE rowid=?", (queue_id,)
            ).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            delay = min(3600, 60 * 2 ** min(attempts - 1, 8))
            self.db.execute(
                "UPDATE deliveries SET attempts=?,next_try=?,last_error=? WHERE rowid=?",
                (attempts, now + delay, error, queue_id),
            )

    def status(self, key: str) -> dict:
        with self.lock:
            result = self.source(key)
            result["pending"] = self.db.execute(
                "SELECT count(*) FROM deliveries WHERE source=? AND status='pending'", (key,)
            ).fetchone()[0]
            result["initialized"] = self.db.execute(
                "SELECT count(*) FROM relations WHERE source=? AND baseline IS NOT NULL", (key,)
            ).fetchone()[0]
            result["filtered"] = self.db.execute(
                "SELECT count(*) FROM deliveries WHERE source=? AND status='filtered'",
                (key,),
            ).fetchone()[0]
            row = self.db.execute(
                """SELECT last_error FROM deliveries WHERE source=? AND status='pending'
                AND last_error<>'' ORDER BY next_try DESC LIMIT 1""",
                (key,),
            ).fetchone()
            result["delivery_error"] = row[0] if row else ""
            return result
