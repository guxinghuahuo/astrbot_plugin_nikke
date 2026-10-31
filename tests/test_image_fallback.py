import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nikke.config import Settings
from nikke.feed import Entry, FeedError
from nikke.image_fallback import ImageMatcher, original_image_pages, parse_match
from nikke.service import Service
from nikke.store import Store


def settings(**fallback):
    return Settings.from_dict(
        {
            "enabled": True,
            "image_fallback": {"enabled": True, "provider": "model", **fallback},
            "subscriptions": [
                {
                    "__template_key": "blablalink",
                    "enabled": True,
                    "name": "国际服",
                    "language": "zh-TW",
                    "targets": ["qq:GroupMessage:1"],
                }
            ],
        }
    )


def original(ident="original", images=1):
    return Entry(
        ident,
        "4週年倒數3天",
        "周年更新倒數3天，角色A登場。",
        "https://www.blablalink.com/post/detail?post_uuid=" + ident,
        "官方",
        1001,
        tuple("https://sg-cdn.blablalink.com/" + str(i) + ".png" for i in range(images)),
    )


def candidate(ident="bili", images=1):
    return Entry(
        ident,
        "4周年倒计时3天",
        "周年更新倒计时3天，角色A登场。",
        "https://t.bilibili.com/" + ident,
        "官方",
        1100,
        tuple("https://i0.hdslb.com/" + str(i) + ".png" for i in range(images)),
    )


def setup(tmp_path, **options):
    cfg, now = settings(**options), [1100.0]
    sub = cfg.subscriptions[0]
    db = Store(tmp_path / "state.db")
    db.sync((sub,))
    db.ingest(sub, [], 1000, 300)
    db.ingest(sub, [original()], now[0], 300)
    client, sender = AsyncMock(), AsyncMock()
    client.fetch.return_value = []

    def image(url):
        if "blablalink" in url:
            raise FeedError("original unavailable")
        return b"bilibili-original-image"

    client.image.side_effect = image
    matcher = AsyncMock(side_effect=lambda sub, orig, items, target: items[0] if items else None)
    service = Service(
        cfg,
        db,
        client,
        sender,
        logging.getLogger("fallback"),
        clock=lambda: now[0],
        image_matcher=matcher,
    )
    return cfg, now, sub, db, client, sender, matcher, service


@pytest.mark.parametrize(
    "option",
    [
        {"retry_minutes": 0},
        {"retry_minutes": 61},
        {"retry_minutes": True},
        {"window_hours": -1},
        {"confidence": "nan"},
        {"confidence": True},
        {"uid": "bad"},
    ],
)
def test_invalid_fallback_configuration(option):
    with pytest.raises(ValueError):
        settings(**option)


def test_fallback_defaults_and_original_link_pagination():
    assert not Settings.from_dict({}).image_fallback.enabled
    cfg = settings().image_fallback
    assert cfg.retry_minutes == 5 and cfg.window_hours == 0 and cfg.confidence == 0.9
    entry = replace(
        original(),
        images=tuple("https://sg-cdn.blablalink.com/" + str(i) + "a" * 150 for i in range(30)),
    )
    pages = original_image_pages(entry)
    assert len(pages) > 1 and max(map(len, pages)) <= 2800
    assert all(url in "\n".join(pages) for url in entry.images)


@pytest.mark.parametrize(
    "text",
    [
        "not-json",
        "[]",
        '{"matched":true,"candidate_id":"invented","confidence":1,"reason":"x"}',
        '{"matched":true,"candidate_id":"bili","confidence":true,"reason":"x"}',
    ],
)
def test_invalid_model_decision_is_not_accepted(text):
    with pytest.raises(FeedError):
        parse_match(text, [candidate()])


