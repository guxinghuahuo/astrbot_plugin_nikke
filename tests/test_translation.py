import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nikke.config import OFFICIAL_OPENID, Settings, Subscription, TranslationSettings
from nikke.feed import Entry, FeedError
from nikke.service import Service
from nikke.store import Store
from nikke.translation import (
    ReferencePending,
    TranslationValidationError,
    Translator,
    parse_translation,
    thinking_parameters,
)

LOG = logging.getLogger("translation-test")
TARGET = "qq:GroupMessage:1"


def post(language="en", ident="9223372036854775807", **changes):
    return replace(
        Entry(
            f"blablalink:{OFFICIAL_OPENID}:{language}:{ident}",
            "Dorothy event",
            "Dorothy starts at 10:00 UTC+9. https://example.com/English?q=1",
            f"https://www.blablalink.com/post/detail?post_uuid={ident}&lang={language}",
            "Official",
            100,
            ("https://sg-cdn.blablalink.com/a.jpg",),
        ),
        **changes,
    )


def reference(ident="9223372036854775807"):
    return post(
        "zh-TW",
        ident,
        title="桃樂絲活動",
        text="桃樂絲 其他語言日期不同",
        images=("https://sg-cdn.blablalink.com/zh-TW.jpg",),
    )


def translation_result():
    return {
        "title": "桃乐丝活动",
        "text": "桃乐丝 10:00 UTC+9 开始。 https://example.com/English?q=1",
        "terms": [{"source": "Dorothy", "target": "桃乐丝"}],
    }


def fixture(tmp_path, *, wait=10, enabled=True, language="en"):
    sub = Subscription(
        "国际",
        OFFICIAL_OPENID,
        "http://rss/nikke-blablalink/official/" + language,
        (TARGET,),
        "blablalink",
        language,
    )
    cfg = Settings(True, 300, True, (sub,), translation=TranslationSettings(enabled, "model", wait))
    db = Store(tmp_path / "db.sqlite")
    db.sync((sub,))
    now = [1000.0]
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(translation_result())),
            )
        ]
    )
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    calls = []

    def with_options(**kwargs):
        calls.append(kwargs)
        return client

    provider = SimpleNamespace(
        get_model=lambda: "deepseek-v4-flash",
        client=SimpleNamespace(with_options=with_options, chat=client.chat),
        provider_config={"custom_extra_body": {"thinking": {"type": "enabled"}}},
    )
    context = SimpleNamespace(
        get_provider_by_id=lambda _: provider,
        get_current_chat_provider_id=AsyncMock(return_value="model"),
    )
    rss = SimpleNamespace(
        fetch=AsyncMock(return_value=[reference()]),
        image=AsyncMock(return_value=b"image"),
        close=AsyncMock(),
    )
    translator = Translator(context, cfg, db, rss, LOG, lambda: now[0])
    return SimpleNamespace(
        sub=sub,
        cfg=cfg,
        db=db,
        now=now,
        create=create,
        context=context,
        provider=provider,
        rss=rss,
        translator=translator,
        options=calls,
    )


@pytest.mark.parametrize("value", [-1, 1441, True, 1.5, "10", None])
def test_invalid_wait(value):
    with pytest.raises(ValueError):
        TranslationSettings.from_dict({"wait_minutes": value})


def test_configuration_and_schema_defaults():
    cfg = Settings.from_dict({})
    assert not cfg.translation.enabled and cfg.translation.wait_minutes == 10
    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())
    defaults = {k: v["default"] for k, v in schema["translation"]["items"].items()}
    assert TranslationSettings.from_dict(defaults) == cfg.translation
    assert Settings.from_dict({"translation": {"wait_minutes": 0}}).translation.wait_minutes == 0
    with pytest.raises(ValueError):
        Settings.from_dict({"translation": []})


