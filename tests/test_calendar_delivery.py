"""Persistent calendar reminders: quiet hours, restart, freshness and delivery isolation."""

import asyncio
import json
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

from test_calendar import item, snapshot, ts

from nikke.calendar import CalendarEvent, normalise, reminder_pages
from nikke.calendar_service import CalendarService
from nikke.config import CalendarSettings, Settings
from nikke.service import Service
from nikke.store import Store


def setup(
    tmp_path,
    *,
    now="2026-10-24 12:00:00",
    end="2026-10-25 12:00:00",
    quiet=False,
    groups=("qq:GroupMessage:1",),
):
    clock = [ts(now)]
    payload = snapshot()
    payload["data"]["version_event"]["items"] = [item(end=end)]
    events = normalise(payload)
    store = Store(tmp_path / "calendar.db")
    settings = CalendarSettings(True, groups, (24, 6), ("story",), quiet)
    client = AsyncMock()
    client.fetch.return_value = (payload, events)
    sender = AsyncMock(return_value=True)
    service = CalendarService(
        settings,
        store,
        sender,
        logging.getLogger("calendar-test"),
        clock=lambda: clock[0],
        random_uniform=lambda *_: 0,
        client=client,
    )
    return clock, payload, events, store, settings, client, sender, service


def test_tiers_restart_new_group_and_durable_dedup(tmp_path):
    async def scenario():
        clock, _, _, store, settings, client, sender, service = setup(tmp_path)
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == 1
        assert "1 天 0 小时" in sender.call_args.args[1]
        await service.deliver_once()
        assert sender.await_count == 1
        store.close()
        store = Store(tmp_path / "calendar.db")
        service = CalendarService(
            settings,
            store,
            sender,
            logging.getLogger("test"),
            clock=lambda: clock[0],
            client=client,
        )
        clock[0] = ts("2026-10-25 08:00:00")
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == 2
        assert "4 小时" in sender.call_args.args[1]
        # Newly added group gets only the closest overdue tier, not both 24h and 6h.
        service.settings = replace(settings, targets=settings.targets + ("qq:GroupMessage:2",))
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == 3
        assert sender.call_args.args[0] == "qq:GroupMessage:2"
        assert store.calendar_counts()["sent"] == 3
        assert store.calendar_counts()["skipped"] == 1
        store.close()

    asyncio.run(scenario())


def test_quiet_advance_actual_remaining_and_late_night_start(tmp_path):
    async def scenario():
        clock, _, _, store, _, _, sender, service = setup(
            tmp_path, now="2026-10-24 22:59:59", end="2026-10-25 09:00:00", quiet=True
        )
        await service.tick()
        # Already overdue 24h reminder is caught up once, then 6h at 23:00.
        await service.deliver_once()
        sender.reset_mock()
        clock[0] = ts("2026-10-24 23:00:00")
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == 1
        text = sender.call_args.args[1]
        assert "夜间提醒提前汇总" in text and "10 小时" in text
        clock[0] = ts("2026-10-25 03:00:00")
        await service.deliver_once()
        assert sender.await_count == 1
        store.close()

    asyncio.run(scenario())


def test_night_recovery_waits_until_eight_and_drops_expired(tmp_path):
    async def scenario():
        clock, payload, events, store, _, client, sender, service = setup(
            tmp_path, now="2026-10-25 01:00:00", end="2026-10-25 09:00:00", quiet=True
        )
        expired = replace(events[0], id="ends-at-seven", end=int(ts("2026-10-25 07:00:00")))
        client.fetch.return_value = (payload, events + [expired])
        await service.tick()
        await service.deliver_once()
        sender.assert_not_awaited()
        clock[0] = ts("2026-10-25 08:00:00")
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == 1
        assert "09:00:00" in sender.call_args.args[1]
        assert "07:00:00" not in sender.call_args.args[1]
        assert store.calendar_counts()["sent"] == 1
        store.close()

    asyncio.run(scenario())


def test_extensions_and_removal_invalidate_pending_rows(tmp_path):
    async def scenario():
        clock, payload, events, store, _, client, sender, service = setup(tmp_path)
        await service.tick()
        updated = replace(events[0], end=events[0].end + 2 * 86400)
        clock[0] += 1000  # Before sending, stale data must be refreshed.
        client.fetch.return_value = (payload, [updated])
        await service.deliver_once()
        sender.assert_not_awaited()
        assert store.calendar_counts().get("cancelled", 0) == 2
        clock[0] = updated.end - 6 * 3600
        client.fetch.return_value = (snapshot(), [])
        await service.tick()
        await service.deliver_once()
        sender.assert_not_awaited()
        assert store.calendar_events(clock[0]) == []
        store.close()

    asyncio.run(scenario())


def test_stale_snapshot_pause_failure_backoff_and_recovery(tmp_path):
    async def scenario():
        clock, _, _, store, _, client, sender, service = setup(
            tmp_path, now="2026-10-24 08:00:00", end="2026-10-25 12:00:00"
        )
        await service.tick()
        client.fetch.side_effect = RuntimeError("do-not-log-remote-secret")
        clock[0] = ts("2026-10-24 12:00:00")
        await service.deliver_once()
        assert sender.await_count == 1  # Four-hour old good data is still usable.
        assert "2026-10-24 08:00:00" in sender.call_args.args[1]
        calls = client.fetch.await_count
        await service.deliver_once()
        assert client.fetch.await_count == calls
        clock[0] = ts("2026-10-25 06:00:00")
        await service.deliver_once()
        assert sender.await_count == 1  # Old cache exceeds six hours.
        assert "secret" not in store.calendar_state()["last_error"]
        client.fetch.side_effect = None
        clock[0] += 3601
        await service.deliver_once()
        assert sender.await_count == 2
        store.close()

    asyncio.run(scenario())


