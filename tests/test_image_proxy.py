import asyncio
import base64
import json
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from nikke.config import Settings
from nikke.feed import FeedError, RSSClient
from scripts.check_international import configured_proxy


@pytest.mark.parametrize(
    "value",
    [
        None,
        12,
        "socks5://user:secret@host:1080",
        "http://host:bad",
        "http://host:0",
        "http://host/path",
        "http://host?q=secret",
        "http://host#secret",
        "http://host:7890\nsecret",
    ],
)
def test_invalid_proxy_configuration_never_exposes_input(value):
    with pytest.raises(ValueError) as error:
        Settings.from_dict({"base": {"official_image_proxy": value}})
    assert "secret" not in str(error.value)


def test_proxy_is_optional_masked_and_read_from_grouped_config(tmp_path, monkeypatch):
    proxy = "http://user:secret@192.168.0.2:7890"
    assert Settings.from_dict({}).official_image_proxy == ""
    cfg = {"base": {"official_image_proxy": proxy + "/"}, "official_image_proxy": "http://old:7890"}
    settings = Settings.from_dict(cfg)
    assert settings.official_image_proxy == proxy and "secret" not in repr(settings)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    assert configured_proxy(str(path)) == proxy
    monkeypatch.setenv("NIKKE_TEST_PROXY", proxy)
    assert configured_proxy(proxy_env="NIKKE_TEST_PROXY") == proxy
    with pytest.raises(ValueError, match="为空"):
        configured_proxy(proxy_env="NIKKE_MISSING_TEST_PROXY")
    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())
    assert schema["base"]["items"]["official_image_proxy"]["secret"]


def test_only_official_images_use_proxy_and_every_redirect_is_rechecked():
    async def run():
        calls = []
        proxy = "http://user:secret@127.0.0.1:7890"

        class Content:
            async def iter_chunked(self, size):
                yield b"\x89PNG\r\n\x1a\noriginal"

        class Response:
            status, headers, content = 200, {}, Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                response = Response()
                if "redirect" in url:
                    response.status, response.headers = (
                        302,
                        {"Location": "https://i0.hdslb.com/a.png"},
                    )
                return response

            async def close(self):
                pass

        client = RSSClient(official_image_proxy=proxy)
        await client.session.close()
        client.session = Session()
        try:
            assert await client.image("https://sg-cdn.blablalink.com/a.png")
            assert calls[-1][1]["proxy"] == proxy
            assert await client.image("https://i0.hdslb.com/a.png")
            assert calls[-1][1]["proxy"] is None
            assert await client.image("https://sg-cdn.blablalink.com/redirect.png")
            assert calls[-2][1]["proxy"] == proxy and calls[-1][1]["proxy"] is None
            with pytest.raises(FeedError):
                await client.fetch("http://rsshub.local/feed")
            assert "proxy" not in calls[-1][1]
        finally:
            await client.close()

    asyncio.run(run())


