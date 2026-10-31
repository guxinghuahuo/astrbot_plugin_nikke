"""Translation cache and reference-wait clocks, independent of raw archives."""

import json


class TranslationStore:
    def set_translation_media(self, queue_id, images):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT sent_parts FROM deliveries WHERE rowid=?", (queue_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("投递记录不存在")
            parts = [p for p in json.loads(row[0]) if not p.startswith("translation_media:")]
            if images is not None:
                parts.append("translation_media:" + json.dumps(images, ensure_ascii=False))
            self.db.execute(
                "UPDATE deliveries SET sent_parts=? WHERE rowid=?",
                (json.dumps(parts, ensure_ascii=False), queue_id),
            )

    def init_translation(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS translation_wait (
                source TEXT NOT NULL, entry_id TEXT NOT NULL, started REAL NOT NULL,
                PRIMARY KEY(source,entry_id)
            );
            CREATE TABLE IF NOT EXISTS translation_cache (
                cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
        """)

    def translation_started(self, source, entry_id, now):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO translation_wait VALUES(?,?,?)", (source, entry_id, now)
            )
            return self.db.execute(
                "SELECT started FROM translation_wait WHERE source=? AND entry_id=?",
                (source, entry_id),
            ).fetchone()[0]

    def translation_cached(self, key):
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM translation_cache WHERE cache_key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_translation(self, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO translation_cache VALUES(?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def archived_entry(self, source, entry_id):
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM archive_entries WHERE source=? AND entry_id=?",
                (source, entry_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def translation_pending(self, queue_id, next_try):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE deliveries SET next_try=?,last_error='等待同帖繁中参考' "
                "WHERE rowid=? AND status='pending'",
                (next_try, queue_id),
            )
