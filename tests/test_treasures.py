"""Favorite-item phases must follow source upgrade order, not skill-slot order."""

import asyncio
import copy
import hashlib
import json
from itertools import permutations
from unittest.mock import AsyncMock, Mock

import pytest

from nikke import characters as module
from nikke.characters import (
    LIST_PATH,
    TREASURE_LIST_PATH,
    TTL,
    CharacterCatalog,
    CharacterError,
    parse_skill_query,
    validate,
)

ITEM_PATH = "equip/zh-tw/favorite_200101.json"
ROLE_PATH = "roledata/72-v2-zh-tw.json"


@pytest.fixture
def sources():
    def skill(slot, enhanced=False):
        return {
            "name_localkey": f"{'強化' if enhanced else '基礎'}技能{slot}",
            "description_localkey": "攻擊力{description_value_01}%",
            "description_value_list": [
                {"description_value": [str(n + (100 if enhanced else 0)) for n in range(1, 11)]},
                {},
            ],
        }

    # Actual identity, phase order and grade layout of Diesel's public item;
    # descriptions/values are synthetic to make wrong levels/replacements visible.
    item = {
        "id": 200101, "name_code": 5020, "name_localkey": "玩具火車套組",
        "favorite_rare": "SSR", "grade": [1, 2, 3], "max_level": 2,
        "favoriteitem_skill_group_data": [
            {"skill_change_slot": slot, "info": skill(slot, True)} for slot in (2, 3, 1)
        ],
    }
    role = {
        "resource_id": 72, "name_code": 5020, "name_localkey": "迪塞爾",
        "skill1_detail": skill(1), "skill2_detail": skill(2),
        "ulti_skill_detail": {**skill(3), "skill_cooltime": 2000},
    }
    return {
        LIST_PATH: [{"resource_id": 72, "name_localkey": {"name": "迪塞爾"}}],
        ROLE_PATH: role, TREASURE_LIST_PATH: {"SSR": [200101]}, ITEM_PATH: item,
    }


@pytest.mark.parametrize(
    "query,expected",
    [("迪塞尔", ("迪塞尔", 10, 0)), ("迪塞尔 7", ("迪塞尔", 7, 0)),
     ("迪塞尔 珍藏品1", ("迪塞尔", 10, 1)),
     (" 迪塞爾 ７ 珍藏品３ ", ("迪塞爾", 7, 3)),
     ("产品 08 7 珍藏品2", ("产品 08", 7, 2))],
)
def test_explicit_skill_and_phase_arguments(query, expected):
    assert parse_skill_query(query) == expected


@pytest.mark.parametrize(
    "query",
    ["迪塞尔 珍藏品", "迪塞尔 珍藏品0", "迪塞尔 珍藏品4", "迪塞尔 珍藏品 3",
     "迪塞尔 珍藏品3 7", "迪塞尔 7 珍藏品1 珍藏品3", "迪塞尔珍藏品3",
     "珍藏品3", "迪塞尔 11 珍藏品3"],
)
def test_bad_phase_commands_fail_before_network(tmp_path, query):
    async def scenario():
        catalog = CharacterCatalog(tmp_path, Mock())
        catalog._download = AsyncMock()
        with pytest.raises(CharacterError):
            await catalog.query(query, skills=True)
        catalog._download.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("order", list(permutations((1, 2, 3))))
@pytest.mark.parametrize("phase", [0, 1, 2, 3])
def test_phases_replace_only_unlocked_slots_at_requested_level(tmp_path, sources, order, phase):
    async def scenario():
        item = sources[ITEM_PATH]
        groups = {g["skill_change_slot"]: g for g in item["favoriteitem_skill_group_data"]}
        item["favoriteitem_skill_group_data"] = [groups[slot] for slot in order]
        original = copy.deepcopy(sources)
        catalog = CharacterCatalog(tmp_path, Mock(), clock=lambda: 1000)
        catalog._download = AsyncMock(side_effect=lambda path, etag: (sources[path], '"v1"'))
        suffix = f" 珍藏品{phase}" if phase else ""
        pages = await catalog.query("迪塞尔 7" + suffix, skills=True)
        assert len(pages) == 3
        for slot, page in enumerate(pages, 1):
            enhanced = slot in order[:phase]
            assert f"{'强化' if enhanced else '基础'}技能{slot}" in page
            assert f"攻击力{107 if enhanced else 7}%" in page
            assert "Lv.7" in page
            if phase:
                assert f"珍藏品阶段{phase}" in page and "玩具火车套组" in page
                assert ("本技能已强化" if enhanced else "本阶段未强化") in page
                assert "未计珍藏品强化" not in page
        assert "冷却 20 秒" in pages[2]  # Preserve omitted base metadata.
        assert sources == original  # Never sort or modify shared source arrays.
        paths = [call.args[0] for call in catalog._download.await_args_list]
        assert paths == ([LIST_PATH, ROLE_PATH, TREASURE_LIST_PATH, ITEM_PATH] if phase
                         else [LIST_PATH, ROLE_PATH])

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [None, {}, {"SSR": [True]}, {"SSR": ["200101"]},
                                 {"SSR": [1, 1]}, {"SSR": [-1]}, {"SSR": list(range(1, 202))}])