@pytest.mark.parametrize(
    "model,mode,expected",
    [
        ("ppio/qwen/qwen3.8-flash", "auto", {"enable_thinking": False}),
        ("deepseek-v4-flash", "auto", {"thinking": {"type": "disabled"}}),
        ("other", "enable_thinking", {"enable_thinking": False}),
        ("deepseek-v4-flash", "enable_thinking", {"enable_thinking": False}),
    ],
)
def test_thinking_wire_parameters(model, mode, expected):
    assert thinking_parameters(model, mode) == expected


def test_unknown_model_allows_provider_default():
    assert thinking_parameters("unknown-reasoner", "auto") == {}


def test_exact_reference_terms_cache_and_original_unchanged(tmp_path):
    f = fixture(tmp_path)

    async def run():
        original = post()
        rendered = await f.translator.render(f.sub, original, TARGET)
        assert rendered.referenced and "桃乐丝" in rendered.entry.text
        assert original.title == "Dorothy event"
        assert rendered.entry.id == original.id and rendered.entry.images == reference().images
        assert original.images != rendered.entry.images
        assert (
            rendered.entry.link == original.link and rendered.entry.published == original.published
        )
        kwargs = f.create.call_args.kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert not kwargs["stream"] and "tools" not in kwargs and "image_urls" not in kwargs
        payload = json.loads(kwargs["messages"][1]["content"])
        assert payload["traditional_reference"]["title"] == "桃樂絲活動"
        assert f.provider.provider_config["custom_extra_body"]["thinking"]["type"] == "enabled"
        assert f.options == [{"max_retries": 0, "timeout": 60}]
        # Another group and a fresh translator reuse persisted translation, no reference request.
        new = Translator(f.context, f.cfg, f.db, f.rss, LOG, lambda: f.now[0])
        assert (await new.render(f.sub, original, "qq:GroupMessage:2")) == rendered
        assert f.create.await_count == 1 and f.rss.fetch.await_count == 1
        raw_ref = f.db.archived_entry(replace(f.sub, language="zh-TW").key, reference().id)
        assert raw_ref["title"] == "桃樂絲活動"

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_reference_wait_deadline_restart_and_other_ids(tmp_path):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = [reference("wrong-post")]
    result = translation_result()
    result["terms"] = []
    f.create.return_value.choices[0].message.content = json.dumps(result)

    async def run():
        with pytest.raises(ReferencePending) as error:
            await f.translator.render(f.sub, post(), TARGET)
        assert error.value.next_try == 1060 and f.create.await_count == 0
        f.now[0] = 1599
        new = Translator(f.context, f.cfg, f.db, f.rss, LOG, lambda: f.now[0])
        with pytest.raises(ReferencePending) as error:
            await new.render(f.sub, post(), TARGET)
        assert error.value.next_try == 1600
        f.now[0] = 1600
        value = await new.render(f.sub, post(), TARGET)
        assert not value.referenced
        payload = json.loads(f.create.call_args.kwargs["messages"][1]["content"])
        assert payload["traditional_reference"] is None
        assert f.create.await_count == 1

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_reference_arrives_on_next_minute(tmp_path):
    f = fixture(tmp_path)

    async def run():
        f.rss.fetch.return_value = []
        with pytest.raises(ReferencePending):
            await f.translator.render(f.sub, post(), TARGET)
        f.now[0] += 59
        with pytest.raises(ReferencePending):
            await f.translator.render(f.sub, post(), TARGET)
        assert f.rss.fetch.await_count == 1
        f.now[0] += 1
        f.rss.fetch.return_value = [reference()]
        assert (await f.translator.render(f.sub, post(), TARGET)).referenced

    try:
        asyncio.run(run())
    finally:
        f.db.close()


@pytest.mark.parametrize("failure", [True, False])
def test_reference_failure_or_zero_wait_translates_immediately(tmp_path, failure):
    f = fixture(tmp_path, wait=10 if failure else 0)
    f.rss.fetch.side_effect = TimeoutError("secret") if failure else None
    f.rss.fetch.return_value = []
    result = translation_result()
    result["terms"] = []
    f.create.return_value.choices[0].message.content = json.dumps(result)
    try:
        assert not asyncio.run(f.translator.render(f.sub, post(), TARGET)).referenced
        assert f.now[0] == 1000
    finally:
        f.db.close()


