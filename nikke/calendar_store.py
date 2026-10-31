"""Additive SQLite persistence for the official calendar and per-group reminders."""

import json

from .calendar import in_quiet, scheduled_at


class CalendarStore:
    """Mixin sharing Store's connection, RLock and transactions."""

    def init_calendar(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                payload TEXT NOT NULL DEFAULT '{}',
                updated REAL, next_poll REAL NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO calendar_state(singleton) VALUES(1);
            CREATE TABLE IF NOT EXISTS calendar_events (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, start INTEGER NOT NULL,
                end INTEGER NOT NULL, category TEXT NOT NULL, active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calendar_reminders (
                event_id TEXT NOT NULL, end INTEGER NOT NULL, target TEXT NOT NULL,
                hours INTEGER NOT NULL, scheduled REAL NOT NULL, early INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(event_id,end,target,hours)
            );
            CREATE INDEX IF NOT EXISTS calendar_due
                ON calendar_reminders(status,scheduled,next_try);
        """)

    def calendar_state(self) -> dict:
        with self.lock:
            return dict(
                self.db.execute("SELECT * FROM calendar_state WHERE singleton=1").fetchone()
            )

    def calendar_success(self, payload, events, now: float, interval: float):
        with self.lock, self.db:
            self.db.execute("UPDATE calendar_events SET active=0")
            for event in events:
                self.db.execute(
                    """INSERT INTO calendar_events VALUES(?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET payload=excluded.payload,
                    start=excluded.start,end=excluded.end,category=excluded.category,
                    active=excluded.active""",
                    (
                        event.id,
                        json.dumps(event.to_dict(), ensure_ascii=False),
                        event.start,
                        event.end,
                        event.category,
                        int(event.end > now),
                    ),
                )
            self.db.execute(
                """UPDATE calendar_state SET payload=?,updated=?,next_poll=?,retry_at=0,
                failures=0,last_error='' WHERE singleton=1""",
                (json.dumps(payload, ensure_ascii=False), now, now + interval),
            )
            self.db.execute("""UPDATE calendar_reminders SET status='cancelled'
                WHERE status='pending' AND NOT EXISTS (
                    SELECT 1 FROM calendar_events e WHERE e.event_id=calendar_reminders.event_id
                    AND e.end=calendar_reminders.end AND e.active=1)""")

    def calendar_failed(self, now: float, error: str):
        with self.lock, self.db:
            failures = self.db.execute("SELECT failures FROM calendar_state").fetchone()[0] + 1
            retry_at = now + min(3600, 60 * 2 ** min(failures - 1, 6))
            self.db.execute(
                """UPDATE calendar_state SET failures=?,retry_at=?,next_poll=?,last_error=?
                WHERE singleton=1""",
                (failures, retry_at, retry_at, error),
            )

    def calendar_events(self, now: float) -> list[dict]:
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload FROM calendar_events WHERE active=1 AND end>? "
                    "ORDER BY end,event_id",
                    (now,),
                )
            ]

    def calendar_plan(self, settings, now: float):
        """Reconcile configuration and collapse overdue tiers, including retries.

        Sent/skipped history is immutable. Cancelled work can be re-enabled when
        a group/category is restored, without replaying confirmed deliveries.
        """
        with self.lock, self.db:
            events = self.db.execute(
                "SELECT * FROM calendar_events WHERE active=1 AND end>?", (now,)
            ).fetchall()
            valid = {}
            if settings.enabled:
                for event in events:
                    if event["category"] not in settings.categories:
                        continue
                    for target in settings.targets:
                        for hours in settings.hours:
                            key = (event["event_id"], event["end"], target, hours)
                            due, early = scheduled_at(event["end"], hours, settings.quiet)
                            valid[key] = (due, early)
            for row in self.db.execute("SELECT * FROM calendar_reminders WHERE status='pending'"):
                key = (row["event_id"], row["end"], row["target"], row["hours"])
                if key not in valid:
                    self.db.execute(
                        """UPDATE calendar_reminders SET status='cancelled'
                        WHERE event_id=? AND end=? AND target=? AND hours=?""",
                        key,
                    )
            for key, (due, early) in valid.items():
                self.db.execute(
                    """INSERT INTO calendar_reminders
                    (event_id,end,target,hours,scheduled,early) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(event_id,end,target,hours) DO UPDATE SET
                    scheduled=excluded.scheduled,early=excluded.early,
                    status=CASE WHEN status='cancelled' THEN 'pending' ELSE status END""",
                    (*key, due, int(early)),
                )
            groups = {}
            for row in self.db.execute(
                """SELECT rowid AS rid,* FROM calendar_reminders
                    WHERE status='pending' AND scheduled<=? ORDER BY hours""",
                (now,),
            ):
                key = (row["event_id"], row["end"], row["target"])
                if key in groups:
                    self.db.execute(
                        "UPDATE calendar_reminders SET status='skipped' WHERE rowid=?",
                        (row["rid"],),
                    )
                else:
                    groups[key] = row["rid"]

    def calendar_due(self, settings, now: float) -> list[dict]:
        if not settings.enabled or in_quiet(now, settings.quiet):
            return []
        with self.lock:
            rows = self.db.execute(
                """SELECT r.rowid AS rid,r.*,e.payload,e.category
                FROM calendar_reminders r JOIN calendar_events e ON e.event_id=r.event_id
                AND e.end=r.end WHERE r.status='pending' AND r.scheduled<=? AND r.next_try<=?
                AND e.active=1 AND e.end>? ORDER BY e.end,r.event_id""",
                (now, now, now),
            ).fetchall()
            return [
                dict(row)
                for row in rows
                if row["target"] in settings.targets
                and row["category"] in settings.categories
                and row["hours"] in settings.hours
            ]

    def calendar_sent(self, ids: list[int]):
        with self.lock, self.db:
            self.db.executemany(
                "UPDATE calendar_reminders SET status='sent',last_error='' "
                "WHERE rowid=? AND status='pending'",
                [(value,) for value in ids],
            )

    def calendar_send_failed(self, ids: list[int], now: float, error: str):
        with self.lock, self.db:
            for value in ids:
                row = self.db.execute(
                    "SELECT attempts FROM calendar_reminders WHERE rowid=?", (value,)
                ).fetchone()
                if row:
                    delay = min(3600, 30 * 2 ** min(row[0], 7))
                    self.db.execute(
                        """UPDATE calendar_reminders SET attempts=attempts+1,
                        next_try=?,last_error=? WHERE rowid=? AND status='pending'""",
                        (now + delay, error, value),
                    )

    def calendar_counts(self) -> dict:
        with self.lock:
            return dict(
                self.db.execute(
                    "SELECT status,COUNT(*) FROM calendar_reminders GROUP BY status"
                ).fetchall()
            )
