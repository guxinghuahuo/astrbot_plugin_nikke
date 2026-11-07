"""Both official callers must distinguish transport and parsing failures safely."""

import asyncio
import json
import logging
import socket
import ssl
from types import SimpleNamespace

import aiohttp
import pytest

from nikke.calendar import CalendarClient
from nikke.feed import FeedError, RSSClient


@pytest.mark.parametrize("source", ["dynamics", "calendar"])
@pytest.mark.parametrize("kind,expected", [
    ("dns", "DNS 解析失败"),
    ("timeout", "请求超时"),
    ("tls", "TLS 证书验证失败"),
    ("connection", "网络连接失败或中断"),
    ("payload", "响应体传输失败"),
    ("json", "响应 JSON 或内容解析失败"),
    ("encoding", "响应字符编码无效"),
    ("cancel", None),
])
def test_official_failures_remain_actionable_and_do_not_leak(monkeypatch, source, kind, expected):
    key = SimpleNamespace(host="api.blablalink.com", port=443, ssl=True)
    secret = "https://user:private-secret@proxy.invalid/?token=private-token"
    errors = {
        "dns": aiohttp.ClientConnectorError(key, socket.gaierror(-2, secret)),
        "timeout": asyncio.TimeoutError(secret),
        "tls": aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError(secret)),
        "connection": aiohttp.ServerDisconnectedError(secret),
        "payload": aiohttp.ClientPayloadError(secret),
        "json": json.JSONDecodeError("bad", secret, 0),
        "encoding": UnicodeDecodeError("utf-8", b"\xff", 0, 1, secret),
        "cancel": asyncio.CancelledError(),
    }
    failure = errors[kind]

    class BrokenResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        async def __aenter__(self):
            if kind not in {"json", "encoding"}:
                raise failure
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"\xff" if kind == "encoding" else secret.encode()

    async def scenario():
        monkeypatch.setattr(aiohttp.ClientSession, "post", lambda *a, **kw: BrokenResponse())
        client = RSSClient()
        try:
            call = (
                client.fetch("blablalink://official/en")
                if source == "dynamics" else CalendarClient().fetch()
            )
            if kind == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await call
            else:
                with pytest.raises(FeedError, match=expected) as error:
                    await call
                assert "private-secret" not in str(error.value)
                assert "private-token" not in str(error.value)
                assert error.value.__cause__ is not None
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["dynamics", "calendar"])
@pytest.mark.parametrize("mode", ["http", "json", "business", "partial", "encoding", "timeout"])
def test_failed_api_logs_context_stack_and_reconstructable_response(
    monkeypatch, caplog, source, mode
):
    import base64

    logger = logging.getLogger("official-api-diagnostics")
    caplog.set_level(logging.WARNING, logger=logger.name)
    body = {
        "http": b"upstream service unavailable",
        "json": ("invalid JSON\x0b原始正文" * 1200).encode(),
        "business": b'{"code":123,"data":null}',
        "partial": b"partially received response",
        "encoding": b"\xff\xfe\x00",
        "timeout": b"",
    }[mode]

    class Response:
        status = 503 if mode == "http" else 200
        headers = {"Content-Type": "text/plain", "X-Request-Id": "upstream-request-123"}

        async def __aenter__(self):
            if mode == "timeout":
                raise asyncio.TimeoutError("waiting for upstream")
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield body
            if mode == "partial":
                raise aiohttp.ClientPayloadError("incomplete transfer")

    async def scenario():
        monkeypatch.setattr(aiohttp.ClientSession, "post", lambda *a, **kw: Response())
        client = RSSClient(logger=logger)
        try:
            with pytest.raises(FeedError):
                if source == "dynamics":
                    await client.fetch("blablalink://official/en")
                else:
                    await CalendarClient(logger=logger).fetch()
        finally:
            await client.close()

    asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records if r.name == logger.name]
    context_text, stack = messages[0].split("\n", 1)
    context = json.loads(context_text.removeprefix("[NIKKE] 官方接口故障 "))
    assert context["method"] == "POST"
    assert context["seconds"] >= 0
    assert "Traceback" in stack
    assert context["response_complete"] == (mode not in {"timeout", "partial"})
    assert context["received_bytes"] == len(body)
    if mode == "timeout":
        assert context["http_status"] is None
        assert "未收到响应" in messages[1]
    else:
        assert context["http_status"] == Response.status
        assert context["response_headers"]["X-Request-Id"] == "upstream-request-123"
        parts = [m.split(" data=", 1) for m in messages[1:]]
        assert all(context["id"] in prefix for prefix, _ in parts)
        raw = "".join(json.loads(chunk) for _, chunk in parts)
        assert (base64.b64decode(raw) if mode == "encoding" else raw.encode()) == body


def test_diagnostic_redacts_credentials_and_marks_size_limit(caplog):
    from nikke.network import OfficialRequestDiagnostics, ResponseTooLarge

    logger = logging.getLogger("bounded-api-diagnostics")
    caplog.set_level(logging.WARNING, logger=logger.name)

    class Response:
        status = 200
        headers = {}

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"x" * 100

    async def scenario():
        diagnostic = OfficialRequestDiagnostics(logger, "test", "https://api.blablalink.com")
        try:
            await diagnostic.read(Response(), 12)
        except ResponseTooLarge as exc:
            diagnostic.failure(exc)
        assert len(diagnostic.body) == 12 and not diagnostic.complete
        try:
            raise ConnectionError("https://user:private-secret@proxy.invalid/?token=private-token")
        except ConnectionError as exc:
            diagnostic.failure(exc)

    asyncio.run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ResponseTooLarge" in text and "上限" in text
    assert "private-secret" not in text and "private-token" not in text
