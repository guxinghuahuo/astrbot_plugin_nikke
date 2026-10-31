import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nikke.classifier import ContentFilter, obvious_winner_notice, parse_decision, split_post
from nikke.config import Settings, Subscription
from nikke.feed import Entry, FeedError
from nikke.service import Service
from nikke.store import Store


def fixture_data(tmp_path):
    sub = Subscription("NIKKE", "1", "http://rss/1", ("qq:GroupMessage:1", "qq:GroupMessage:2"))
    settings = Settings(True, 300, True, (sub,), jitter=0, ai_filter_enabled=True)
    entry = Entry(
        "new", "抽奖活动", "转发并关注参加抽奖", "https://t.bilibili.com/1", "官方", 101, ()
    )
    db = Store(tmp_path / "state.db")
    db.sync((sub,))
    ctx = SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="model-1"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"category":"giveaway","reason":"纯转发抽奖"}',
            )
        ),
    )
    return sub, settings, entry, db, ctx


@pytest.mark.parametrize(
    "text",
    [
        "oops",
        "```json\n{}\n```",
        "[]",
        '{"category":"unknown","reason":"x"}',
        '{"category":"news","reason":false}',
        '{"category":["news"],"reason":"x"}',
    ],
)
def test_invalid_model_output_is_retryable(text):
    with pytest.raises(FeedError):
        parse_decision(text)