@pytest.mark.parametrize("language,enabled", [("en", False), ("zh-TW", True)])
def test_bypass_has_no_network_or_model_calls(tmp_path, language, enabled):
    f = fixture(tmp_path, language=language, enabled=enabled)
    try:
        assert asyncio.run(f.translator.render(f.sub, post(language), TARGET)) is None
        assert (
            asyncio.run(f.translator.render(replace(f.sub, source="bilibili"), post(), TARGET))
            is None
        )
        f.create.assert_not_awaited()
        f.rss.fetch.assert_not_awaited()
    finally:
        f.db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"text": "没有原链接"},
        {"text": "<think>分析</think> https://example.com/English?q=1"},
        {"title": ""},
        {"text": ""},
    ],
)
def test_invalid_translation_not_cached(change):
    value = {**translation_result(), **change}
    with pytest.raises(FeedError):
        parse_translation(json.dumps(value), post(), reference())


def test_official_title_typography_does_not_trigger_term_warning():
    original = post(title="勝利の女神：NIKKE", text="新たなピルグリムニケ")
    ref = reference()
    ref = replace(ref, title="勝利女神 : 妮姬", text="新朝聖者妮姬pick up！")
    value = {
        "title": "胜利女神：妮姬",
        "text": "新的朝圣者妮姬",
        "terms": [
            {"source": "勝利の女神：NIKKE", "target": "胜利女神：妮姬"},
            {"source": "ピルグリムニケ", "target": "朝圣者妮姬"},
        ],
    }
    warnings = []
    result = parse_translation(json.dumps(value), original, ref, warnings=warnings)
    assert not warnings
    assert result == {k: value[k] for k in ("title", "text")}


@pytest.mark.parametrize(
    "terms",
    [
        [{"source": "Dorothy", "target": "多萝西"}],
        [{"source": "Other", "target": "桃乐丝"}],
        [{"source": "Dorothy", "target": "其他语言"}],
        [None],
        None,
        "Dorothy",
        [{"source": "Dorothy"}],
    ],
)
def test_advisory_term_mismatches_do_not_block_valid_content(terms):
    value = {**translation_result(), "terms": terms}
    warnings = []
    result = parse_translation(json.dumps(value), post(), reference(), warnings=warnings)
    assert warnings
    assert result["text"] == value["text"]


def test_optional_metadata_and_json_fence_are_tolerated():
    value = translation_result()
    del value["terms"]
    value["note"] = "辅助元数据不会发送到群"
    result = parse_translation("```json\n" + json.dumps(value) + "\n```", post(), reference())
    assert set(result) == {"title", "text"}


