import asyncio
import logging
from dataclasses import replace

import pytest

from nikke.config import Settings, Subscription
from nikke.feed import Entry, FeedError
from nikke.service import Service, error_summary, message_text
from nikke.store import Store


def sub(targets=("test:GroupMessage:100", "test:GroupMessage:200")):
    return Subscription("NIKKE", "1", "http://rss/bilibili/user/dynamic/1", targets)


def entry(ident, published=100):
    return Entry(
        str(ident), "更新", "正文", "https://t.bilibili.com/" + str(ident), "官方", published, ()
    )


def test_rsshub_title_does_not_repeat_multiline_body():
    item = replace(
        entry(1),
        title="#NIKKE4周年# 【NIKKE4周年倒计时】 版本更新还有6天！",
        text="#NIKKE4周年#\n【NIKKE4周年倒计时】\n 版本更新还有6天！",
    )
    result = message_text("NIKKE官方", item)
    assert result.count("版本更新还有6天！") == 1
    assert item.text in result
    distinct = replace(item, title="官方公告")
    assert "官方公告\n" + item.text in message_text("NIKKE官方", distinct)


def test_baseline_dedup_restart_and_independent_targets(tmp_path):
    path, subscription = tmp_path / "state.db", sub()
    db = Store(path)
    db.sync((subscription,))
    db.ingest(subscription, [entry(1, 50)], 100, 300)
    assert db.due(100) == []
    db.ingest(subscription, [entry(3, 102), entry(2, 101), entry(1, 50)], 400, 300)
    first = db.due(400)
    assert len(first) == 2 and {r["entry_id"] for r in first} == {"2"}
    db.delivered(first[0]["queue_id"])
    db.delivery_failed(first[1]["queue_id"], 400, "offline")
    pending = db.due(403)
    assert len(pending) == 1 and pending[0]["entry_id"] == "3"
    assert pending[0]["target"] == first[0]["target"]
    db.delivered(pending[0]["queue_id"])
    db.close()
    db = Store(path)
    db.sync((subscription,))
    db.ingest(subscription, [entry(3, 102), entry(2, 101)], 700, 300)
    pending = db.due(700)
    assert len(pending) == 1 and pending[0]["target"] == first[1]["target"]
    assert pending[0]["entry_id"] == "2"
    assert db.status(subscription.key)["pending"] == 2
    db.close()


def test_new_target_old_pinned_and_removed_targets(tmp_path):
    db, s = Store(tmp_path / "state.db"), sub(("test:GroupMessage:100",))
    db.sync((s,))
    db.ingest(s, [entry(1, 50)], 100, 300)
    expanded = replace(s, targets=s.targets + ("test:GroupMessage:200",))
    db.sync((expanded,))
    db.ingest(expanded, [entry(2, 101), entry(0, 20), entry(1, 50)], 400, 300)
    pending = db.due(400)
    assert len(pending) == 1 and pending[0]["target"] == s.targets[0]
    assert pending[0]["entry_id"] == "2"
    db.sync((replace(s, targets=("test:GroupMessage:200",)),))
    assert db.due(500) == []
    db.close()


@pytest.mark.parametrize("parts", [(), ("text",), ('translation_media:["image"]',)])
def test_translation_backoff_restarts_without_losing_order_or_partial_delivery(tmp_path, parts):
    path = tmp_path / "state.db"
    s = sub()
    db = Store(path)
    db.sync((s,))
    db.ingest(s, [], 1, 300)
    db.ingest(s, [entry("old", 100), entry("new", 101)], 400, 300)
    first = db.due(400)
    for part in parts:
        db.part_sent(first[0]["queue_id"], part)
    db.delivery_failed(first[0]["queue_id"], 400, "翻译校验失败：无效 JSON")
    db.close()
    db = Store(path)
    try:
        ready = db.due(403)
        expected = {(first[1]["target"], "old")}
        if not parts:
            expected.add((first[0]["target"], "new"))
        assert {(r["target"], r["entry_id"]) for r in ready} == expected
        # Once its retry is due, the older post is selected first again.
        assert {r["entry_id"] for r in db.due(460)} == {"old"}
        assert db.status(s.key)["pending"] == 4
    finally:
        db.close()


def test_switch_to_group_number_preserves_baseline_and_retry_queue(tmp_path):
    cfg = {"subscriptions": [{"name": "NIKKE", "uid": "1", "targets": ["qq:GroupMessage:100"]}]}
    old = Settings.from_dict(cfg).subscriptions[0]
    db = Store(tmp_path / "state.db")
    try:
        db.sync((old,))
        db.ingest(old, [entry(1, 50)], 100, 300)
        db.ingest(old, [entry(2, 101)], 400, 300)
        db.delivery_failed(db.due(400)[0]["queue_id"], 400, "offline")
        before = db.source(old.key)
        queued = db.due(500)
        cfg["subscriptions"][0]["targets"] = ["100"]
        new = Settings.from_dict(cfg, qq_platform_ids=("qq",)).subscriptions[0]
        db.sync((new,))
        assert db.source(old.key) == before
        assert db.due(500) == queued
        assert db.status(old.key)["initialized"] == 1
        assert db.db.execute("select baseline from relations").fetchone()[0] == 100
        db.ingest(new, [entry(1, 50), entry(2, 101)], 700, 300)
        assert len(db.due(700)) == 1
        assert db.due(700)[0]["entry_id"] == "2"
    finally:
        db.close()


