"""Both official callers must distinguish transport and parsing failures safely."""

import asyncio
import base64
import json
import logging
import socket
import ssl
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from nikke import blablalink
from nikke.calendar import MODULES, CalendarClient
from nikke.feed import FeedError, RSSClient


def test_proxy_fallback_sends_official_request_via_local_proxy(monkeypatch):
    async def scenario():
        received = []

        async def proxy(request):
            received.append((request.raw_path, request.headers["x-language"], await request.json()))
            auth_header = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
            assert request.headers["Proxy-Authorization"] == auth_header
            assert "Authorization" not in request.headers
            return web.json_response({"code": 0, "data": {"list": []}})

        app = web.Application()
        app.router.add_post("/official", proxy)
        runner = web.AppRunner(app)
        await runner.setup()
        # Reserve a port without listening so the initial direct connection is refused.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            port = unavailable.getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}/official"
            monkeypatch.setattr(blablalink, "ENDPOINT", endpoint)
            try:
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                proxy_port = site._server.sockets[0].getsockname()[1]
                client = RSSClient(official_api_proxy=f"http://user:pass@127.0.0.1:{proxy_port}")
                try:
                    assert await client.fetch("blablalink://official/ko") == []
                finally:
                    await client.close()
                assert received == [(endpoint, "ko", blablalink.request_data("ko")[1])]
            finally:
                await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["dynamics", "calendar"])
@pytest.mark.parametrize(
    "mode",
    [
        "direct", "timeout", "disconnected", "both_fail", "disabled",
        "tls", "http", "json", "read", "cancel",
    ],
)
def test_official_proxy_fallback_is_limited_to_pre_response_network_failure(
    monkeypatch, caplog, source, mode
):
    proxy = "http://user:private-secret@127.0.0.1:7890"
    logger = logging.getLogger("official-proxy-fallback")
    requests = []
    payload = {"code": 0, "data": (
        {"list": []} if source == "dynamics" else {key: {"items": []} for key in MODULES}
    )}

    class Response:
        headers = {}

        def __init__(self, proxied):
            self.proxied = proxied
            self.status = 503 if mode == "http" else 200

        async def __aenter__(self):
            if self.proxied:
                if mode == "both_fail":
                    raise aiohttp.ServerDisconnectedError(f"proxy connection failed: {proxy}")
            elif mode in {"timeout", "both_fail", "disabled"}:
                raise asyncio.TimeoutError("TCP/TLS connection timed out")
            elif mode == "disconnected":
                raise aiohttp.ServerDisconnectedError("connection closed")
            elif mode == "tls":
                key = SimpleNamespace(host="api.blablalink.com", port=443, ssl=True)
                raise aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError("invalid"))
            elif mode == "cancel":
                raise asyncio.CancelledError()
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            if mode == "read":
                raise asyncio.TimeoutError("response body timed out")
            yield b"<html>" if mode == "json" else json.dumps(payload).encode()

    def post(session, url, **kwargs):
        requests.append((url, kwargs))
        return Response(bool(kwargs.get("proxy")))

    async def scenario():
        monkeypatch.setattr(aiohttp.ClientSession, "post", post)
        fallback = "" if mode == "disabled" else proxy
        client = RSSClient(official_api_proxy=fallback, logger=logger)
        try:
            call = (
                client.fetch("blablalink://official/ko") if source == "dynamics"
                else CalendarClient(logger=logger, fallback_proxy=fallback).fetch()
            )
            if mode in {"direct", "timeout", "disconnected"}:
                result = await call
                assert result == ([] if source == "dynamics" else (payload, []))
            else:
                error = asyncio.CancelledError if mode == "cancel" else FeedError
                with pytest.raises(error) as raised:
                    await call
                assert "private-secret" not in str(raised.value)
        finally:
            await client.close()

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        asyncio.run(scenario())
    assert requests[0][1].get("proxy") is None
    if mode in {"timeout", "disconnected", "both_fail"}:
        first, second = requests
        assert second == (first[0], {**first[1], "proxy": proxy})
        assert "官方接口切换代理重试" in caplog.text
    else:
        assert len(requests) == 1
    assert "private-secret" not in caplog.text
    if mode == "both_fail":
        contexts = [
            json.loads(line.split("\n", 1)[0].removeprefix("[NIKKE] 官方接口故障 "))
            for line in (record.getMessage() for record in caplog.records)
            if line.startswith("[NIKKE] 官方接口故障 ")
        ]
        assert [context["route"] for context in contexts] == ["direct", "proxy"]
        assert contexts[0]["id"] == contexts[1]["id"]


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
    logger = logging.getLogger("official-api-diagnostics")
    caplog.set_level(logging.DEBUG, logger=logger.name)
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
    records = [r for r in caplog.records if r.name == logger.name]
    warning, = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    reason = {
        "http": "HTTP 503", "timeout": "请求超时", "partial": "传输失败",
        "json": "解析失败", "encoding": "编码无效", "business": "官方",
    }[mode]
    assert reason in warning and len(warning.splitlines()) == 1
    assert "Traceback" not in warning and "API原始响应" not in warning
    messages = [r.getMessage() for r in records if r.levelno == logging.DEBUG]
    context_text, stack = messages[0].split("\n", 1)
    context = json.loads(context_text.removeprefix("[NIKKE] 官方接口故障 "))
    assert context["id"] in warning
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


def test_timeout_with_debug_disabled_emits_one_short_warning(caplog, monkeypatch):
    from unittest.mock import Mock

    from nikke.network import OfficialRequestDiagnostics

    logger = logging.getLogger("concise-api-warning")
    caplog.set_level(logging.WARNING, logger=logger.name)
    format_stack = Mock()
    monkeypatch.setattr("nikke.network.traceback.format_exception", format_stack)
    diagnostic = OfficialRequestDiagnostics(logger, "官方动态/ko", "https://api.blablalink.com")
    diagnostic.failure(asyncio.TimeoutError("private-network-details"))
    warning, = [record for record in caplog.records if record.name == logger.name]
    assert warning.levelno == logging.WARNING
    assert "官方动态/ko 直连失败：请求超时" in warning.getMessage()
    assert diagnostic.trace_id in warning.getMessage()
    assert "private-network-details" not in warning.getMessage()
    format_stack.assert_not_called()


def test_diagnostic_redacts_credentials_and_marks_size_limit(caplog):
    from nikke.network import OfficialRequestDiagnostics, ResponseTooLarge

    logger = logging.getLogger("bounded-api-diagnostics")
    caplog.set_level(logging.DEBUG, logger=logger.name)

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