def test_advisory_warning_is_cached_and_sends_text_and_images_once(tmp_path, caplog):
    f = fixture(tmp_path)
    value = {**translation_result(), "terms": [{"source": "Dorothy", "target": "多萝西"}]}
    f.create.return_value.choices[0].message.content = json.dumps(value)
    sender = AsyncMock()
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [post()], 1000, 300)
        await service.deliver_once()
        row = f.db.db.execute("SELECT * FROM deliveries").fetchone()
        assert row["status"] == "sent" and row["attempts"] == 0
        assert sender.await_count == 2
        assert value["text"] in sender.call_args_list[0].args[1]
        await f.translator.render(f.sub, post(), TARGET)
        assert f.create.await_count == 1
        assert caplog.text.count("翻译专名检查提示，不阻断推送") == 1

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_invalid_output_does_not_starve_later_post(tmp_path):
    f = fixture(tmp_path)
    sender = AsyncMock()
    first, second = post(), post(ident="later", published=101)

    async def translate(sub, item, target):
        if item.id == first.id:
            raise TranslationValidationError("翻译结果不是有效 JSON，保留任务重试")
        return await f.translator.render(sub, item, target)

    f.rss.fetch.return_value = [reference("later")]
    service = Service(f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=translate)

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [first, second], 1000, 300)
        await service.deliver_once()
        sender.assert_not_awaited()
        f.now[0] += 3
        await service.deliver_once()
        rows = list(f.db.db.execute("SELECT * FROM deliveries ORDER BY sort_ts"))
        assert [r["status"] for r in rows] == ["pending", "sent"]
        assert rows[0]["attempts"] == 1 and rows[0]["last_error"].startswith("翻译校验失败：")
        assert len(f.db.due(1060)) == 1

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_model_failure_retains_raw_queue_and_no_images_sent(tmp_path):
    f = fixture(tmp_path)
    f.create.side_effect = RuntimeError("api-key-secret")
    sender = AsyncMock()
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [post()], 1000, 300)
        await service.deliver_once()
        sender.assert_not_awaited()
        f.rss.image.assert_not_awaited()
        row = f.db.db.execute("SELECT * FROM deliveries").fetchone()
        assert row["status"] == "pending" and row["attempts"] == 1
        assert row["sent_parts"] == "[]" and "api-key-secret" not in row["last_error"]
        assert json.loads(row["payload"])["title"] == "Dorothy event"

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_wait_queue_then_translated_text_before_original_images(tmp_path):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = []
    sender = AsyncMock()
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [post()], 1000, 300)
        await service.deliver_once()
        sender.assert_not_awaited()
        pending = f.db.due(1060)[0]
        assert pending["attempts"] == 0 and pending["next_try"] == 1060
        f.now[0] = 1060
        f.rss.fetch.return_value = [reference()]
        await service.deliver_once()
        assert len(sender.await_args_list) == 2
        text = sender.await_args_list[0].args[1]
        assert "桃乐丝" in text and post().link in text
        assert "AI 简体译文" not in text and "繁中配图来源" not in text
        assert reference().link not in text
        assert sender.await_args_list[0].args[2] == []
        assert sender.await_args_list[1].args[1:] == ("", [b"image"])
        f.rss.image.assert_awaited_once_with(reference().images[0])
        assert f.db.status(f.sub.key)["pending"] == 0
        # Media-only retries never invoke the translator again.
        service.translator = AsyncMock(side_effect=AssertionError("must not translate"))
        await service.send_entry(f.sub, post(), TARGET, sent_parts={"text"})
        service.translator.assert_not_awaited()

    try:
        asyncio.run(run())
    finally:
        f.db.close()


