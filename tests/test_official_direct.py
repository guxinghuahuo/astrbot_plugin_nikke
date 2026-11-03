import asyncio
import copy
import json
import shutil
import subprocess
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from aiohttp import web

from nikke import blablalink
from nikke.blablalink import normalize
from nikke.config import OFFICIAL_LANGUAGES, OFFICIAL_OPENID, Settings, migrate_layout
from nikke.feed import FeedError, RSSClient, parse_feed
from nikke.store import Store


def post(language="zh-TW", **changes):
    return {
        "intl_openid": OFFICIAL_OPENID, "is_official": 1,
        "post_uuid": "9223372036854775807", "language": language,
        "title": "勝利女神 &amp; 公告", "content": "<div>官方<br>公告 &amp; 更新</div>",
        "user": {"username": "妮姬官方"},
        "pic_urls": ["https://sg-cdn.blablalink.com/a.png?height=1080&width=1920"],
        "publish_on": 1792810800, "created_on": "1792807200", **changes,
    }


@pytest.mark.parametrize("language", OFFICIAL_LANGUAGES)
def test_same_payload_matches_existing_node_route_in_every_entry_field(language):
    if not shutil.which("node"):
        pytest.skip("Node is required for cross-runtime route parity")
    first = post(language)
    rows = [
        first, copy.deepcopy(first),
        post(language, post_uuid="2", publish_on="bad", created_on="bad", pic_urls=[]),
        post(language, post_uuid="3", publish_on=0, pic_urls=[], type=3, ext_info=json.dumps([
            {"video_cover": "https://sg-cdn.blablalink.com/cover.png?x=1&y=2",
             "video_url": "https://www.youtube.com/watch?v=abcdefghijk", "video_title": "视频"}
        ])),
        post(language, post_uuid="4", is_del=1),
        *[post(language, post_uuid=str(i)) for i in range(10, 21)],
    ]
    payload = {"code": 0, "data": {"list": list(reversed(rows))}}
    script = """
import { normalize } from './deploy/rsshub/routes-user/nikke-blablalink.mjs';
let input = ''; for await (const c of process.stdin) input += c;
const data = JSON.parse(input); console.log(JSON.stringify(normalize(data.payload, data.language)));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps({"language": language, "payload": payload}),
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    route = json.loads(result.stdout)
    root = Element("rss", version="2.0")
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = route["title"]
    for item in route["item"]:
        node = SubElement(channel, "item")
        for key, value in item.items():
            SubElement(node, key).text = value
    assert normalize(payload, language) == parse_feed(tostring(root, encoding="utf-8"))
    assert len(normalize(payload, language)) == 14  # Never truncate pinned/additional posts.


@pytest.mark.parametrize("change", [
    {"intl_openid": "other"}, {"language": "en"}, {"post_uuid": 123},
    {"pic_urls": ["javascript:bad"]}, {"type": 3, "ext_info": "not JSON"},
])
def test_malformed_posts_fail_without_silently_establishing_baseline(change):
    with pytest.raises(FeedError):
        normalize({"code": 0, "data": {"list": [post(**change)]}}, "zh-TW")


def test_empty_conflicting_duplicates_and_business_errors():
    assert normalize({"code": 0, "data": {"list": []}}, "en") == []
    with pytest.raises(FeedError, match="冲突"):
        normalize({"code": 0, "data": {"list": [post(), post(title="changed")]}}, "zh-TW")
    for payload in ({"code": 1}, {"code": False, "data": {"list": []}}, {}):
        with pytest.raises(FeedError):
            normalize(payload, "en")


def test_shared_address_and_switch_preserve_delivery_state(tmp_path):
    sub = {"source": "blablalink", "name": "国际", "enabled": True,
           "language": "en", "targets": ["qq:GroupMessage:1"]}
    cfg = {"rsshub_base_url": "http://legacy:1200", "subscriptions": [sub]}
    legacy = Settings.from_dict({**cfg, "international_direct": False}).subscriptions[0]
    direct = Settings.from_dict(cfg).subscriptions[0]
    assert legacy.url == "http://legacy:1200/nikke-blablalink/official/en"
    assert direct.url == "blablalink://official/en" and direct.key == legacy.key
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.sync([legacy])
        store.db.execute("UPDATE relations SET baseline=123")
        store.db.execute(
            "INSERT INTO deliveries(source,target,entry_id,payload,sort_ts,status,sent_parts) "
            "VALUES(?,?,?,?,?,?,?)",
            (legacy.key, legacy.targets[0], "pending", "{}", 123, "pending", '["text"]'),
        )
        store.db.commit()
        store.sync([direct])
        assert store.db.execute("SELECT baseline FROM relations").fetchone()[0] == 123
        row = store.db.execute("SELECT entry_id,sent_parts FROM deliveries").fetchone()
        assert tuple(row) == ("pending", '["text"]')
    finally:
        store.close()
    cfg["international_direct"] = False
    cfg["subscriptions"].append({"name": "国服", "uid": "1", "targets": []})
    settings = Settings.from_dict(cfg)
    assert settings.subscriptions[0].url.startswith("http://legacy:1200/")
    assert settings.subscriptions[1].url.startswith("http://legacy:1200/")
    assert settings.image_fallback.url.startswith("http://legacy:1200/")


@pytest.mark.parametrize("value", ["ftp://invalid", "https://user:secret@host", "https://host?q=1"])
def test_shared_address_rejects_credentials_and_non_http_urls(value):
    with pytest.raises(ValueError, match="RSSHub 地址"):
        Settings.from_dict({"base": {"rsshub_base_url": value}})


def test_shared_address_and_default_switch_schema():
    from pathlib import Path

    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    fields = schema["base"]["items"]
    assert fields["international_direct"]["type"] == "bool"
    assert fields["international_direct"]["default"] is True
    assert "options" not in fields["international_direct"]
    assert {key for key in fields if "rsshub" in key} == {"rsshub_base_url"}
    with pytest.raises(ValueError, match="开关"):
        Settings.from_dict({"base": {"international_direct": "false"}})


@pytest.mark.parametrize("mode,expected", [("rsshub", False), ("direct", True)])
def test_unreleased_dropdown_config_migrates_once_to_switch(mode, expected):
    cfg = {"_layout_version": 1, "base": {
        "rsshub_base_url": "http://existing:1200", "international_source": mode,
        "bilibili_rsshub_base_url": "https://old-bili.example",
        "international_rsshub_base_url": "https://old-intl.example",
    }}
    assert migrate_layout(cfg)
    assert cfg["base"] == {"rsshub_base_url": "http://existing:1200",
                           "international_direct": expected}
    assert not migrate_layout(cfg)


def test_direct_http_contract_text_plain_and_sanitized_failures(monkeypatch):
    async def run():
        mode = ["ok"]

        async def handler(request):
            assert request.method == "POST"
            assert request.headers["x-language"] == "en"
            assert json.loads(request.headers["x-common-params"])["language"] == "en"
            assert "Cookie" not in request.headers and "Authorization" not in request.headers
            assert await request.json() == blablalink.request_data("en")[1]
            if mode[0] == "bad":
                return web.Response(text="private upstream body", status=503)
            if mode[0] == "large":
                return web.Response(body=b"x" * (4 * 1024 * 1024 + 1))
            return web.Response(text=json.dumps({"code": 0, "data": {"list": [post("en")]}}),
                                content_type="text/plain")

        app = web.Application()
        app.router.add_post("/official", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(blablalink, "ENDPOINT", f"http://127.0.0.1:{port}/official")
        client = RSSClient()
        try:
            assert (await client.fetch("blablalink://official/en"))[0].id.endswith("5807")
            for state in ("bad", "large"):
                mode[0] = state
                with pytest.raises(FeedError) as error:
                    await client.fetch("blablalink://official/en")
                assert "private upstream body" not in str(error.value)
            with pytest.raises(FeedError):
                await client.fetch("blablalink://evil/en")
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())
