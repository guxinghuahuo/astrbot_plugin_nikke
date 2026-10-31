import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from nikke.classifier import ContentFilter
from nikke.config import Settings, Subscription
from nikke.feed import Entry
from nikke.jev import JevError, JevResult, evaluate, parse_result
from nikke.service import Service
from nikke.store import Store


def data(tmp_path):
    sub = Subscription("官方", "1", "http://rss/1", ("qq:GroupMessage:1", "qq:GroupMessage:2"))
    settings = Settings(
        True,
        300,
        False,
        (sub,),
        ai_filter_enabled=True,
        jev_filter_enabled=True,
        jev_token="private-test-token",
    )
    entry = Entry("new", "角色更新", "明日更新新角色", "https://t.bilibili.com/1", "官方", 101, ())
    store = Store(tmp_path / "state.db")
    store.sync((sub,))
    ctx = SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="chat"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"category":"giveaway","reason":"大模型判断应过滤"}'
            )
        ),
    )
    return sub, settings, entry, store, ctx


@pytest.mark.parametrize(
    "choice,confidence,fastpass",
    [
        ("normal", 0.95, True),
        ("normal", 0.8, True),
        ("normal", 0.7999, False),
        ("lottery", 1.0, False),
        ("uncertain", 1.0, False),
    ],
)
def test_only_high_confidence_normal_bypasses_llm(tmp_path, choice, confidence, fastpass):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        image_url = "https://i0.hdslb.com/bfs/new_dyn/original.jpg"
        entry = replace(entry, images=(image_url,))
        jev = AsyncMock(return_value=JevResult(choice, confidence, "jev-test"))
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), jev_evaluator=jev
        )
        result = await content_filter.assess(sub, entry)
        assert result.skip != fastpass
        jev.assert_awaited_once()
        state = jev.call_args.args[1]
        assert set(state) == {"title", "current_text", "quoted_text", "is_repost"}
        assert image_url not in json.dumps(state)
        assert "GroupMessage" not in json.dumps(state) and settings.jev_token not in json.dumps(
            state
        )
        if fastpass:
            ctx.get_current_chat_provider_id.assert_not_awaited()
            ctx.llm_generate.assert_not_awaited()
        else:
            ctx.llm_generate.assert_awaited_once()
        db.close()

    asyncio.run(run())


def test_high_normal_shared_cache_and_actual_delivery(tmp_path):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        ctx.get_current_chat_provider_id.side_effect = RuntimeError("No chat provider")
        jev = AsyncMock(return_value=JevResult("normal", 0.99, "jev-test"))
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), jev_evaluator=jev
        )
        sender, client = AsyncMock(), AsyncMock()
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("jev"),
            clock=lambda: 400,
            classifier=content_filter.assess,
        )
        db.ingest(sub, [], 100, 300)
        db.ingest(sub, [entry], 400, 300)
        await service.deliver_once()
        assert sender.await_count == 2
        jev.assert_awaited_once()
        ctx.llm_generate.assert_not_awaited()
        assert db.status(sub.key)["pending"] == 0
        await service.close()

    asyncio.run(run())


def test_concurrent_group_assessments_share_one_jev_request(tmp_path):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        jev = AsyncMock(return_value=JevResult("normal", 0.99, "jev-test"))
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), jev_evaluator=jev
        )
        first, second = await asyncio.gather(
            content_filter.assess(sub, entry), content_filter.assess(sub, entry)
        )
        assert first == second and not first.skip
        jev.assert_awaited_once()
        ctx.llm_generate.assert_not_awaited()
        db.close()

    asyncio.run(run())


def test_cache_rethreshold_model_content_expiry_and_restart(tmp_path):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        now = [100.0]
        jev = AsyncMock(return_value=JevResult("normal", 0.95, "jev-test"))

        def classifier(cfg, store):
            return ContentFilter(
                ctx, cfg, store, logging.getLogger("jev"), clock=lambda: now[0], jev_evaluator=jev
            )

        assert not (await classifier(settings, db).assess(sub, entry)).skip
        db.close()
        db = Store(tmp_path / "state.db")
        assert (
            await classifier(replace(settings, jev_confidence_threshold=0.99), db).assess(
                sub, entry
            )
        ).skip
        assert not (await classifier(settings, db).assess(sub, entry)).skip
        jev.assert_awaited_once()
        ctx.llm_generate.assert_awaited_once()
        await classifier(replace(settings, jev_model="pinned"), db).assess(sub, entry)
        await classifier(settings, db).assess(sub, replace(entry, text="更正内容"))
        assert jev.await_count == 3
        now[0] += 86401
        await classifier(settings, db).assess(sub, entry)
        assert jev.await_count == 4
        db.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["error", "timeout"])
def test_jev_failures_fall_back_then_retry_cooldown_and_hide_token(tmp_path, caplog, mode):
    caplog.set_level(logging.INFO)

    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        settings = replace(settings, jev_timeout=0.01)
        now = [100.0]

        async def broken(*args):
            if mode == "timeout":
                await asyncio.sleep(1)
            raise RuntimeError(settings.jev_token)

        jev = AsyncMock(side_effect=broken)
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), clock=lambda: now[0], jev_evaluator=jev
        )
        assert (await content_filter.assess(sub, entry)).skip
        assert (await content_filter.assess(sub, replace(entry, id="second"))).skip
        assert jev.await_count == 1 and ctx.llm_generate.await_count == 2
        assert db.db.execute("select count(*) from jev_results").fetchone()[0] == 0
        now[0] += 61
        await content_filter.assess(sub, replace(entry, id="third"))
        assert jev.await_count == 2
        db.close()

    asyncio.run(run())
    assert "private-test-token" not in caplog.text
    assert "转交大模型" in caplog.text


