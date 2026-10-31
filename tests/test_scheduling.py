import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from nikke.config import Settings, Subscription
from nikke.feed import FeedError
from nikke.service import Service
from nikke.store import Store


@pytest.mark.parametrize("offset,expected", [(-60, 240), (60, 360), (12.5, 312.5)])
def test_jitter_is_sampled_per_fetch_and_survives_restart(tmp_path, offset, expected):
    async def scenario():
        s = Subscription("test", "1", "http://rss/1", ("qq:GroupMessage:1",))
        db = Store(tmp_path / "state.db")
        db.sync((s,))
        now, draws = [100.0], []

        def random_uniform(low, high):
            draws.append((low, high))
            return offset

        client = AsyncMock()
        client.fetch.return_value = []
        settings = Settings(True, 300, False, (s,))
        service = Service(
            settings,
            db,
            client,
            AsyncMock(),
            logging.getLogger("schedule"),
            clock=lambda: now[0],
            random_uniform=random_uniform,
        )
        await service.poll_once()
        assert db.source(s.key)["next_poll"] == 100 + expected
        assert draws == [(-60, 60)]
        await service.close()

        db = Store(tmp_path / "state.db")
        db.sync((s,))
        service = Service(
            settings,
            db,
            client,
            AsyncMock(),
            logging.getLogger("schedule"),
            clock=lambda: now[0],
            random_uniform=random_uniform,
        )
        now[0] = 100 + expected - 1
        await service.poll_once()
        assert client.fetch.await_count == 1 and len(draws) == 1
        now[0] += 1
        await service.poll_once()
        assert client.fetch.await_count == 2 and len(draws) == 2
        assert "下次自动检查" in await service.status()
        await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "interval,jitter,offset,expected",
    [(60, 300, -300, 60), (3600, 300, 300, 3600), (300, 0, 0, 300)],
)
def test_poll_delay_bounds_and_jitter_off(tmp_path, interval, jitter, offset, expected):
    async def scenario():
        s = Subscription("test", "1", "http://rss/1", ("qq:GroupMessage:1",))
        db = Store(tmp_path / "state.db")
        db.sync((s,))
        client = AsyncMock()
        client.fetch.return_value = []

        def draw(*args):
            assert jitter > 0
            return offset

        service = Service(
            Settings(True, interval, False, (s,), jitter=jitter),
            db,
            client,
            AsyncMock(),
            logging.getLogger("schedule"),
            clock=lambda: 100,
            random_uniform=draw,
        )
        await service.poll_once()
        assert db.source(s.key)["next_poll"] == 100 + expected
        client.fetch.side_effect = FeedError("503")
        # Failure backoff also respects the configured floor/ceiling.
        for n in range(1, 9):
            db.fetch_failed(s.key, 100, interval, "503", offset)
            delay = max(60, min(3600, min(3600, interval * 2 ** (n - 1)) + offset))
            assert db.source(s.key)["next_poll"] == 100 + delay
        await service.close()

    asyncio.run(scenario())
