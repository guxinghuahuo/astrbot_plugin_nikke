"""Official wire format, normalized identities, config and Beijing calendar boundaries."""

import asyncio
import copy
import json
from datetime import datetime

import pytest
from aiohttp import web

from nikke.calendar import (
    MODULES,
    SHANGHAI,
    CalendarClient,
    in_quiet,
    normalise,
    scheduled_at,
)
from nikke.config import Settings
from nikke.feed import FeedError


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp()


def snapshot():
    return {"code": 0, "data": {module: {"items": []} for module in MODULES}}


def item(
    kind="StoryEvent",
    *,
    name="剧情活动",
    banner="event1",
    start="2026-10-01 11:00:00",
    end="2026-10-25 22:59:59",
):
    return {
        "type": kind,
        "name": name,
        "banner": banner,
        "start_time": str(int(ts(start))),
        "end_time": str(int(ts(end))),
    }


def test_normalise_categories_phase_reset_dedup_and_stable_order():
    data = snapshot()
    data["data"]["version_event"]["items"] = [item(), item("LoginEvent"), item("EventQuest")]
    data["data"]["skin_gacha"]["items"] = [item("LoginEvent"), item("SeasonPass")]
    data["data"]["character_gacha"]["items"] = [
        {**item("PickupGachaEvent"), "characters": [{"name": "测试角色"}]}
    ]
    data["data"]["raid"]["items"] = [item("CooperationEvent")]
    data["data"]["simulation_room"]["items"] = [
        item("SimulationOverclockSeason", end="2026-12-15 03:59:59"),
        item("SimulationOverclockSeason_Sub_3"),
    ]
    data["data"]["arena"]["items"] = [
        item("ArenaChampionSeason"),
        item("ArenaChampionBetting64"),
        item("ArenaChampionBattle64"),
    ]
    events = normalise(data)
    assert len(events) == 9
    assert len([e for e in events if e.type == "LoginEvent"]) == 1
    assert next(e for e in events if e.category == "recruitment").title == "测试角色"
    assert {e.notice for e in events if e.category == "overclock"} == {"Buff 重置", "赛季结束"}
    for module in data["data"].values():
        module["items"].reverse()
    assert normalise(data) == events
    changed = copy.deepcopy(data)
    changed["data"]["version_event"]["items"][-1]["end_time"] = int(ts("2026-10-26 23:00:00"))
    before = next(e for e in events if e.type == "StoryEvent")
    after = next(e for e in normalise(changed) if e.type == "StoryEvent")
    assert before.id == after.id and before.end != after.end


def test_final_overclock_stage_is_season_end_without_a_duplicate_buff_reset():
    payload = snapshot()
    payload["data"]["simulation_room"]["items"] = [
        item("SimulationOverclockSeason", end="2026-12-15 03:59:59"),
        item(
            "SimulationOverclockSeason_Sub_5",
            start="2026-11-17 04:00:00",
            end="2026-12-01 03:59:59",
        ),
        item(
            "SimulationOverclockSeason_Sub_6",
            start="2026-12-01 04:00:00",
            end="2026-12-15 03:59:59",
        ),
    ]
    events = normalise(payload)
    assert len(events) == 2
    assert {event.notice for event in events} == {"赛季结束", "Buff 重置"}
    assert [event.notice for event in events if event.end == int(ts("2026-12-15 03:59:59"))] == [
        "赛季结束"
    ]


@pytest.mark.parametrize("value", [None, True, 123.5, "invalid", "-1", "99999999999999"])
def test_invalid_times_reject_entire_snapshot(value):
    payload = snapshot()
    payload["data"]["version_event"]["items"] = [{**item(), "end_time": value}]
    with pytest.raises(FeedError):
        normalise(payload)


def test_missing_module_invalid_range_conflict_and_real_empty():
    assert normalise(snapshot()) == []
    payload = snapshot()
    del payload["data"]["raid"]
    with pytest.raises(FeedError):
        normalise(payload)
    payload = snapshot()
    payload["data"]["version_event"]["items"] = [
        item(start="2026-10-26 00:00:00", end="2026-10-25 00:00:00")
    ]
    with pytest.raises(FeedError):
        normalise(payload)
    payload["data"]["version_event"]["items"] = [item(), item(end="2026-10-26 00:00:00")]
    with pytest.raises(FeedError):
        normalise(payload)


@pytest.mark.parametrize(
    "due,expected,early",
    [
        ("2026-11-01 00:00:00", "2026-10-31 23:00:00", True),
        ("2026-11-01 07:59:59", "2026-10-31 23:00:00", True),
        ("2026-11-01 08:00:00", "2026-11-01 08:00:00", False),
        ("2026-11-01 23:59:59", "2026-11-01 23:59:59", False),
    ],
)
def test_advance_midnight_month_boundary(due, expected, early):
    scheduled, advanced = scheduled_at(int(ts(due) + 6 * 3600), 6, True)
    assert scheduled == ts(expected) and advanced == early
    assert scheduled_at(int(ts(due) + 6 * 3600), 6, False) == (ts(due), False)


def test_config_defaults_and_group_resolver():
    settings = Settings.from_dict(
        {"calendar": {"enabled": True, "targets": ["123", "123"], "hours": ["6", "24", "6"]}},
        qq_platform_ids=("QQ",),
    )
    assert not settings.enabled and settings.calendar.enabled
    assert settings.calendar.targets == ("QQ:GroupMessage:123",)
    assert settings.calendar.hours == (24, 6) and settings.calendar.quiet
    assert "arena" not in settings.calendar.categories
    assert not Settings.from_dict({}).calendar.enabled
    assert in_quiet(ts("2026-10-24 00:00:00"), True)
    assert not in_quiet(ts("2026-10-24 08:00:00"), True)


@pytest.mark.parametrize(
    "change",
    [
        {"hours": []},
        {"hours": [0]},
        {"hours": [721]},
        {"hours": [True]},
        {"hours": [1.5]},
        {"hours": "24,6"},
        {"targets": "123"},
    ],
)
def test_invalid_calendar_configuration(change):
    with pytest.raises(ValueError):
        Settings.from_dict({"calendar": change})


def test_http_client_anonymous_request_and_bounded_errors(monkeypatch):
    async def scenario():
        mode = ["ok"]

        async def endpoint(request):
            assert await request.json() == {}
            assert "Cookie" not in request.headers and "Authorization" not in request.headers
            common = json.loads(request.headers["x-common-params"])
            assert common["area_id"] == "global" and common["language"] == "zh-TW"
            assert request.headers["x-language"] == "zh-TW"
            if mode[0] == "http":
                return web.Response(status=503, text="private-response")
            if mode[0] == "large":
                return web.Response(body=b"x" * (4 * 1024 * 1024 + 1))
            if mode[0] == "bad":
                return web.json_response({"code": 0, "data": {}})
            return web.json_response(snapshot())

        app = web.Application()
        app.router.add_post("/calendar", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr("nikke.calendar.CALENDAR_URL", f"http://127.0.0.1:{port}/calendar")
        try:
            payload, events = await CalendarClient().fetch()
            assert payload == snapshot() and events == []
            for bad in ("http", "large", "bad"):
                mode[0] = bad
                with pytest.raises(FeedError) as error:
                    await CalendarClient().fetch()
                assert "private-response" not in str(error.value)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