@pytest.mark.parametrize(
    "mode,disable,custom,expected",
    [
        ("thinking", True, {}, {"thinking": {"type": "disabled"}}),
        ("enable_thinking", True, {}, {"enable_thinking": False}),
        (
            "custom",
            True,
            {"chat_template_kwargs": {"enable_thinking": False}},
            {"chat_template_kwargs": {"enable_thinking": False}},
        ),
        ("thinking", False, {"anything": False}, {}),
    ],
)
@pytest.mark.parametrize("reject", [False, True])
def test_actual_sdk_http_body_disables_thinking(tmp_path, mode, disable, custom, expected, reject):
    # A local HTTP endpoint inspects the serialized request, not just mock kwargs.
    openai = pytest.importorskip("openai")
    from aiohttp import web

    f = fixture(tmp_path)
    f.translator.settings = replace(
        f.cfg,
        translation=replace(
            f.cfg.translation,
            thinking_mode=mode,
            disable_thinking=disable,
            custom_parameters=custom,
        ),
    )
    packets = []

    async def handler(request):
        packets.append(await request.json())
        if reject and expected and len(packets) == 1:
            return web.json_response(
                {"error": {"message": "unsupported parameter: " + next(iter(expected))}},
                status=400,
            )
        return web.json_response(
            {
                "id": "local-only",
                "object": "chat.completion",
                "created": 1000,
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(translation_result()),
                        },
                    }
                ],
            }
        )

    async def run():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = openai.AsyncOpenAI(
            api_key="local-test",
            base_url=f"http://127.0.0.1:{port}/v1",
            http_client=openai.DefaultAsyncHttpxClient(trust_env=False),
        )
        f.provider.client = client
        try:
            assert (await f.translator.render(f.sub, post(), TARGET)).referenced
            assert len(packets) == (2 if reject and expected else 1)
            body = packets[-1]
            assert body["stream"] is False
            assert {
                k: v
                for k, v in body.items()
                if k not in ("model", "messages", "stream", "max_tokens")
            } == ({} if reject else expected)
            assert "extra_body" not in body and "tools" not in body
            assert f.provider.provider_config["custom_extra_body"]["thinking"]["type"] == "enabled"
        finally:
            await client.close()
            await runner.cleanup()

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_wait_survives_sqlite_close_and_reopen(tmp_path):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = []

    async def run():
        with pytest.raises(ReferencePending):
            await f.translator.render(f.sub, post(), TARGET)
        f.db.close()
        f.db = Store(tmp_path / "db.sqlite")
        f.now[0] = 1599
        translator = Translator(f.context, f.cfg, f.db, f.rss, LOG, lambda: f.now[0])
        with pytest.raises(ReferencePending) as error:
            await translator.render(f.sub, post(), TARGET)
        assert error.value.next_try == 1600

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_cached_reference_without_rss_and_current_group_model(tmp_path):
    f = fixture(tmp_path)
    f.db.archive(replace(f.sub, language="zh-TW"), [reference()], 999)
    f.translator.settings = replace(f.cfg, translation=replace(f.cfg.translation, provider=""))
    f.rss.fetch.side_effect = AssertionError("archive has exact reference")
    try:
        assert asyncio.run(f.translator.render(f.sub, post(), TARGET)).referenced
        f.context.get_current_chat_provider_id.assert_awaited_once_with(umo=TARGET)
        f.rss.fetch.assert_not_awaited()
    finally:
        f.db.close()


def test_manual_preview_wait_returns_without_sending_or_blocking_group(tmp_path):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = []
    sender = AsyncMock()
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )

    async def run():
        with pytest.raises(ReferencePending):
            await service.send_entry(f.sub, post(), TARGET)
        assert not service.target_lock(TARGET).locked()
        sender.assert_not_awaited()
        assert await service._send_supplement(TARGET, "calendar", [])

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_model_returning_thinking_uses_final_translation(tmp_path):
    f = fixture(tmp_path)
    f.create.return_value.choices[0].message.reasoning_content = "unexpected thoughts"
    try:
        result = asyncio.run(f.translator.render(f.sub, post(), TARGET))
        assert "桃乐丝" in result.entry.text and "unexpected thoughts" not in result.entry.text
        assert f.db.db.execute("SELECT count(*) FROM translation_cache").fetchone()[0] == 1
    finally:
        f.db.close()


def test_missing_translator_never_sends_raw_when_enabled(tmp_path):
    f = fixture(tmp_path)
    sender = AsyncMock()
    service = Service(f.cfg, f.db, f.rss, sender, LOG)
    try:
        with pytest.raises(FeedError, match="翻译器未初始化"):
            asyncio.run(service.send_entry(f.sub, post(), TARGET))
        sender.assert_not_awaited()
    finally:
        f.db.close()


