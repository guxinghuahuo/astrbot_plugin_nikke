"""Simplified calendar output covers fresh data and persisted pre-upgrade records."""

import asyncio
import hashlib
import json
import logging
from unittest.mock import AsyncMock

import pytest
from test_calendar import item, snapshot, ts

from nikke.calendar import normalise, reminder_pages
from nikke.calendar_service import CalendarService
from nikke.chinese import simplified
from nikke.config import CalendarSettings
from nikke.store import Store


@pytest.mark.parametrize(
    "traditional,expected",
    [
        ("應援 / 協同作戰", "应援 / 协同作战"),
        ("神祕學者 · 累積簽到活動", "神秘学者 · 累积签到活动"),
        ("貝洛塔：南瓜女巫", "贝洛塔：南瓜女巫"),
        ("頭髮與發送", "头发与发送"),
        ("NIKKE · NO CALLER ID / PASS", "NIKKE · NO CALLER ID / PASS"),
    ],
)
def test_local_phrase_conversion_preserves_english(traditional, expected):
    assert simplified(traditional) == expected


def test_conversion_keeps_raw_snapshot_and_legacy_fallback_identity():
    payload = snapshot()
    original = item(name="累積簽到活動")
    del original["banner"]  # Legacy fallback hashes the original title.
    payload["data"]["version_event"]["items"] = [original]
    before = json.dumps(payload, ensure_ascii=False)
    event = normalise(payload)[0]
    expected = hashlib.sha256(
        json.dumps(
            ["StoryEvent", int(original["start_time"]), "累積簽到活動"],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert event.id == expected
    assert event.title == "累积签到活动"
    assert json.dumps(payload, ensure_ascii=False) == before


def test_cached_traditional_listing_and_reminder_without_refetch_or_requeue(tmp_path):
    async def scenario():
        now = ts("2026-10-24 12:00:00")
        payload = snapshot()
        payload["data"]["version_event"]["items"] = [
            item(name="神祕學者", end="2026-10-25 12:00:00")
        ]
        events = normalise(payload)
        store = Store(tmp_path / "calendar.db")
        store.calendar_success(payload, events, now, 3600)
        # Simulate the SQLite payload saved by v0.1.13 before conversion existed.
        legacy = events[0].to_dict()
        legacy["title"] = "神祕學者"
        legacy["notice"] = "活動結束"
        with store.lock, store.db:
            store.db.execute(
                "UPDATE calendar_events SET payload=? WHERE event_id=?",
                (json.dumps(legacy, ensure_ascii=False), events[0].id),
            )
        settings = CalendarSettings(True, ("qq:GroupMessage:1",), quiet=False)
        store.calendar_plan(settings, now)
        due = store.calendar_due(settings, now)
        pages = reminder_pages(due, now, now)
        assert "神秘学者" in pages[0][0] and "活动结束" in pages[0][0]
        assert "神祕學者" not in pages[0][0]
        client, sender = AsyncMock(), AsyncMock()
        service = CalendarService(
            settings, store, sender, logging.getLogger("test"), clock=lambda: now, client=client
        )
        listing = await service.listing()
        assert "神秘学者" in listing and "活动结束" in listing
        client.fetch.assert_not_awaited()
        sender.assert_not_awaited()
        store.calendar_sent(pages[0][1])
        count = store.calendar_counts()
        store.calendar_success(payload, normalise(payload), now + 60, 3600)
        store.calendar_plan(settings, now + 60)
        assert store.calendar_counts() == count
        assert store.calendar_due(settings, now + 60) == []
        store.close()

    asyncio.run(scenario())