def test_empty_snapshot_and_failure_do_not_confuse_baseline(tmp_path):
    db, s = Store(tmp_path / "state.db"), sub()
    db.sync((s,))
    db.fetch_failed(s.key, 100, 300, "HTTP 503")
    assert db.status(s.key)["initialized"] == 0
    assert db.source(s.key)["last_success"] is None
    assert db.source(s.key)["next_poll"] == 400
    db.ingest(s, [], 500, 300)
    assert db.status(s.key)["initialized"] == 2
    db.ingest(s, [entry(2, 501)], 800, 300)
    assert len(db.due(800)) == 2
    db.close()


def test_transaction_rollback(tmp_path):
    db, s = Store(tmp_path / "state.db"), sub()
    db.sync((s,))
    bad = replace(entry("bad", 100), text=object())
    with pytest.raises(TypeError):
        db.ingest(s, [bad], 100, 300)
    assert db.status(s.key)["initialized"] == 0
    assert db.source(s.key)["last_success"] is None
    db.close()


class FakeClient:
    def __init__(self):
        self.entries = [entry(1, 50)]
        self.fail = False
        self.closed = False

    async def fetch(self, url):
        if self.fail:
            raise FeedError("HTTP 503")
        return self.entries

    async def image(self, url):
        return b"original-image"

    async def close(self):
        self.closed = True


def test_poll_send_retry_preview_and_shutdown(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        s, client, now = sub(), FakeClient(), [100.0]
        db = Store(tmp_path / "state.db")
        db.sync((s,))
        delivered = []
        fail_target = [s.targets[1]]

        async def sender(target, text, images):
            if target == fail_target[0]:
                raise RuntimeError("offline")
            if text:
                delivered.append(target)

        service = Service(
            Settings(True, 300, True, (s,), jitter=0),
            db,
            client,
            sender,
            logging.getLogger("test"),
            clock=lambda: now[0],
        )
        client.fail = True
        await service.poll_once()
        assert db.status(s.key)["initialized"] == 0
        now[0] = 400
        client.fail = False
        await service.poll_once()
        assert db.due(400) == []
        now[0] = 700
        client.entries = [replace(entry(2, 401), images=("https://i0.hdslb.com/a.png",))]
        await service.inspect(s.name)
        assert db.due(700) == []  # Preview is read-only.
        await service.poll_once()
        await service.deliver_once()
        assert delivered == [s.targets[0]]
        assert db.status(s.key)["pending"] == 1
        now[0] = 760
        fail_target[0] = ""
        await service.deliver_once()
        assert delivered == list(s.targets)
        assert db.status(s.key)["pending"] == 0
        await service.close()
        assert client.closed

    asyncio.run(scenario())
    assert "RSS 获取成功" in caplog.text
    assert "轮询成功" not in caplog.text  # Routine polling is Debug, sends remain Info.
    assert "原图已发送" in caplog.text
    assert "自动投递失败" in caplog.text and "offline" in caplog.text
    assert "已记录投递成功" in caplog.text


def test_error_logs_bound_payload_and_hide_credentials():
    assert "secret" not in error_summary(RuntimeError("token=secret"))
    assert "secret" not in error_summary(RuntimeError("Cookie: secret\nmore_secret"))
    assert "pixels" not in error_summary(RuntimeError("image=base64://pixels"))
    assert len(error_summary(RuntimeError("x" * 10000))) < 600
    assert "超时" in error_summary(asyncio.TimeoutError())


def test_disabled_service_no_fetch_or_delivery_and_tasks_cancel(tmp_path):
    async def scenario():
        s, client = sub(), FakeClient()
        client.fail = True

        async def sender(*args):
            raise AssertionError("must not send")

        db = Store(tmp_path / "state.db")
        service = Service(
            Settings(False, 300, True, (s,)), db, client, sender, logging.getLogger("test")
        )
        await service.start()
        await service.poll_once()
        await service.deliver_once()
        assert db.status(s.key)["last_error"] == ""
        assert not service.tasks
        await service.close()

        db = Store(tmp_path / "state.db")
        service = Service(
            Settings(True, 300, True, (s,)), db, FakeClient(), sender, logging.getLogger("test")
        )
        await service.start()
        tasks = list(service.tasks)
        await service.close()
        assert all(t.done() for t in tasks)

    asyncio.run(scenario())