def test_real_http_connect_authentication_and_407_redaction():
    async def run():
        received = []

        async def proxy_server(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                received.append(headers)
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(proxy_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = RSSClient(official_image_proxy=f"http://user:secret@127.0.0.1:{port}")
        try:
            with pytest.raises(FeedError, match="图片代理") as error:
                await client.image("https://sg-cdn.blablalink.com/a.png")
            assert "secret" not in str(error.value) and error.value.__suppress_context__
            assert "ClientHttpProxyError" in str(error.value) and "407" in str(error.value)
            assert received[0].startswith(b"CONNECT sg-cdn.blablalink.com:443 HTTP/1.1")
            assert b"Proxy-Authorization: Basic " + base64.b64encode(b"user:secret") in received[0]
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_transport_error_does_not_leak_authenticated_proxy_url(monkeypatch):
    async def run():
        client = RSSClient(official_image_proxy="http://user:secret@127.0.0.1:7890")

        async def fail(url):
            raise aiohttp.ClientConnectionError("http://user:secret@127.0.0.1:7890")

        monkeypatch.setattr(client, "_image", fail)
        try:
            with pytest.raises(FeedError) as error:
                await client.image("https://sg-cdn.blablalink.com/a.png")
            assert "secret" not in str(error.value)
            assert "ClientConnectionError" in str(error.value)
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError("secret"),
        aiohttp.ClientPayloadError("secret"),
        asyncio.TimeoutError(),
    ],
)
def test_transient_proxy_failure_retries_download_then_returns_original(monkeypatch, failure):
    async def run():
        client = RSSClient(official_image_proxy="http://user:secret@127.0.0.1:7890")
        image = AsyncMock(side_effect=[failure, failure, b"original-image"])
        sleep = AsyncMock()
        monkeypatch.setattr(client, "_image", image)
        monkeypatch.setattr("nikke.feed.asyncio.sleep", sleep)
        try:
            assert await client.image("https://sg-cdn.blablalink.com/a.png") == b"original-image"
            assert image.await_count == 3
            assert [c.args for c in sleep.call_args_list] == [(1,), (2,)]
        finally:
            await client.close()

    asyncio.run(run())


def test_connection_reset_is_bounded_and_reported_without_credentials(monkeypatch):
    async def run():
        client = RSSClient(official_image_proxy="http://user:secret@127.0.0.1:7890")
        key = SimpleNamespace(host="sg-cdn.blablalink.com", port=443, ssl=True)
        reset = aiohttp.ClientConnectorError(key, ConnectionResetError("private-secret"))
        image = AsyncMock(side_effect=reset)
        monkeypatch.setattr(client, "_image", image)
        monkeypatch.setattr("nikke.feed.asyncio.sleep", AsyncMock())
        try:
            with pytest.raises(FeedError) as error:
                await client.image("https://sg-cdn.blablalink.com/a.png")
            assert image.await_count == 3
            assert "ConnectionResetError" in str(error.value)
            assert "已尝试 3 次" in str(error.value)
            assert "secret" not in str(error.value)
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        FeedError("图片格式不支持"),
        FeedError("响应体超过大小限制"),
        aiohttp.ClientHttpProxyError(None, (), status=407),
        aiohttp.ClientConnectorSSLError(
            SimpleNamespace(host="sg-cdn.blablalink.com", port=443, ssl=True),
            ssl.SSLError("secret"),
        ),
    ],
)
def test_permanent_image_and_proxy_errors_do_not_retry(monkeypatch, failure):
    async def run():
        client = RSSClient(official_image_proxy="http://127.0.0.1:7890")
        image = AsyncMock(side_effect=failure)
        monkeypatch.setattr(client, "_image", image)
        try:
            with pytest.raises(FeedError):
                await client.image("https://sg-cdn.blablalink.com/a.png")
            assert image.await_count == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_all_image_attempts_share_deadline_and_support_cancellation(monkeypatch):
    async def run():
        client = RSSClient(official_image_proxy="http://127.0.0.1:7890")
        cancelled = []

        async def slow(url):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(client, "_image", slow)
        monkeypatch.setattr("nikke.feed.IMAGE_DOWNLOAD_TIMEOUT", 0.02)
        try:
            with pytest.raises(FeedError, match="TimeoutError"):
                await client.image("https://sg-cdn.blablalink.com/a.png")
            assert cancelled == [True]
            task = asyncio.create_task(client.image("https://sg-cdn.blablalink.com/a.png"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.close()

    asyncio.run(run())


def test_bilibili_direct_download_does_not_gain_proxy_retries(monkeypatch):
    async def run():
        client = RSSClient(official_image_proxy="http://127.0.0.1:7890")
        image = AsyncMock(side_effect=aiohttp.ClientConnectionError("secret"))
        monkeypatch.setattr(client, "_image", image)
        try:
            with pytest.raises(FeedError):
                await client.image("https://i0.hdslb.com/a.png")
            assert image.await_count == 1
        finally:
            await client.close()

    asyncio.run(run())
