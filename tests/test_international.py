import asyncio
import json
import logging
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nikke.chat import build_context
from nikke.classifier import ContentFilter
from nikke.config import OFFICIAL_LANGUAGES, Settings
from nikke.feed import Entry, FeedError, RSSClient, parse_feed
from nikke.service import Service, message_text
from nikke.store import Store


def item(language="zh-TW", **changes):
    return {
        "__template_key": "blablalink",
        "name": "国际" + language,
        "enabled": True,
        "language": language,
        "targets": ["qq:GroupMessage:1"],
        **changes,
    }


def settings(*items, **changes):
    return Settings.from_dict({"enabled": True, "subscriptions": list(items), **changes})


def entry():
    return Entry(
        "blablalink:account:zh-TW:9223372036854775807",
        "勝利女神",
        "恭喜指揮官中獎！私信收貨地址\nhttps://example.com/獎勵?q=資訊",
        "https://www.blablalink.com/post/detail?post_uuid=9223372036854775807&lang=zh-TW",
        "勝利女神官方",
        101,
        ("https://sg-cdn.blablalink.com/a.png?height=400&width=900",),
    )


def test_schema_source_defaults_and_legacy_identity():
    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())
    fields = schema["subscriptions"]["templates"]["blablalink"]["items"]
    saved = {key: value["default"] for key, value in fields.items()}
    saved["__template_key"] = "blablalink"
    assert not settings(saved).subscriptions
    assert not settings({"__template_key": "blablalink"}).subscriptions
    for language in OFFICIAL_LANGUAGES:
        sub = settings(item(language)).subscriptions[0]
        assert sub.language == language and sub.source == "blablalink"
        assert sub.url.endswith("/nikke-blablalink/official/" + language)
        assert sub.key == replace(sub, simplify=False, url="http://another-rss/route").key
    old = settings({"name": "旧订阅", "uid": "1", "targets": []}).subscriptions[0]
    assert old.source == "bilibili" and old.key == sha256(old.url.encode()).hexdigest()


def test_language_and_group_conflicts_are_explicit():
    with pytest.raises(ValueError, match="QQ 群 1.*国际zh-TW.*国际en"):
        settings(item(), item("en"))
    with pytest.raises(ValueError, match="QQ 群 1"):
        settings(item(), item("ja", targets=["other:GroupMessage:1"]))
    with pytest.raises(ValueError, match="同一 UID 或官方语言"):
        settings(item(), item(name="另一个", targets=[]))
    with pytest.raises(ValueError, match="国际服语言"):
        settings(item("xx"))
    assert (
        len(
            settings(
                item(),
                item("ko", targets=["qq:GroupMessage:2"]),
                {
                    "name": "国服",
                    "uid": "1",
                    "targets": ["qq:GroupMessage:1"],
                },
            ).subscriptions
        )
        == 3
    )


def test_display_toggle_and_chat_preserve_raw_archive_and_delivery_state(tmp_path):
    sub = settings(item()).subscriptions[0]
    db = Store(tmp_path / "state.db")
    raw = entry()
    try:
        db.sync((sub,))
        db.ingest(sub, [raw], 100, 300)
        before = db.source(sub.key)
        rows = db.recent_entries((sub.key,), 0, 300)
        text = message_text(sub, raw)
        assert "胜利女神" in text and "中獎" not in text and "中奖" in text
        assert "https://example.com/獎勵?q=資訊" in text
        assert "国际服 / 繁中官方" in text
        assert json.loads(rows[0]["payload"])["text"] == raw.text
        assert "中獎" in message_text(replace(sub, simplify=False), raw)
        for lang in ("en", "ja", "ko"):
            assert raw.text in message_text(replace(sub, language=lang), raw)
        context = build_context((sub,), rows, 7, 300, 12000)
        records = json.loads(context.split("\n")[-1])
        assert records[0]["language"] == "zh-TW" and "中奖" in records[0]["text"]
        changed = replace(sub, simplify=False)
        db.sync((changed,))
        db.ingest(changed, [raw], 400, 300)
        assert db.due(400) == []
        assert db.source(sub.key)["failures"] == before["failures"]
        assert "中獎" in build_context((changed,), rows, 7, 300, 12000)
    finally:
        db.close()