def test_cache_across_groups_restarts_and_model_changes(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        first, second = await asyncio.gather(
            content_filter.assess(sub, entry),
            content_filter.assess(sub, entry),
        )
        assert first.skip and first == second
        ctx.llm_generate.assert_awaited_once()
        kwargs = ctx.llm_generate.call_args.kwargs
        assert json.loads(kwargs["prompt"]) == {
            "title": entry.title,
            "current_text": entry.text,
            "quoted_text": "",
            "is_repost": False,
        }
        assert "GroupMessage" not in kwargs["prompt"]
        assert kwargs["contexts"] == [] and kwargs["tools"] is None
        db.close()
        db = Store(tmp_path / "state.db")
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        assert (await content_filter.assess(sub, entry)).skip
        assert ctx.llm_generate.await_count == 1
        # A changed provider or text cannot reuse a stale verdict.
        ctx.get_current_chat_provider_id.return_value = "model-2"
        await content_filter.assess(sub, entry)
        await content_filter.assess(sub, replace(entry, text="changed"))
        assert ctx.llm_generate.await_count == 3
        db.close()

    asyncio.run(scenario())


WINNER_TEXT = (
    "恭喜以下指挥官中奖，请中奖指挥官在10月30日24:00之前私信回复收货信息！ "
    "@Yurinou @像二哈一样的拿子 @去玩热武器 //转发自: @NIKKE_Official: \n"
    "▶关注@NIKKE_Official 并评论、转发本条动态，我们将于10月23日抽取3位指挥官"
    "送出【桃乐丝亚克力立牌】1份！\n"
    "大约一百年前，她们的故事拉开了序幕。被称为“胜利女神”的女神部队，"
    "以及继承了童话之名的她们。肩负着人类希望的她们，历经无数磨难，一路走到了今天。"
    "现在，就请翻开那本蒙尘已久的书页，重新追溯她们一路走过的历程吧。\n"
    "【NIKKE总集篇】朝圣者篇现已公开▶ https://www.bilibili.com/video/BV1GiJD6JEZP/\n"
    "图文地址：https://www.bilibili.com/opus/1259681171732692995"
)


def test_winner_repost_cannot_be_saved_by_quoted_game_news(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        entry = replace(entry, text=WINNER_TEXT, images=("https://i0.hdslb.com/image.jpg",))
        # This clear result notice must be filtered even if the provider is unavailable.
        ctx.get_current_chat_provider_id.side_effect = RuntimeError("offline")
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        client, sender = AsyncMock(), AsyncMock()
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("filter"),
            clock=lambda: 400,
            classifier=content_filter.assess,
        )
        db.ingest(sub, [], 100, 300)
        db.ingest(sub, [entry], 400, 300)
        await service.deliver_once()
        assert db.status(sub.key)["filtered"] == 2
        assert db.status(sub.key)["pending"] == 0
        sender.assert_not_awaited()
        client.image.assert_not_awaited()
        ctx.get_current_chat_provider_id.assert_not_awaited()
        ctx.llm_generate.assert_not_awaited()
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("marker", ["//转发自:", "// 转发自：", "转发自:"])
def test_repost_boundary(marker):
    assert split_post("新正文 " + marker + " @官方：旧正文 //转发自: 更早正文") == (
        "新正文",
        "@官方：旧正文 //转发自: 更早正文",
        True,
    )


@pytest.mark.parametrize(
    "text",
    [
        "新版本今日上线，转发抽立牌，中奖者请私信回复收货信息。",
        "游戏内奖励登录即可领取。",
        "恭喜以下指挥官未中奖，不要私信回复收货信息。",
        "恭喜大家解锁剧情，台词为“恭喜中奖，请私信回复收货信息”。",
    ],
)
def test_news_and_negation_are_not_locally_filtered(text):
    assert not obvious_winner_notice(text)


def test_only_quoted_winner_notice_does_not_override_new_news(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        ctx.llm_generate.return_value.completion_text = (
            '{"category":"news","reason":"本次是版本更新"}'
        )
        entry = replace(entry, text="新版本维护公告：明日更新角色。 //转发自: " + WINNER_TEXT)
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        decision = await content_filter.assess(sub, entry)
        assert not decision.skip
        payload = json.loads(ctx.llm_generate.call_args.kwargs["prompt"])
        assert payload["current_text"] == "新版本维护公告：明日更新角色。"
        assert payload["quoted_text"] == WINNER_TEXT
        assert payload["is_repost"]
        db.close()

    asyncio.run(scenario())


def test_model_winner_category_and_policy_cache_invalidation(tmp_path, monkeypatch):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        entry = replace(entry, text="名单揭晓，请三位幸运玩家联系官号。 //转发自: 新角色发布")
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        monkeypatch.setattr("nikke.classifier.POLICY_VERSION", "nikke-content-v1")
        ctx.llm_generate.return_value.completion_text = (
            '{"category":"news","reason":"旧规则看到引用资讯"}'
        )
        assert not (await content_filter.assess(sub, entry)).skip
        monkeypatch.setattr("nikke.classifier.POLICY_VERSION", "nikke-content-v2")
        ctx.llm_generate.return_value.completion_text = (
            '{"category":"giveaway_result","reason":"本次公布中奖名单"}'
        )
        assert (await content_filter.assess(sub, entry)).skip
        assert ctx.llm_generate.await_count == 2
        assert (await content_filter.assess(sub, entry)).skip
        assert ctx.llm_generate.await_count == 2
        db.close()

    asyncio.run(scenario())


def test_disabled_filter_also_disables_local_winner_rule(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        content_filter = ContentFilter(
            ctx, replace(settings, ai_filter_enabled=False), db, logging.getLogger("filter")
        )
        assert not (await content_filter.assess(sub, replace(entry, text=WINNER_TEXT))).skip
        ctx.llm_generate.assert_not_awaited()
        db.close()

    asyncio.run(scenario())


def test_disabled_filter_never_calls_model(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        content_filter = ContentFilter(
            ctx, replace(settings, ai_filter_enabled=False), db, logging.getLogger("filter")
        )
        assert not (await content_filter.assess(sub, entry)).skip
        ctx.get_current_chat_provider_id.assert_not_awaited()
        ctx.llm_generate.assert_not_awaited()
        db.close()

    asyncio.run(scenario())


def test_explicit_provider_timeout_and_unconfigured_provider(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        settings = replace(settings, ai_provider_id="remote", ai_timeout=0.01)

        async def stalled_model(**kwargs):
            await asyncio.sleep(10)

        ctx.llm_generate.side_effect = stalled_model
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        with pytest.raises(FeedError, match="调用失败或超时"):
            await content_filter.assess(sub, entry)
        ctx.get_current_chat_provider_id.assert_not_awaited()
        assert db.db.execute("SELECT count(*) FROM filter_results").fetchone()[0] == 0
        ctx.get_current_chat_provider_id.side_effect = RuntimeError("not configured")
        content_filter = ContentFilter(
            ctx, replace(settings, ai_provider_id=""), db, logging.getLogger("filter")
        )
        with pytest.raises(FeedError, match="提供商配置"):
            await content_filter.assess(sub, entry)
        db.close()

    asyncio.run(scenario())


def test_failures_wait_retry_without_dropping_or_sending(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        now = [100.0]
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("filter"), clock=lambda: now[0]
        )
        client, sender = AsyncMock(), AsyncMock()
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("filter"),
            clock=lambda: now[0],
            classifier=content_filter.assess,
        )
        db.ingest(sub, [], 100, 300)
        db.ingest(sub, [entry], 400, 300)
        now[0] = 400
        ctx.llm_generate.return_value.completion_text = "invalid"
        await service.deliver_once()
        assert db.status(sub.key)["pending"] == 2 and db.status(sub.key)["filtered"] == 0
        ctx.llm_generate.assert_awaited_once()  # No repeated request for the second group.
        sender.assert_not_awaited()
        client.image.assert_not_awaited()
        now[0] = 460
        ctx.llm_generate.return_value.completion_text = (
            '{"category":"news","reason":"版本更新附带抽奖，应保留"}'
        )
        await service.deliver_once()
        assert sender.await_count == 2 and db.status(sub.key)["pending"] == 0
        assert ctx.llm_generate.await_count == 2
        await service.close()

    asyncio.run(scenario())


def test_skipped_items_are_terminal_and_initial_history_is_not_classified(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        content_filter = ContentFilter(ctx, settings, db, logging.getLogger("filter"))
        client, sender = AsyncMock(), AsyncMock()
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("filter"),
            clock=lambda: 400,
            classifier=content_filter.assess,
        )
        db.ingest(sub, [replace(entry, id="old", published=50)], 100, 300)
        await service.deliver_once()
        ctx.llm_generate.assert_not_awaited()
        db.ingest(sub, [entry], 400, 300)
        await service.deliver_once()
        assert db.status(sub.key)["filtered"] == 2 and db.status(sub.key)["pending"] == 0
        sender.assert_not_awaited()
        client.image.assert_not_awaited()
        ctx.llm_generate.assert_awaited_once()
        db.ingest(sub, [entry], 700, 300)
        await service.deliver_once()
        ctx.llm_generate.assert_awaited_once()
        await service.close()

    asyncio.run(scenario())