@pytest.mark.parametrize("language", ["en", "ja", "ko"])
def test_chinese_image_selection_survives_retry_reload_and_switch_off(tmp_path, language):
    f = fixture(tmp_path, language=language)
    sender = AsyncMock()
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [post(language)], 1000, 300)
        f.rss.image.side_effect = TimeoutError()
        await service.deliver_once()
        assert sender.await_count == 1
        text = sender.call_args.args[1]
        assert post(language).link in text
        assert "AI 简体译文" not in text and reference().link not in text
        raw = f.db.archived_entry(f.sub.key, post(language).id)
        assert raw["images"] == list(post(language).images)
        pending = f.db.due(2000)[0]
        parts = json.loads(pending["sent_parts"])
        assert "text" in parts and any(p.startswith("translation_media:") for p in parts)
        f.db.close()
        f.db = Store(tmp_path / "db.sqlite")
        cfg = replace(f.cfg, translation=TranslationSettings(False))
        callback = AsyncMock(side_effect=AssertionError("already translated"))
        resumed = Service(
            cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=callback
        )
        f.now[0] = 2000
        f.rss.image.side_effect = None
        await resumed.deliver_once()
        callback.assert_not_awaited()
        assert sender.await_count == 2  # never repeats text
        assert f.rss.image.await_args_list[-1].args == reference().images
        assert all(call.args[0] not in post().images for call in f.rss.image.await_args_list)
        assert f.db.status(f.sub.key)["pending"] == 0

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_missing_reference_sends_translation_and_original_images(tmp_path):
    f = fixture(tmp_path, wait=0)
    f.rss.fetch.return_value = []
    value = translation_result()
    value["terms"] = []
    f.create.return_value.choices[0].message.content = json.dumps(value)
    sender = AsyncMock()
    service = Service(f.cfg, f.db, f.rss, sender, LOG, translator=f.translator.render)
    try:
        asyncio.run(service.send_entry(f.sub, post(), TARGET))
        assert sender.await_count == 2
        text = sender.await_args_list[0].args[1]
        assert post().link in text and "AI 简体译文" not in text and "未附配图" not in text
        assert sender.await_args_list[1].args[1:] == ("", [b"image"])
        f.rss.image.assert_awaited_once_with(post().images[0])
    finally:
        f.db.close()


def test_chinese_image_failure_hands_off_chinese_urls_to_fallback(tmp_path):
    f = fixture(tmp_path)
    cfg = replace(f.cfg, image_fallback=replace(f.cfg.image_fallback, enabled=True))
    f.rss.image.side_effect = TimeoutError()
    sender = AsyncMock()
    service = Service(
        cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=f.translator.render
    )
    try:
        assert asyncio.run(service.send_entry(f.sub, post(), TARGET)) is False
        job = f.db.image_jobs(1000)[0]
        payload = json.loads(job["payload"])
        assert payload["images"] == list(reference().images)
        assert payload["text"] == post().text and payload["id"] == post().id
        texts = "\n".join(call.args[1] for call in sender.await_args_list)
        assert post().images[0] in texts  # retain international original-image URLs
    finally:
        f.db.close()


@pytest.mark.parametrize(
    "parameters",
    [
        "[]",
        "null",
        "{bad",
        '{"enable_thinking":NaN}',
        '{"messages":[]}',
        '{"model":"other"}',
        '{"api_key":"secret"}',
        '{"tools":[]}',
        '{"stream":true}',
        {"": 1},
        42,
    ],
)
def test_custom_thinking_parameters_validation(parameters):
    with pytest.raises(ValueError):
        TranslationSettings.from_dict({"custom_parameters": parameters})


def test_custom_thinking_parameters_nested_json_and_overrides():
    options = TranslationSettings.from_dict(
        {
            "disable_thinking": False,
            "thinking_mode": "custom",
            "custom_parameters": '{"chat_template_kwargs":{"enable_thinking":false}}',
        }
    )
    assert not options.disable_thinking
    assert options.custom_parameters == {"chat_template_kwargs": {"enable_thinking": False}}
    assert (
        thinking_parameters("unknown", "auto", options.custom_parameters)
        == options.custom_parameters
    )
    assert thinking_parameters("qwen", "auto", {"enable_thinking": False, "budget": 0}) == {
        "enable_thinking": False,
        "budget": 0,
    }
    assert thinking_parameters("deepseek", "custom", {"my_flag": False}) == {"my_flag": False}
    with pytest.raises(ValueError):
        TranslationSettings.from_dict({"disable_thinking": "false"})


