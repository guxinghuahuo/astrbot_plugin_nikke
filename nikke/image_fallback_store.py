"""A separate durable supplement queue; waiting for Bilibili never blocks new posts."""

import json


class ImageFallbackStore:
    def init_image_fallback(self):
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS image_fallbacks (
                    source TEXT NOT NULL, target TEXT NOT NULL, entry_id TEXT NOT NULL,
                    payload TEXT NOT NULL, manual INTEGER NOT NULL, created REAL NOT NULL,
                    next_try REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending', chosen TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '', policy TEXT NOT NULL DEFAULT '',
                    parts TEXT NOT NULL DEFAULT '[]', last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source,target,entry_id),
                    FOREIGN KEY(source,target) REFERENCES relations(source,target) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS image_fallback_due ON image_fallbacks(status,next_try);
                CREATE TABLE IF NOT EXISTS image_match_cache (
                    cache_key TEXT PRIMARY KEY, decision TEXT NOT NULL
                );
            """)

    def defer_images(self, sub, entry, target, now, queue_id=None):
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO image_fallbacks
                (source,target,entry_id,payload,manual,created,next_try)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,target,entry_id) DO UPDATE SET
                manual=MAX(image_fallbacks.manual,excluded.manual)""",
                (
                    sub.key,
                    target,
                    entry.id,
                    json.dumps(entry.to_dict(), ensure_ascii=False),
                    int(queue_id is None),
                    now,
                    now,
                ),
            )
            if queue_id is not None:
                status = self.db.execute(
                    "SELECT status FROM image_fallbacks WHERE source=? AND target=? AND entry_id=?",
                    (sub.key, target, entry.id),
                ).fetchone()[0]
                self.db.execute(
                    "UPDATE deliveries SET status=?,last_error='' WHERE rowid=?",
                    (
                        {
                            "sent": "sent",
                            "expired": "image_expired",
                            "cancelled": "image_cancelled",
                        }.get(status, "fallback"),
                        queue_id,
                    ),
                )

    def expire_image_jobs(self, now, max_wait_minutes):
        with self.lock, self.db:
            expired = self.db.execute(
                "SELECT source,target,entry_id FROM image_fallbacks "
                "WHERE status='pending' AND created<=?",
                (now - max_wait_minutes * 60,),
            ).fetchall()
            self.db.execute(
                "UPDATE image_fallbacks SET status='expired',last_error='等待补图已超时' "
                "WHERE status='pending' AND created<=?",
                (now - max_wait_minutes * 60,),
            )
            for row in expired:
                self.db.execute(
                    "UPDATE deliveries SET status='image_expired',last_error='等待补图已超时' "
                    "WHERE source=? AND target=? AND entry_id=? AND status='fallback'",
                    tuple(row),
                )
            return len(expired)

    def image_jobs(self, now):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT rowid AS job_id,* FROM image_fallbacks WHERE status='pending' "
                    "AND next_try<=? ORDER BY next_try,rowid LIMIT 50",
                    (now,),
                ).fetchall()
            ]

    def image_job_update(self, job_id, **fields):
        allowed = {
            "next_try",
            "attempts",
            "status",
            "chosen",
            "kind",
            "policy",
            "parts",
            "last_error",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid image job fields")
        with self.lock, self.db:
            self.db.execute(
                "UPDATE image_fallbacks SET "
                + ",".join(key + "=?" for key in fields)
                + " WHERE rowid=?",
                (*fields.values(), job_id),
            )

    def image_job_complete(self, job):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE image_fallbacks SET status='sent',last_error='' WHERE rowid=?",
                (job["job_id"],),
            )
            self.db.execute(
                "UPDATE deliveries SET status='sent',payload='{}',sent_parts='[]',last_error='' "
                "WHERE source=? AND target=? AND entry_id=? AND status='fallback'",
                (job["source"], job["target"], job["entry_id"]),
            )

    def image_job_count(self):
        with self.lock:
            return self.db.execute(
                "SELECT count(*) FROM image_fallbacks WHERE status='pending'"
            ).fetchone()[0]

    def image_match_result(self, key):
        with self.lock:
            row = self.db.execute(
                "SELECT decision FROM image_match_cache WHERE cache_key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_image_match(self, key, decision):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO image_match_cache VALUES(?,?)",
                (key, json.dumps(decision, ensure_ascii=False)),
            )
