import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from nikke import characters as module
from nikke.characters import (
    LIST_PATH,
    TTL,
    CharacterCatalog,
    CharacterError,
    asset_url,
    clean_text,
    description,
    format_profile,
    parse_query,
)

ROLE_PATH = "roledata/16-v2-zh-tw.json"


@pytest.fixture
def reference():
    # Representative public zh-TW schema. Values are already scaled in skills;
    # integer weapon ratios use hundredths. No player/account data is involved.
    skill = {
        "name_localkey": "掌握戰況",
        "info_description_localkey": "技能1",
        "description_localkey": (
            "<在階段1使用：輔助部隊行動方針>\n"
            "<color=#00AEFF>【攻擊力{description_value_01}%▲】</color>"
            "<word_group=10031>冷卻時間</word_group>{description_value_02}秒"
        ),
        "description_value_list": [
            {
                "description_value": [
                    "5.34",
                    "5.58",
                    "5.82",
                    "6.05",
                    "6.29",
                    "6.53",
                    "6.77",
                    "7.01",
                    "7.24",
                    "7.48",
                ]
            },
            {"description_value": ["10"]},
            {},
        ],
    }
    role = {
        "resource_id": 16,
        "name_localkey": "拉毗：小紅帽",
        "original_rare": "SSR",
        "class": "Attacker",
        "corporation": "ELYSION",
        "use_burst_skill": "Step1",
        "element_details": [{"element_name_localekey": "燃燒"}],
        "character_level_hp_list": [13500, 14175],
        "character_level_attack_list": [400],
        "character_level_defence_list": [100],
        "shot_detail": {
            "name_localkey": "機槍",
            "description_localkey": "傷害{damage}% 核心{core_damage_rate}%",
            "damage": 557,
            "core_damage_rate": 20000,
            "max_ammo": 300,
            "reload_time": 250,
        },
        "skill1_detail": skill,
        "skill2_detail": copy.deepcopy(skill),
        "ulti_skill_detail": {**skill, "skill_cooltime": 4000},
    }
    rows = [
        {"resource_id": 16, "name_localkey": {"name": "拉毗：小紅帽"}},
        {"resource_id": 32, "name_localkey": {"name": "小紅帽"}},
    ]
    return rows, role


def test_public_paths_and_lossless_skill_rendering(reference):
    assert asset_url(LIST_PATH).endswith("/jz-26/ww-14/c4619ec83335bcfd7b23e43600520dc7.json")
    assert asset_url(ROLE_PATH).endswith("/gh-63/78500850d16bc195da282203c61ea37e.json")
    skill = reference[1]["skill1_detail"]
    assert "5.34%" in description(skill, 1)
    rendered = description(skill, 10)
    assert "7.48%" in rendered and "冷却时间10秒" in rendered
    assert "<在阶段1使用：辅助部队行动方针>" in rendered
    assert "<color" not in rendered and "word_group" not in rendered
    assert clean_text("攻擊傷害、攻擊力、最終、全爆裂時間") == "攻击伤害、攻击力、最终、全爆裂时间"
    profile = format_profile(reference[1])
    assert "Lv.1 体力：13500" in profile and "5.57% 核心200%" in profile
    assert "换弹时间：2.5 秒" in profile
    assert "爆裂阶段：Ⅰ / Ⅱ / Ⅲ" in format_profile({**reference[1], "use_burst_skill": "AllStep"})
    broken = {**skill, "description_value_list": []}
    with pytest.raises(CharacterError, match="无法准确"):
        description(broken, 10)


@pytest.mark.parametrize("query", ["", "x" * 101, "拉毗 0", "拉毗 11", "拉毗 -1"])
def test_bad_queries_never_fetch(tmp_path, query):
    async def scenario():
        catalog = CharacterCatalog(tmp_path, Mock())
        catalog._download = AsyncMock()
        with pytest.raises(CharacterError):
            await catalog.query(query, skills=True)
        catalog._download.assert_not_awaited()

    asyncio.run(scenario())


def test_levels_and_full_names():
    assert parse_query(" 拉毗：小紅帽 ７ ", True) == ("拉毗:小紅帽", 7)
    assert parse_query("2B", True) == ("2B", 10)
    assert parse_query("拉毗：小红帽", False)[1] == 10