@pytest.mark.parametrize("disabled,model", [(False, "deepseek-v4-flash"), (True, "unknown")])
def test_disabled_switch_or_unknown_model_does_not_send_disable_flags(tmp_path, disabled, model):
    f = fixture(tmp_path)
    f.provider.get_model = lambda: model
    f.translator.settings = replace(
        f.cfg, translation=replace(f.cfg.translation, disable_thinking=disabled)
    )
    try:
        assert asyncio.run(f.translator.render(f.sub, post(), TARGET)).referenced
        assert f.create.call_args.kwargs["extra_body"] == {}
        assert "关闭思考" not in f.create.call_args.kwargs["messages"][0]["content"]
    finally:
        f.db.close()


class RejectedParameter(Exception):
    status_code = 400
    body = {"error": {"message": "unsupported parameter: thinking"}}


def test_unsupported_disable_flags_retry_once_and_remember_capability(tmp_path):
    f = fixture(tmp_path)
    response = f.create.return_value
    f.create.side_effect = [RejectedParameter("no secrets logged"), response, response]
    f.rss.fetch.return_value = [reference(), reference("second")]

    async def run():
        assert (await f.translator.render(f.sub, post(), TARGET)).referenced
        assert f.create.await_count == 2
        assert f.create.await_args_list[0].kwargs["extra_body"] == {
            "thinking": {"type": "disabled"}
        }
        assert f.create.await_args_list[1].kwargs["extra_body"] == {}
        assert (await f.translator.render(f.sub, post(ident="second"), TARGET)).referenced
        assert f.create.await_count == 3 and f.create.await_args_list[2].kwargs["extra_body"] == {}
        assert (await f.translator.render(f.sub, post(), TARGET)).referenced
        assert f.create.await_count == 3  # cache still matches the user's requested policy

    try:
        asyncio.run(run())
    finally:
        f.db.close()


@pytest.mark.parametrize(
    "status,body",
    [
        (401, "unsupported thinking"),
        (429, "unsupported thinking"),
        (500, "unsupported thinking"),
        (400, "invalid context length"),
    ],
)
def test_unrelated_request_failures_do_not_retry_as_thinking(tmp_path, status, body):
    f = fixture(tmp_path)
    failure = RejectedParameter("error")
    failure.status_code, failure.body = status, body
    f.create.side_effect = failure
    try:
        with pytest.raises(FeedError, match="翻译调用失败"):
            asyncio.run(f.translator.render(f.sub, post(), TARGET))
        assert f.create.await_count == 1 and not f.translator.unsupported
    finally:
        f.db.close()


def test_tagged_thinking_is_excluded_from_translation():
    text = "<think>private reasoning</think>\n" + json.dumps(translation_result())
    result = parse_translation(text, post(), reference())
    assert "桃乐丝" in result["text"] and "private reasoning" not in result["text"]


@pytest.mark.parametrize("disable", [True, False])
def test_native_provider_without_per_call_thinking_control_can_translate(tmp_path, disable):
    f = fixture(tmp_path)
    f.provider.client = object()
    f.translator.settings = replace(
        f.cfg, translation=replace(f.cfg.translation, disable_thinking=disable)
    )
    f.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text=json.dumps(translation_result()),
            reasoning_content="not sent",
        )
    )
    try:
        value = asyncio.run(f.translator.render(f.sub, post(), TARGET))
        assert "桃乐丝" in value.entry.text and value.entry.images == reference().images
        f.create.assert_not_awaited()
        f.context.llm_generate.assert_awaited_once()
        kwargs = f.context.llm_generate.call_args.kwargs
        assert kwargs["chat_provider_id"] == "model" and kwargs["tools"] is None
        assert "thinking" not in kwargs and "extra_body" not in kwargs
        assert not kwargs["contexts"]
    finally:
        f.db.close()


