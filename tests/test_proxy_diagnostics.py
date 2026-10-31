import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.diagnose_image_proxy import latest, plugin_probe, probe


def test_specific_post_diagnostics_never_select_unrelated_latest_image(monkeypatch):
    client = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[
                SimpleNamespace(id="blablalink:zh-TW:new", images=("latest",)),
                SimpleNamespace(id="blablalink:zh-TW:5482869474770001176", images=("target",)),
            ]
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr("scripts.diagnose_image_proxy.RSSClient", lambda: client)
    assert asyncio.run(latest("http://rss", "5482869474770001176")) == "target"
    with pytest.raises(ValueError):
        asyncio.run(latest("http://rss", "missing"))
    assert client.close.await_count == 2


def test_plugin_diagnostic_uses_public_download_with_retries(monkeypatch, capsys):
    client = SimpleNamespace(
        fetch=AsyncMock(),
        image=AsyncMock(return_value=b"image"),
        close=AsyncMock(),
        _image=AsyncMock(side_effect=AssertionError("must use retry wrapper")),
        session=SimpleNamespace(_request=AsyncMock()),
    )
    monkeypatch.setattr("scripts.diagnose_image_proxy.RSSClient", lambda **kwargs: client)
    assert (
        asyncio.run(
            plugin_probe(
                "http://user:secret@proxy:7890", "http://rss", "https://sg-cdn.blablalink.com/a.png"
            )
        )
        == 0
    )
    client.image.assert_awaited_once_with("https://sg-cdn.blablalink.com/a.png")
    client._image.assert_not_awaited()
    assert "secret" not in capsys.readouterr().out


def test_connect_failure_has_status_without_leaking_credentials(monkeypatch, capsys):
    sent = []
    response = iter(b"HTTP/1.1 407 Private secret response\r\nX-Secret: token\r\n\r\n")

    class Socket:
        closed = False

        def sendall(self, value):
            sent.append(value)

        def recv(self, size):
            return bytes([next(response)])

        def close(self):
            self.closed = True

    sock = Socket()
    monkeypatch.setattr(
        "scripts.diagnose_image_proxy.socket.create_connection", lambda *a, **k: sock
    )
    assert probe("http://user:secret@127.0.0.1:7890", "https://sg-cdn.blablalink.com/a.png") == 1
    output = capsys.readouterr().out
    assert '"http_status": 407' in output and '"stage": "proxy_tcp"' in output
    assert "secret" not in output and "token" not in output and "user:" not in output
    assert sent[0].startswith(b"CONNECT sg-cdn.blablalink.com:443 HTTP/1.1")
    assert sock.closed


def test_tcp_refusal_is_attributed_without_transport_text(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ConnectionRefusedError(111, "secret=private")

    monkeypatch.setattr("scripts.diagnose_image_proxy.socket.create_connection", fail)
    assert probe("http://127.0.0.1:7890", "https://sg-cdn.blablalink.com/a.png") == 1
    output = capsys.readouterr().out
    assert '"error_type": "ConnectionRefusedError"' in output and '"errno": 111' in output
    assert '"stage": "proxy_tcp"' in output and "secret" not in output