def test_filter_bypass_and_media_retry_across_restart_and_new_language(tmp_path):
    async def run():
        sub = settings(item()).subscriptions[0]
        cfg = settings(item(), ai_filter_enabled=True, jev_filter_enabled=True, jev_token="test")
        path = tmp_path / "state.db"
        db = Store(path)
        db.sync((sub,))
        db.ingest(sub, [], 100, 300)
        db.ingest(sub, [entry()], 400, 300)
        client = AsyncMock()
        client.image.side_effect = FeedError("temporary")
        classifier = AsyncMock(side_effect=AssertionError("must bypass"))
        sender = AsyncMock()
        service = Service(
            cfg,
            db,
            client,
            sender,
            logging.getLogger("international"),
            clock=lambda: 400,
            classifier=classifier,
        )
        ctx = SimpleNamespace(llm_generate=AsyncMock(side_effect=AssertionError("no LLM")))
        filtering = ContentFilter(ctx, cfg, db, logging.getLogger("international"))
        assert not (await filtering.assess(sub, entry())).skip
        ctx.llm_generate.assert_not_called()
        await service.deliver_once()
        classifier.assert_not_called()
        assert sender.await_count == 1 and "中奖" in sender.await_args.args[1]
        assert db.status(sub.key)["pending"] == 1
        await service.close()
        db = Store(path)
        db.sync((sub,))
        client = AsyncMock()
        client.image.return_value = b"original"
        sender = AsyncMock()
        service = Service(
            cfg,
            db,
            client,
            sender,
            logging.getLogger("international"),
            clock=lambda: 800,
            classifier=classifier,
        )
        await service.deliver_once()
        assert sender.await_args.args[1:] == ("", [b"original"])
        assert db.status(sub.key)["pending"] == 0
        # A new language/group builds its own baseline and never replays old posts.
        other = settings(item("en", targets=["qq:GroupMessage:2"])).subscriptions[0]
        db.sync((sub, other))
        db.ingest(other, [entry()], 900, 300)
        assert db.due(900) == [] and db.status(other.key)["initialized"] == 1
        assert db.status(sub.key)["initialized"] == 1
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "redirect",
    [
        None,
        "/original.png?height=400&width=900",
        "http://127.0.0.1/private",
        "https://sg-cdn.blablalink.com.evil.invalid/x",
    ],
)
def test_official_images_preserve_url_and_validate_redirects(redirect):
    async def run():
        calls = []
        body = b"\x89PNG\r\n\x1a\noriginal"

        class Content:
            async def iter_chunked(self, size):
                yield body

        class Response:
            content = Content()

            def __init__(self, status=200, headers=None):
                self.status, self.headers = status, headers or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return (
                    Response(302, {"Location": redirect})
                    if redirect and len(calls) == 1
                    else Response()
                )

            async def close(self):
                pass

        client = RSSClient()
        await client.session.close()
        client.session = Session()
        try:
            url = entry().images[0]
            if redirect and redirect.startswith("http"):
                with pytest.raises(FeedError, match="域名"):
                    await client.image(url)
                assert len(calls) == 1
            else:
                assert await client.image(url) == body
                assert calls[0][0] == url
                assert calls[-1][0].endswith("height=400&width=900")
                assert calls[0][1]["headers"]["Referer"] == "https://www.blablalink.com/"
        finally:
            await client.close()

    asyncio.run(run())


def test_standard_rss_round_trip():
    rss = b"""<rss version="2.0"><channel><title>Official</title><item>
    <guid>blablalink:account:en:9223372036854775807</guid><title>News</title>
    <link>https://www.blablalink.com/post/detail?post_uuid=9223372036854775807&amp;lang=en</link>
    <pubDate>Sat, 24 Oct 2026 03:00:00 GMT</pubDate><author>NIKKE</author>
    <description><![CDATA[<div>Official<br>news</div>
    <p><a href="https://youtube.com/watch?v=test">Video</a></p>
    <img src="https://sg-cdn.blablalink.com/a.png?height=400&amp;width=900">]]></description>
    </item></channel></rss>"""
    parsed = parse_feed(rss)[0]
    assert parsed.id.endswith("9223372036854775807") and parsed.published
    assert "Official\nnews" in parsed.text and "https://youtube.com/watch?v=test" in parsed.text
    assert parsed.images == entry().images