def test_translated_video_uses_same_post_chinese_youtube_cover_from_old_archive(tmp_path):
    f = fixture(tmp_path)
    foreign_url = "https://www.youtube.com/watch?v=r9rOkQcYzzU"
    chinese_url = "https://www.youtube.com/watch?v=abcdEFG_123"
    original = post(text="Dorothy " + foreign_url, images=())
    old_reference = reference()
    old_reference = replace(old_reference, text="桃樂絲 " + chinese_url, images=())
    f.db.archive(replace(f.sub, language="zh-TW"), [old_reference], 999)
    result = translation_result()
    result["text"] = "桃乐丝 " + foreign_url
    f.create.return_value.choices[0].message.content = json.dumps(result)
    try:
        value = asyncio.run(f.translator.render(f.sub, original, TARGET))
        assert value.entry.images == ("https://i.ytimg.com/vi_webp/abcdEFG_123/maxresdefault.webp",)
        assert foreign_url in value.entry.text
        f.rss.fetch.assert_not_awaited()
    finally:
        f.db.close()


def test_reference_without_images_keeps_original_images(tmp_path):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = [replace(reference(), images=())]
    try:
        value = asyncio.run(f.translator.render(f.sub, post(), TARGET))
        assert value.referenced and value.entry.images == post().images
    finally:
        f.db.close()


@pytest.mark.parametrize("referenced", [True, False])
def test_old_empty_media_translation_cache_is_repaired_without_new_model_call(tmp_path, referenced):
    f = fixture(tmp_path, wait=0)
    if not referenced:
        f.rss.fetch.return_value = []
        result = translation_result()
        result["terms"] = []
        f.create.return_value.choices[0].message.content = json.dumps(result)

    async def run():
        await f.translator.render(f.sub, post(), TARGET)
        row = f.db.db.execute("SELECT cache_key,payload FROM translation_cache").fetchone()
        cached = json.loads(row["payload"])
        cached["images"] = []  # simulate v0.1.28's persisted empty media selection
        f.db.save_translation(row["cache_key"], cached)
        f.create.side_effect = AssertionError("must reuse translation")
        f.rss.fetch.side_effect = AssertionError("must not wait for reference again")
        new = Translator(f.context, f.cfg, f.db, f.rss, LOG)
        value = await new.render(f.sub, post(), TARGET)
        assert value.entry.images == post().images and value.referenced == referenced
        assert f.db.translation_cached(row["cache_key"])["images"] == list(post().images)
        assert f.create.await_count == 1

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_pending_old_empty_media_snapshot_resumes_original_images_without_repeating_text(tmp_path):
    f = fixture(tmp_path)
    sender = AsyncMock()
    translator = AsyncMock(side_effect=AssertionError("text already sent"))
    service = Service(
        f.cfg, f.db, f.rss, sender, LOG, clock=lambda: f.now[0], translator=translator
    )

    async def run():
        f.db.ingest(f.sub, [], 1, 300)
        f.db.ingest(f.sub, [post()], 1000, 300)
        item = f.db.due(1000)[0]
        f.db.set_translation_media(item["queue_id"], [])
        f.db.part_sent(item["queue_id"], "text")
        await service.deliver_once()
        translator.assert_not_awaited()
        assert sender.await_count == 1 and sender.call_args.args[1:] == ("", [b"image"])
        f.rss.image.assert_awaited_once_with(post().images[0])
        assert f.db.status(f.sub.key)["pending"] == 0

    try:
        asyncio.run(run())
    finally:
        f.db.close()


def test_routine_polling_is_debug_while_failures_remain_warning(tmp_path, caplog):
    f = fixture(tmp_path)
    f.rss.fetch.return_value = [post()]
    service = Service(f.cfg, f.db, f.rss, AsyncMock(), LOG, clock=lambda: f.now[0])

    async def run():
        with caplog.at_level(logging.INFO, logger=LOG.name):
            await service.poll_once()
        assert not caplog.records
        f.now[0] += 1000
        with caplog.at_level(logging.DEBUG, logger=LOG.name):
            await service.poll_once()
        records = [r for r in caplog.records if r.name == LOG.name]
        assert len(records) == 3 and all(r.levelno == logging.DEBUG for r in records)
        caplog.clear()
        f.now[0] += 1000
        f.rss.fetch.side_effect = FeedError("read failed")
        with caplog.at_level(logging.INFO, logger=LOG.name):
            await service.poll_once()
        assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING

    try:
        asyncio.run(run())
    finally:
        f.db.close()