def test_malformed_index_is_rejected(bad):
    with pytest.raises(CharacterError, match="索引"):
        validate(TREASURE_LIST_PATH, bad)


@pytest.mark.parametrize(
    "change",
    [lambda d: d.update(id=200201), lambda d: d.update(name_code=True),
     lambda d: d.update(favorite_rare="SR"), lambda d: d.update(grade=[1, 2]),
     lambda d: d.update(max_level=3), lambda d: d.update(favoriteitem_skill_group_data=[]),
     lambda d: d["favoriteitem_skill_group_data"][0].update(skill_change_slot=1),
     lambda d: d["favoriteitem_skill_group_data"][0].update(skill_change_slot=True),
     lambda d: d["favoriteitem_skill_group_data"][0].update(info=None),
     lambda d: d["favoriteitem_skill_group_data"][0]["info"].update(description_value_list=[])],
)
def test_malformed_items_do_not_enter_cache(sources, change):
    item = copy.deepcopy(sources[ITEM_PATH])
    change(item)
    with pytest.raises(CharacterError):
        validate(ITEM_PATH, item)


@pytest.mark.parametrize("stale_path", [TREASURE_LIST_PATH, ITEM_PATH])
def test_treasure_cache_restart_refresh_failure_and_recovery(tmp_path, sources, stale_path):
    async def scenario():
        now = [1000]
        first = CharacterCatalog(tmp_path, Mock(), clock=lambda: now[0])
        first._download = AsyncMock(side_effect=lambda path, etag: (sources[path], '"v1"'))
        await first.query("迪塞尔 珍藏品2", skills=True)
        restarted = CharacterCatalog(tmp_path, Mock(), clock=lambda: now[0])
        restarted._download = AsyncMock()
        await restarted.query("迪塞尔 珍藏品2", skills=True)
        restarted._download.assert_not_awaited()
        now[0] += TTL + 1

        async def download(path, etag):
            assert etag == '"v1"'
            if path == stale_path:
                raise CharacterError("HTTP 503")
            return None, etag

        restarted._download = AsyncMock(side_effect=download)
        pages = await restarted.query("迪塞尔 珍藏品2", skills=True)
        assert all("本地旧缓存" in p and "珍藏品阶段2" in p for p in pages)
        file = tmp_path / (hashlib.sha256(stale_path.encode()).hexdigest() + ".json")
        assert json.loads(file.read_text(encoding="utf8"))["checked"] == 1000
        now[0] += 301
        restarted._download = AsyncMock(return_value=(None, '"v1"'))
        assert "本地旧缓存" not in (await restarted.query("迪塞尔 珍藏品2", skills=True))[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["missing", "partial_failure", "duplicate", "unrelated_failure"])
def test_missing_or_incomplete_treasure_never_silently_becomes_base(tmp_path, sources, mode):
    async def scenario():
        if mode in ("missing", "partial_failure"):
            sources[ITEM_PATH]["name_code"] = 9999
        if mode != "missing":
            sources[TREASURE_LIST_PATH]["SSR"].append(200201)

        async def download(path, etag):
            if path.endswith("200201.json"):
                if mode != "duplicate":
                    raise CharacterError("unavailable")
                return {**sources[ITEM_PATH], "id": 200201}, ""
            return sources[path], ""

        catalog = CharacterCatalog(tmp_path, Mock())
        catalog._download = AsyncMock(side_effect=download)
        if mode == "unrelated_failure":
            assert "本技能已强化" in (await catalog.query("迪塞尔 珍藏品3", skills=True))[0]
        else:
            with pytest.raises(CharacterError, match={
                "missing": "未收录", "partial_failure": "暂时无法确认", "duplicate": "歧义"
            }[mode]):
                await catalog.query("迪塞尔 珍藏品3", skills=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["bounded", "cancel", "timeout"])
def test_bulk_lookup_limits_concurrency_and_cleans_up_cancelled_tasks(tmp_path, monkeypatch, mode):
    async def scenario():
        catalog = CharacterCatalog(tmp_path, Mock())
        active = peak = finished = 0
        started = asyncio.Event()

        async def load(path):
            nonlocal active, peak, finished
            if path == TREASURE_LIST_PATH:
                return {"SSR": list(range(1, 10))}, 1000, False
            active += 1
            peak = max(peak, active)
            started.set()
            try:
                await asyncio.sleep(0.001 if mode == "bounded" else 10)
                return {"name_code": 1}, 1000, False
            finally:
                active -= 1
                finished += 1

        catalog._load = load
        if mode == "timeout":
            monkeypatch.setattr(module, "TREASURE_TIMEOUT", 0.01)
        task = asyncio.create_task(catalog._treasure(999))
        await started.wait()
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(CharacterError, match="未收录" if mode == "bounded" else "超时"):
                await task
        assert active == 0 and peak <= 4 and finished > 0

    asyncio.run(scenario())