def test_jev_and_llm_failure_keeps_delivery_pending(tmp_path):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        jev = AsyncMock(side_effect=JevError("unavailable"))
        ctx.llm_generate.return_value.completion_text = "invalid"
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), jev_evaluator=jev
        )
        sender, client = AsyncMock(), AsyncMock()
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("jev"),
            clock=lambda: 400,
            classifier=content_filter.assess,
        )
        db.ingest(sub, [], 100, 300)
        db.ingest(sub, [entry], 400, 300)
        await service.deliver_once()
        assert db.status(sub.key)["pending"] == 2 and db.status(sub.key)["filtered"] == 0
        sender.assert_not_awaited()
        client.image.assert_not_awaited()
        jev.assert_awaited_once()
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["master_off", "jev_off", "local_winner"])
def test_switches_and_winner_rule_run_before_jev(tmp_path, mode):
    async def run():
        sub, settings, entry, db, ctx = data(tmp_path)
        if mode == "master_off":
            settings = replace(settings, ai_filter_enabled=False)
        elif mode == "jev_off":
            settings = replace(settings, jev_filter_enabled=False)
        else:
            entry = replace(
                entry, text="恭喜以下指挥官中奖，请私信回复收货信息！ //转发自: 新角色公告"
            )
        jev = AsyncMock()
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("jev"), jev_evaluator=jev
        )
        result = await content_filter.assess(sub, entry)
        jev.assert_not_awaited()
        if mode == "jev_off":
            ctx.llm_generate.assert_awaited_once()
        else:
            ctx.llm_generate.assert_not_awaited()
        if mode == "local_winner":
            assert result.skip
        db.close()

    asyncio.run(run())


def response_data():
    return {
        "model": "jev-test",
        "answers": {
            "dynamic_type": {
                "type": "choice",
                "choice": "normal",
                "confidence": 0.95,
                "probabilities": {"normal": 0.98, "lottery": 0.01, "uncertain": 0.01},
            }
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"choice": "unknown"},
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": 1.1},
        {"confidence": "0.99"},
        {"type": "noul"},
        {"probabilities": None},
        {"probabilities": {"normal": 1}},
        {"probabilities": {"normal": 0.1, "lottery": 0.8, "uncertain": 0.1}},
        {"probabilities": {"normal": 1, "lottery": 1, "uncertain": 1}},
    ],
)
def test_invalid_api_result_is_never_a_fastpass(change):
    body = response_data()
    body["answers"]["dynamic_type"].update(change)
    with pytest.raises(JevError):
        parse_result(body)


def test_real_http_uses_official_wire_format_and_safe_error_handling(tmp_path, monkeypatch):
    async def run():
        _, settings, _, db, _ = data(tmp_path)
        db.close()
        custom_criteria = {
            "normal": "自定义正常条件",
            "lottery": "自定义垃圾条件",
            "uncertain": "自定义不确定条件",
        }
        settings = replace(
            settings,
            jev_instructions="自定义 Jev 提示词",
            jev_criteria=json.dumps(custom_criteria),
        )
        mode = ["ok"]
        received = []
        redirected = []

        async def endpoint(request):
            received.append(await request.json())
            assert request.headers["Authorization"] == "Bearer private-test-token"
            assert "Cookie" not in request.headers
            if mode[0] in ("401", "429", "503"):
                return web.Response(status=int(mode[0]), text="private-test-token")
            if mode[0] == "redirect":
                raise web.HTTPFound("/redirect_target")
            if mode[0] == "large":
                return web.Response(body=b"x" * 65537)
            if mode[0] == "invalid":
                return web.Response(text="not JSON private-test-token")
            return web.json_response(response_data())

        async def redirect_target(request):
            redirected.append(True)
            return web.json_response(response_data())

        app = web.Application()
        app.router.add_post("/v1/systemone", endpoint)
        app.router.add_get("/redirect_target", redirect_target)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr("nikke.jev.ENDPOINT", f"http://127.0.0.1:{port}/v1/systemone")
        try:
            result = await evaluate(settings, {"current_text": "新公告"})
            assert result == JevResult("normal", 0.95, "jev-test")
            assert received[0]["model"] == "jev-latest"
            assert set(received[0]) == {"model", "state", "questions"}
            assert received[0]["questions"]["dynamic_type"]["type"] == "choice"
            assert received[0]["questions"]["dynamic_type"]["instructions"] == "自定义 Jev 提示词"
            assert received[0]["questions"]["dynamic_type"]["criteria"] == custom_criteria
            assert set(received[0]["questions"]["dynamic_type"]["criteria"]) == {
                "normal",
                "lottery",
                "uncertain",
            }
            for name in ("401", "429", "503", "redirect", "large", "invalid"):
                mode[0] = name
                with pytest.raises(JevError) as error:
                    await evaluate(settings, {"current_text": "新公告"})
                assert settings.jev_token not in str(error.value)
            assert redirected == []
        finally:
            await runner.cleanup()

    asyncio.run(run())