def test_page_progress_failure_group_removal_and_retry(tmp_path):
    async def scenario():
        clock, payload, events, store, settings, client, sender, service = setup(tmp_path)
        many = [replace(events[0], id=f"event-{i:03}", title="长活动标题" * 25) for i in range(24)]
        client.fetch.return_value = (payload, many)
        await service.tick()
        await service.deliver_once()
        sent = store.calendar_counts()["sent"]
        assert 0 < sent < len(many)
        first_text = sender.call_args.args[1]
        sender.side_effect = RuntimeError("offline")
        await service.deliver_once()
        assert store.calendar_counts()["sent"] == sent
        sender.side_effect = None
        for _ in range(10):
            clock[0] += 31
            await service.deliver_once()
        assert store.calendar_counts()["sent"] == len(many)
        assert [call.args[1] for call in sender.call_args_list].count(first_text) == 1
        # No restored configuration should replay confirmed reminder pages.
        service.settings = replace(settings, targets=())
        await service.tick()
        count = sender.await_count
        service.settings = settings
        await service.tick()
        await service.deliver_once()
        assert sender.await_count == count
        store.close()

    asyncio.run(scenario())


def test_disabled_rss_calendar_lifecycle_and_shared_group_cooldown(tmp_path):
    async def scenario():
        clock, _, _, store, settings, _, _, _ = setup(tmp_path)
        sender = AsyncMock()
        rss = AsyncMock()
        service = Service(
            Settings(False, 300, False, (), calendar=settings),
            store,
            rss,
            sender,
            logging.getLogger("test"),
            clock=lambda: clock[0],
        )
        service.calendar.tick = AsyncMock()
        service.calendar.deliver_once = AsyncMock()
        await service.start()
        await asyncio.sleep(0)
        assert {task.get_name() for task in service.tasks} == {
            "nikke-calendar-poll",
            "nikke-calendar-send",
        }
        rss.fetch.assert_not_awaited()
        assert await service._send_calendar("qq:GroupMessage:1", "提醒")
        assert not await service._send_calendar("qq:GroupMessage:1", "限速")
        assert await service._send_calendar("qq:GroupMessage:2", "独立群")
        async with service.target_lock("qq:GroupMessage:3"):
            assert not await service._send_calendar("qq:GroupMessage:3", "群忙")
        assert sender.await_count == 2
        await service.close()
        assert service.tasks == []
        rss.close.assert_awaited_once()

    asyncio.run(scenario())


def test_disabled_and_category_changes_cancel_pending_and_expiry(tmp_path):
    _, payload, events, store, settings, _, _, _ = setup(tmp_path)
    now = ts("2026-10-24 12:00:00")
    store.calendar_success(payload, events, now, 3600)
    store.calendar_plan(settings, now)
    assert len(store.calendar_due(settings, now)) == 1
    store.calendar_plan(replace(settings, categories=()), now)
    assert store.calendar_counts()["cancelled"] == 2
    store.calendar_plan(settings, now)
    assert len(store.calendar_due(settings, now)) == 1
    store.calendar_plan(replace(settings, enabled=False), now)
    assert store.calendar_due(replace(settings, enabled=False), now) == []
    store.calendar_plan(settings, events[0].end)
    assert store.calendar_due(settings, events[0].end) == []
    store.close()


def test_reminder_content_is_bounded_and_pages_acknowledge_exact_rows():
    now = ts("2026-10-24 12:00:00")
    event = CalendarEvent(
        "event", "overclock", "Sub", "超频", int(now - 10), int(now + 3600), "Buff 重置"
    )
    rows = [
        {
            "rid": i,
            "event_id": str(i),
            "end": event.end,
            "early": True,
            "payload": json.dumps(event.to_dict()),
        }
        for i in range(90)
    ]
    pages = reminder_pages(rows, now, now)
    assert len(pages) > 1
    assert all(len(text) <= 3000 and "Buff 重置" in text for text, _ in pages)
    assert sorted(value for _, ids in pages for value in ids) == list(range(90))


def test_live_refresh_before_send_and_minute_planning_do_not_duplicate_requests(tmp_path):
    async def scenario():
        clock, _, _, store, _, client, _, service = setup(tmp_path)
        await asyncio.gather(service.tick(), service.tick())
        assert client.fetch.await_count == 1
        clock[0] += 901
        await asyncio.gather(service.refresh(before_send=True), service.refresh(before_send=True))
        assert client.fetch.await_count == 2
        assert store.calendar_state()["next_poll"] == clock[0] + 3600
        store.close()

    asyncio.run(scenario())


def test_next_group_is_revalidated_if_first_send_crosses_deadline(tmp_path):
    async def scenario():
        clock, _, events, store, _, _, sender, service = setup(
            tmp_path, now="2026-10-25 11:59:59", groups=("qq:GroupMessage:1", "qq:GroupMessage:2")
        )

        async def send(*args):
            clock[0] = events[0].end + 1
            return True

        sender.side_effect = send
        await service.tick()
        await service.deliver_once()
        sender.assert_awaited_once()
        store.close()

    asyncio.run(scenario())


def test_listing_without_push_and_status_stale_warning(tmp_path):
    async def scenario():
        clock, _, _, store, settings, client, sender, service = setup(tmp_path)
        service.settings = replace(settings, enabled=False)
        text = await service.listing()
        assert "剧情活动" in text and "北京时间" in text
        client.fetch.assert_awaited_once()
        sender.assert_not_awaited()
        clock[0] += 7 * 3600
        client.fetch.side_effect = RuntimeError("offline")
        text = await service.listing()
        assert "超过 6 小时" in text
        assert "日历最近成功刷新" in "\n".join(await service.status())
        store.close()

    asyncio.run(scenario())