def test_wait_for_delayed_bilibili_then_send_supplement_without_repeating_body(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, matcher, service = setup(tmp_path)
        await service.deliver_once()
        assert sender.await_count == 2  # body, then all original image URLs
        assert original().images[0] in sender.await_args.args[1]
        assert db.status(sub.key)["pending"] == 0 and db.image_job_count() == 1
        now[0] += 5
        await service.image_fallback.deliver_once()
        matcher.assert_not_awaited()
        job = db.image_jobs(now[0] + 300)[0]
        assert job["next_try"] == now[0] + 300 and job["attempts"] == 1
        # Waiting for one post never blocks a newer one in the normal outbox.
        db.ingest(sub, [replace(original("newer"), published=now[0] + 1)], now[0] + 2, 300)
        assert db.due(now[0] + 2)[0]["entry_id"] == "newer"
        # Bilibili publishes later. A new poll finds the corresponding candidate.
        now[0] += 300
        client.fetch.return_value = [candidate()]
        await service.image_fallback.deliver_once()
        matcher.assert_awaited_once()
        assert "配图来自 bilibili" in sender.await_args.args[1]
        assert candidate().link in sender.await_args.args[1]
        assert original().images[0] in sender.await_args.args[1]
        now[0] += 5
        await service.image_fallback.deliver_once()
        assert sender.await_args.args[1:] == ("", [b"bilibili-original-image"])
        assert db.image_job_count() == 0
        assert sender.await_count == 4  # no repeated original body or links
        archived = [json.loads(row["payload"]) for row in db.recent_entries((sub.key,), 0, now[0])]
        assert any(
            item["id"] == "original" and item["images"] == list(original().images)
            for item in archived
        )
        await service.close()

    asyncio.run(run())


def test_original_recovery_is_preferred_without_invoking_model(tmp_path):
    async def run():
        _, now, _, db, client, sender, matcher, service = setup(tmp_path)
        await service.deliver_once()
        now[0] += 5
        await service.image_fallback.deliver_once()
        client.image.side_effect = None
        client.image.return_value = b"recovered-official-image"
        now[0] += 300
        await service.image_fallback.deliver_once()
        matcher.assert_not_awaited()
        assert sender.await_args.args[1:] == ("", [b"recovered-official-image"])
        assert db.image_job_count() == 0
        await service.close()

    asyncio.run(run())


def test_partial_original_success_and_upload_failures_do_not_trigger_replacement(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, _, service = setup(tmp_path)
        entry = original(images=2)
        client.image.side_effect = lambda url: (
            b"ok" if url.endswith("0.png") else (_ for _ in ()).throw(FeedError("fail"))
        )
        with pytest.raises(FeedError):
            await service.send_entry(sub, entry, sub.targets[0])
        assert db.image_job_count() == 0
        client.image.side_effect = None
        client.image.return_value = b"ok"
        sender.side_effect = lambda target, text, images: (
            (_ for _ in ()).throw(RuntimeError("upload")) if images else None
        )
        with pytest.raises(FeedError):
            await service.send_entry(sub, entry, sub.targets[0])
        assert db.image_job_count() == 0
        await service.close()

    asyncio.run(run())


def test_pinned_match_and_partial_progress_survive_restart(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, matcher, service = setup(tmp_path)
        client.fetch.return_value = [candidate(images=2)]
        await service.deliver_once()
        now[0] += 5
        await service.image_fallback.deliver_once()  # match and attribution
        now[0] += 5
        await service.image_fallback.deliver_once()  # first image
        assert db.image_job_count() == 1
        await service.close()
        db = Store(tmp_path / "state.db")
        db.sync((sub,))
        now[0] += 5
        client, sender, matcher = (
            AsyncMock(),
            AsyncMock(),
            AsyncMock(side_effect=AssertionError("must reuse match")),
        )
        client.image.return_value = b"second-image"
        service = Service(
            cfg,
            db,
            client,
            sender,
            logging.getLogger("fallback"),
            clock=lambda: now[0],
            image_matcher=matcher,
        )
        await service.image_fallback.deliver_once()
        assert client.image.await_args.args[0].endswith("1.png")
        client.fetch.assert_not_awaited()
        matcher.assert_not_awaited()
        assert sender.await_count == 1 and not sender.await_args.args[1]
        assert db.image_job_count() == 0
        await service.close()

    asyncio.run(run())


def test_disabled_automatic_pauses_but_explicit_manual_supplement_can_continue(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, _, service = setup(tmp_path)
        await service.deliver_once()
        now[0] += 5
        service.image_fallback.settings = replace(cfg, enabled=False)
        await service.image_fallback.deliver_once()
        client.fetch.assert_not_awaited()
        # An explicit manual preview upgrades only this post's supplement to manual.
        await service.send_entry(sub, original(), sub.targets[0])
        now[0] += 5
        await service.image_fallback.deliver_once()
        client.fetch.assert_awaited_once()
        db.sync(())
        assert db.image_job_count() == 0
        await service.close()

    asyncio.run(run())


def test_matcher_caches_unchanged_candidates_but_rechecks_new_posts_and_uses_text_only(tmp_path):
    async def run():
        cfg, _, sub, db, _, _, _, service = setup(tmp_path)
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=json.dumps(
                        {
                            "matched": False,
                            "candidate_id": "",
                            "confidence": 0.98,
                            "reason": "尚未同步",
                        }
                    )
                )
            ),
            get_current_chat_provider_id=AsyncMock(),
        )
        matcher = ImageMatcher(context, cfg, db, logging.getLogger("fallback"))
        assert await matcher.match(sub, original(), [candidate()], sub.targets[0]) is None
        assert await matcher.match(sub, original(), [candidate()], sub.targets[0]) is None
        assert context.llm_generate.await_count == 1
        call = context.llm_generate.await_args.kwargs
        assert call["tools"] is None and call["contexts"] == []
        assert "sg-cdn.blablalink" not in call["prompt"] and "hdslb.com" not in call["prompt"]
        context.llm_generate.return_value.completion_text = json.dumps(
            {"matched": True, "candidate_id": "new", "confidence": 0.95, "reason": "同一次具体发布"}
        )
        assert (
            await matcher.match(sub, original(), [candidate(), candidate("new")], sub.targets[0])
        ).id == "new"
        assert context.llm_generate.await_count == 2
        await service.close()

    asyncio.run(run())


def test_low_confidence_or_unavailable_model_keeps_waiting(tmp_path):
    async def run():
        cfg, _, sub, db, _, _, _, service = setup(tmp_path)
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=json.dumps(
                        {
                            "matched": True,
                            "candidate_id": "bili",
                            "confidence": 0.7,
                            "reason": "不确定",
                        }
                    )
                )
            )
        )
        matcher = ImageMatcher(context, cfg, db, logging.getLogger("fallback"))
        assert await matcher.match(sub, original(), [candidate()], sub.targets[0]) is None
        context.llm_generate.side_effect = RuntimeError("private-provider-token")
        with pytest.raises(FeedError) as error:
            await matcher.match(sub, original(), [candidate("new")], sub.targets[0])
        assert "private-provider-token" not in str(error.value)
        await service.close()

    asyncio.run(run())