def test_cache_restart_etag_stale_and_ambiguity(tmp_path, reference):
    async def scenario():
        now = [1000000]
        catalog = CharacterCatalog(tmp_path, Mock(), clock=lambda: now[0])
        rows, role = reference
        catalog._download = AsyncMock(side_effect=[(rows, '"list"'), (role, '"role"')])
        pages = await catalog.query("拉毗:小红帽 10", skills=True)
        assert len(pages) == 3 and "Lv.10" in pages[0] and "7.48%" in pages[0]
        assert "基础技能" in pages[0] and "冷却 40 秒" in pages[2]
        assert "资料校验" in pages[0] and "BlaBlaLink" in pages[0]
        # An exact name wins over the longer form that contains it.
        with pytest.raises(CharacterError, match="多个"):
            await catalog.query("红帽")
        with pytest.raises(CharacterError, match="未找到"):
            await catalog.query("完全不存在")
        assert catalog._download.await_count == 2
        restarted = CharacterCatalog(tmp_path, Mock(), clock=lambda: now[0])
        restarted._download = AsyncMock()
        await restarted.query("拉毗：小紅帽")
        restarted._download.assert_not_awaited()
        now[0] += TTL + 1
        restarted._download = AsyncMock(side_effect=[(None, '"list"'), (None, '"role"')])
        await restarted.query("拉毗：小红帽")
        assert restarted._download.await_args_list[0].args == (LIST_PATH, '"list"')
        now[0] += TTL + 1
        restarted._download = AsyncMock(side_effect=CharacterError("HTTP 503"))
        stale = await restarted.query("拉毗：小红帽")
        assert "本地旧缓存" in stale[0]
        await restarted.query("拉毗：小红帽")
        assert restarted._download.await_count == 2  # backoff per asset
        now[0] += 301
        restarted._download = AsyncMock(side_effect=[(rows, '"new"'), (role, '"new"')])
        assert "本地旧缓存" not in (await restarted.query("拉毗：小红帽"))[0]

    asyncio.run(scenario())


def test_bad_data_does_not_replace_cache_and_concurrent_queries_share_fetch(tmp_path, reference):
    async def scenario():
        catalog = CharacterCatalog(tmp_path, Mock())
        catalog._download = AsyncMock(side_effect=[(reference[0], ""), (reference[1], "")])
        await asyncio.gather(*(catalog.query("拉毗:小红帽") for _ in range(5)))
        assert catalog._download.await_count == 2
        files = list(tmp_path.glob("*.json"))
        for file in files:
            cached = json.loads(file.read_text(encoding="utf-8"))
            cached["checked"] = 0
            file.write_text(json.dumps(cached), encoding="utf-8")
        catalog._download = AsyncMock(return_value=({"wrong": "shape"}, ""))
        assert "本地旧缓存" in (await catalog.query("拉毗:小红帽"))[0]
        # Invalid network data never overwrites the last usable source snapshot.
        assert all(json.loads(f.read_text(encoding="utf-8"))["checked"] == 0 for f in files)

    asyncio.run(scenario())


def test_cache_write_failure_uses_injected_logger_and_returns_reference(tmp_path, reference):
    async def scenario():
        logger = Mock()
        catalog = CharacterCatalog(tmp_path, logger)
        catalog._download = AsyncMock(side_effect=[(reference[0], ""), (reference[1], "")])
        catalog._save = Mock(side_effect=OSError("private cache path"))
        pages = await catalog.query("拉毗：小红帽", skills=True)
        assert len(pages) == 3 and "Lv.10" in pages[0]
        assert logger.warning.call_count == 2
        logger.warning.assert_called_with("[NIKKE] 角色资料缓存写入失败")
        assert "private cache path" not in str(logger.mock_calls)

    asyncio.run(scenario())


def test_downloader_no_cookies_redirects_and_size_limit(tmp_path, monkeypatch, reference):
    async def scenario():
        seen = []

        async def handle(request):
            seen.append(dict(request.headers))
            if request.query.get("redirect"):
                raise web.HTTPFound("/other")
            if request.headers.get("If-None-Match"):
                return web.Response(status=304)
            return web.json_response(
                reference[0], headers={"ETag": '"v1"', "Set-Cookie": "test=secret"}
            )

        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
        monkeypatch.setattr(module, "asset_url", lambda path: url)
        catalog = CharacterCatalog(tmp_path, Mock())
        try:
            assert (await catalog._download(LIST_PATH, ""))[1] == '"v1"'
            assert await catalog._download(LIST_PATH, '"v1"') == (None, '"v1"')
            assert all("Cookie" not in headers for headers in seen)
            monkeypatch.setattr(module, "asset_url", lambda path: url + "?redirect=1")
            with pytest.raises(CharacterError, match="302"):
                await catalog._download(LIST_PATH, "")
            monkeypatch.setattr(module, "asset_url", lambda path: url)
            monkeypatch.setattr(module, "MAX_BYTES", 1)
            with pytest.raises(CharacterError, match="大小限制"):
                await catalog._download(LIST_PATH, "")
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
