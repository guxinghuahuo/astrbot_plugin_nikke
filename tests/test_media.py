import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from nikke.classifier import Decision
from nikke.config import Settings, Subscription
from nikke.feed import Entry, RSSClient
from nikke.service import Service
from nikke.store import Store


def setup_data(tmp_path):
    sub = Subscription("官方", "1", "http://rss/1", ("qq:GroupMessage:1",))
    settings = Settings(True, 300, True, (sub,), jitter=0)
    entry = Entry(
        "new",
        "公告",
        "最新公告",
        "https://t.bilibili.com/1",
        "官方",
        101,
        tuple(f"https://i0.hdslb.com/{name}.png" for name in ("a", "b", "c")),
    )
    db = Store(tmp_path / "state.db")
    db.sync((sub,))
    db.ingest(sub, [], 100, 300)
    db.ingest(sub, [entry], 400, 300)
    return sub, settings, entry, db


def test_large_original_download_keeps_bytes_and_removes_thumbnail_transform():
    async def run():
        body = b"\x89PNG\r\n\x1a\n" + b"x" * (17 * 1024 * 1024)
        requests = []

        class Content:
            async def iter_chunked(self, size):
                for index in range(0, len(body), size):
                    yield body[index : index + size]

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def get(self, url, **kwargs):
                requests.append((url, kwargs))
                return Response()

            async def close(self):
                pass

        client = RSSClient()
        await client.session.close()
        client.session = Session()
        try:
            original = await client.image(
                "https://i0.hdslb.com/bfs/new_dyn/art.png@1048w_!web-dynamic.webp"
            )
            assert original == body
            assert requests[0][0] == "https://i0.hdslb.com/bfs/new_dyn/art.png"
            assert requests[0][1]["timeout"].total == 90
        finally:
            await client.close()

    asyncio.run(run())


def test_send_images_sequentially_without_old_aggregate_limit(tmp_path):
    async def run():
        sub, settings, entry, db = setup_data(tmp_path)
        events = []
        image = b"x" * (17 * 1024 * 1024)

        async def fetch_image(url):
            events.append(("fetch", url))
            return image

        async def send(target, text, images):
            assert bool(text) != bool(images)
            assert len(images) <= 1
            events.append(("send", "text" if text else len(images[0])))

        client = AsyncMock()
        client.image.side_effect = fetch_image
        service = Service(settings, db, client, send, logging.getLogger("media"), clock=lambda: 400)
        await service.deliver_once()
        assert events[0] == ("send", "text")
        assert events[1::2] == [("fetch", url) for url in entry.images]
        assert events[2::2] == [("send", len(image))] * 3
        assert db.status(sub.key)["pending"] == 0
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("fail_stage", ["download", "send"])
def test_partial_image_failure_resumes_after_restart_without_repeating_text_or_other_images(
    tmp_path,
    fail_stage,
):
    async def run():
        sub, settings, entry, db = setup_data(tmp_path)
        settings = replace(settings, ai_filter_enabled=True)
        sent = []
        client = AsyncMock()

        def image(url):
            if fail_stage == "download" and url == entry.images[1]:
                raise RuntimeError("Download failed")
            return url.encode()

        client.image.side_effect = image

        async def send(target, text, images):
            if fail_stage == "send" and images and images[0] == entry.images[1].encode():
                raise RuntimeError("QQ image upload failed")
            sent.append((text, images))

        service = Service(
            settings,
            db,
            client,
            send,
            logging.getLogger("media"),
            clock=lambda: 400,
            classifier=AsyncMock(return_value=Decision("news", "资讯")),
        )
        await service.deliver_once()
        assert db.status(sub.key)["pending"] == 1
        row = db.db.execute("SELECT * FROM deliveries").fetchone()
        assert set(json.loads(row["sent_parts"])) == {
            "text",
            "image:" + entry.images[0],
            "image:" + entry.images[2],
        }
        assert len(sent) == 3
        await service.close()
        db = Store(tmp_path / "state.db")
        client = AsyncMock()
        client.image.return_value = b"retry-original"
        sender = AsyncMock()
        classifier = AsyncMock(side_effect=AssertionError("Do not reclassify a partly sent entry"))
        service = Service(
            settings,
            db,
            client,
            sender,
            logging.getLogger("media"),
            clock=lambda: 460,
            classifier=classifier,
        )
        await service.deliver_once()
        client.image.assert_awaited_once_with(entry.images[1])
        sender.assert_awaited_once_with(sub.targets[0], "", [b"retry-original"])
        classifier.assert_not_awaited()
        assert db.status(sub.key)["pending"] == 0
        await service.close()

    asyncio.run(run())


def test_failed_text_send_does_not_download_images(tmp_path):
    async def run():
        sub, settings, entry, db = setup_data(tmp_path)
        client = AsyncMock()
        sender = AsyncMock(side_effect=RuntimeError("offline"))
        service = Service(
            settings, db, client, sender, logging.getLogger("media"), clock=lambda: 400
        )
        await service.deliver_once()
        client.image.assert_not_awaited()
        assert db.status(sub.key)["pending"] == 1
        assert db.db.execute("SELECT sent_parts FROM deliveries").fetchone()[0] == "[]"
        await service.close()

    asyncio.run(run())


def test_old_delivery_table_gains_progress_without_losing_queued_content(tmp_path):
    path = tmp_path / "state.db"
    sub = Subscription("官方", "1", "http://rss/1", ("qq:GroupMessage:1",))
    body = Entry("new", "公告", "原文", "https://t.bilibili.com/1", "官方", 101, ())
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sources (source TEXT PRIMARY KEY,last_success REAL,last_error TEXT DEFAULT '',
            failures INTEGER DEFAULT 0,next_poll REAL DEFAULT 0);
        CREATE TABLE relations (source TEXT,target TEXT,baseline REAL,PRIMARY KEY(source,target));
        CREATE TABLE deliveries (source TEXT,target TEXT,entry_id TEXT,payload TEXT,sort_ts REAL,
            status TEXT,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,
            last_error TEXT DEFAULT '',
            PRIMARY KEY(source,target,entry_id));
    """)
    con.execute("INSERT INTO sources(source,last_success,next_poll) VALUES(?,400,700)", (sub.key,))
    con.execute("INSERT INTO relations VALUES(?,?,100)", (sub.key, sub.targets[0]))
    con.execute(
        "INSERT INTO deliveries(source,target,entry_id,payload,sort_ts,status) "
        "VALUES(?,?,?,?,101,'pending')",
        (sub.key, sub.targets[0], body.id, json.dumps(body.to_dict())),
    )
    con.commit()
    con.close()
    db = Store(path)
    try:
        pending = db.due(400)
        assert len(pending) == 1 and pending[0]["sent_parts"] == "[]"
        assert json.loads(pending[0]["payload"])["text"] == "原文"
        assert db.source(sub.key)["next_poll"] == 700
        db.part_sent(pending[0]["queue_id"], "text")
    finally:
        db.close()
    db = Store(path)
    assert json.loads(db.due(400)[0]["sent_parts"]) == ["text"]
    db.close()