def test_retry_stays_in_minutes_until_wait_limit_without_hour_backoff(tmp_path):
    async def run():
        _, now, _, db, _, _, _, service = setup(tmp_path, retry_minutes=2)
        await service.deliver_once()
        now[0] += 5
        for _ in range(5):
            await service.image_fallback.deliver_once()
            jobs = db.image_jobs(now[0] + 120)
            assert len(jobs) == 1 and jobs[0]["next_try"] == now[0] + 120
            now[0] += 120
        # Elapsed waiting stops the job, even if the next retry is later.
        now[0] += 30 * 86400
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0
        assert db.db.execute("SELECT status FROM image_fallbacks").fetchone()[0] == "expired"
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("disabled", ["fallback", "images"])
def test_disabling_feature_pauses_existing_supplements(tmp_path, disabled):
    async def run():
        cfg, now, _, db, client, sender, _, service = setup(tmp_path)
        await service.deliver_once()
        before = sender.await_count
        now[0] += 5
        service.image_fallback.settings = (
            replace(cfg, image_fallback=replace(cfg.image_fallback, enabled=False))
            if disabled == "fallback"
            else replace(cfg, send_images=False)
        )
        await service.image_fallback.deliver_once()
        assert sender.await_count == before and db.image_job_count() == 1
        client.fetch.assert_not_awaited()
        await service.close()

    asyncio.run(run())


def test_multigroup_dedup_reuses_llm_decision_and_source_fetch(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, _, service = setup(tmp_path)
        sub = replace(sub, targets=("qq:GroupMessage:1", "qq:GroupMessage:2"))
        cfg = replace(cfg, subscriptions=(sub,))
        await service.close()
        db = Store(tmp_path / "state.db")
        db.sync((sub,))
        db.ingest(sub, [], 1000, 300)
        db.ingest(sub, [original()], now[0], 300)
        client.fetch.return_value = [candidate()]
        context = SimpleNamespace(
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=json.dumps(
                        {
                            "matched": True,
                            "candidate_id": "bili",
                            "confidence": 0.98,
                            "reason": "同一次公告",
                        }
                    )
                )
            )
        )
        matcher = ImageMatcher(context, cfg, db, logging.getLogger("fallback"))
        service = Service(
            cfg,
            db,
            client,
            sender,
            logging.getLogger("fallback"),
            clock=lambda: now[0],
            image_matcher=matcher.match,
        )
        await service.deliver_once()
        now[0] += 5
        await service.image_fallback.deliver_once()
        context.llm_generate.assert_awaited_once()
        client.fetch.assert_awaited_once()
        now[0] += 5
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0
        assert {call.args[0] for call in sender.await_args_list if call.args[2]} == set(sub.targets)
        await service.close()

    asyncio.run(run())


def test_time_window_is_optional_and_never_matches_missing_or_outside_times_when_set(tmp_path):
    async def run():
        _, now, _, _, client, _, _, service = setup(tmp_path, window_hours=1)
        now[0] = 30000
        client.fetch.return_value = [
            candidate(),
            replace(candidate("late"), published=20000),
            replace(candidate("unknown"), published=None),
        ]
        items = await service.image_fallback.candidates(original())
        assert [item.id for item in items] == ["bili"]
        await service.close()

    asyncio.run(run())


def test_additive_sqlite_migration_preserves_existing_outbox_and_archive(tmp_path):
    cfg = settings()
    sub = cfg.subscriptions[0]
    path = tmp_path / "migration.db"
    db = Store(path)
    db.sync((sub,))
    db.ingest(sub, [], 1000, 300)
    db.ingest(sub, [original()], 1100, 300)
    before = db.due(1100)
    archive = db.recent_entries((sub.key,), 0, 1100)
    db.db.executescript("DROP TABLE image_fallbacks; DROP TABLE image_match_cache;")
    db.close()
    db = Store(path)
    assert db.due(1100) == before
    assert db.recent_entries((sub.key,), 0, 1100) == archive
    assert db.image_jobs(1100) == []
    db.close()


@pytest.mark.parametrize("value", [0, -1, 10081, True, 1.5, "bad"])
def test_invalid_wait_limit(value):
    with pytest.raises(ValueError, match="max_wait_minutes"):
        settings(max_wait_minutes=value)


def test_wait_limit_defaults_to_thirty_minutes():
    assert Settings.from_dict({}).image_fallback.max_wait_minutes == 30
    assert settings().image_fallback.max_wait_minutes == 30
    assert settings(max_wait_minutes="120").image_fallback.max_wait_minutes == 120


def test_exact_deadline_expires_future_retry_without_fetching_or_sending(tmp_path):
    async def run():
        _, now, sub, db, client, sender, matcher, service = setup(tmp_path)
        created = now[0]
        await service.deliver_once()
        job = db.image_jobs(now[0])[0]
        db.image_job_update(job["job_id"], next_try=created + 100000)
        count = sender.await_count
        now[0] = created + 30 * 60
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0 and sender.await_count == count
        assert (
            db.db.execute("SELECT status FROM deliveries WHERE entry_id='original'").fetchone()[0]
            == "image_expired"
        )
        client.fetch.assert_not_awaited()
        matcher.assert_not_awaited()
        assert json.loads(db.recent_entries((sub.key,), 0, now[0])[0]["payload"])["images"] == list(
            original().images
        )
        # Re-enqueue or increasing the limit cannot revive an expired job.
        db.defer_images(sub, original(), sub.targets[0], now[0])
        service.image_fallback.settings = replace(
            service.settings,
            image_fallback=replace(service.settings.image_fallback, max_wait_minutes=120),
        )
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0
        await service.close()

    asyncio.run(run())


def test_restart_preserves_created_time_and_short_limit_caps_next_retry(tmp_path):
    async def run():
        cfg, now, sub, db, client, sender, matcher, service = setup(tmp_path, max_wait_minutes=1)
        created = now[0]
        await service.deliver_once()
        now[0] += 5
        await service.image_fallback.deliver_once()
        assert db.image_jobs(created + 60)[0]["next_try"] == created + 60
        await service.close()
        now[0] = created + 60
        db = Store(tmp_path / "state.db")
        db.sync((sub,))
        sender = AsyncMock()
        service = Service(
            cfg,
            db,
            client,
            sender,
            logging.getLogger("fallback"),
            clock=lambda: now[0],
            image_matcher=matcher,
        )
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0
        sender.assert_not_awaited()
        assert db.db.execute("SELECT created FROM image_fallbacks").fetchone()[0] == created
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["fetch", "model", "image"])
def test_expiry_during_slow_operation_stops_subsequent_model_or_send(tmp_path, stage):
    async def run():
        _, now, _, db, client, sender, matcher, service = setup(tmp_path, max_wait_minutes=1)
        created = now[0]
        await service.deliver_once()
        before = sender.await_count
        now[0] += 5
        client.fetch.return_value = [candidate()]
        if stage == "fetch":

            def fetch(url):
                now[0] = created + 60
                return [candidate()]

            client.fetch.side_effect = fetch
        elif stage == "model":

            def match(*args):
                now[0] = created + 60
                return candidate()

            matcher.side_effect = match
        else:
            await service.image_fallback.deliver_once()  # matched, attribution sent
            before = sender.await_count
            now[0] = created + 59

            def image(url):
                now[0] = created + 60
                return b"late-image"

            client.image.side_effect = image
        await service.image_fallback.deliver_once()
        assert db.image_job_count() == 0 and sender.await_count == before
        if stage == "fetch":
            matcher.assert_not_awaited()
        await service.close()

    asyncio.run(run())
