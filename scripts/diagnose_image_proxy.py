"""Read-only staged probe: proxy TCP, CONNECT, TLS and an image range request."""

import argparse
import asyncio
import base64
import json
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from nikke.feed import FeedError, RSSClient  # noqa: E402
from nikke.network import image_proxy_url  # noqa: E402


def emit(stage, **values):
    print(json.dumps({"stage": stage, **values, "messages_sent": 0}), flush=True)


def headers(sock):
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Incomplete HTTP headers")
        data.extend(chunk)
        if len(data) > 65536:
            raise ValueError("Headers exceed limit")
    status = bytes(data).split(b"\r\n", 1)[0].split(b" ")
    return int(status[1])


def probe(proxy, image, timeout=20):
    proxy = image_proxy_url(proxy)
    target, endpoint = urlsplit(image), urlsplit(proxy)
    if not proxy:
        raise ValueError("No configured image proxy")
    if target.scheme != "https" or target.hostname != "sg-cdn.blablalink.com":
        raise ValueError("Unsupported image target")
    emit(
        "environment",
        python=sys.version.split()[0],
        aiohttp=aiohttp.__version__,
        openssl=ssl.OPENSSL_VERSION,
        proxy_host=endpoint.hostname,
        proxy_port=endpoint.port or 80,
        proxy_authenticated=bool(endpoint.username or endpoint.password),
        timeout_seconds=timeout,
    )
    sock = None
    stage = "proxy_tcp"
    started = time.monotonic()
    try:
        sock = socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=timeout)
        emit(stage, ok=True, seconds=round(time.monotonic() - started, 3))
        stage, started = "proxy_connect", time.monotonic()
        request = f"CONNECT {target.hostname}:443 HTTP/1.1\r\nHost: {target.hostname}:443\r\n"
        if endpoint.username is not None:
            auth = f"{unquote(endpoint.username)}:{unquote(endpoint.password or '')}"
            request += (
                "Proxy-Authorization: Basic "
                + base64.b64encode(auth.encode("latin1")).decode("ascii")
                + "\r\n"
            )
        sock.sendall((request + "\r\n").encode("ascii"))
        status = headers(sock)
        emit(
            stage,
            ok=status == 200,
            http_status=status,
            seconds=round(time.monotonic() - started, 3),
        )
        if status != 200:
            return 1
        stage, started = "tls", time.monotonic()
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
        emit(stage, ok=True, seconds=round(time.monotonic() - started, 3), version=sock.version())
        stage, started = "image_http", time.monotonic()
        path = target.path + ("?" + target.query if target.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {target.hostname}\r\n"
            "Range: bytes=0-511\r\nReferer: https://www.blablalink.com/\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        status = headers(sock)
        emit(
            stage,
            ok=status in (200, 206),
            http_status=status,
            seconds=round(time.monotonic() - started, 3),
        )
        return 0 if status in (200, 206) else 1
    except Exception as exc:
        # Never print exception text, headers, certificates or authenticated URLs.
        info = {
            "ok": False,
            "error_type": type(exc).__name__,
            "seconds": round(time.monotonic() - started, 3),
        }
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            info["errno"] = exc.errno
        if isinstance(exc, ssl.SSLCertVerificationError):
            info["certificate_verify_code"] = exc.verify_code
        emit(stage, **info)
        return 1
    finally:
        if sock is not None:
            sock.close()


async def latest(base, post_id=None):
    client = RSSClient()
    try:
        entries = await client.fetch(base.rstrip("/") + "/nikke-blablalink/official/zh-TW")
        image = next(
            (
                url
                for item in entries
                if post_id is None or item.id.endswith(":" + post_id)
                for url in item.images
            ),
            None,
        )
        if not image:
            raise ValueError("No image in RSS")
        emit("rss", ok=True, entries=len(entries))
        return image
    finally:
        await client.close()


async def aiohttp_probes(proxy, image):
    """Compare aiohttp's default ALPN with the verified stdlib TLS context."""
    results = []
    for mode in ("default_tls", "stdlib_tls"):
        stage = "aiohttp_" + mode
        start = time.monotonic()
        trace = aiohttp.TraceConfig()

        async def begin(*args):
            emit(stage + "_connection", state="start")

        async def connected(*args):
            emit(stage + "_connection", state="ready", seconds=round(time.monotonic() - start, 3))

        trace.on_connection_create_start.append(begin)
        trace.on_connection_create_end.append(connected)
        context = ssl.create_default_context() if mode == "stdlib_tls" else True
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=context),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=35, connect=20, sock_read=10),
                cookie_jar=aiohttp.DummyCookieJar(),
                trace_configs=[trace],
            ) as session:
                async with session.get(
                    image,
                    proxy=proxy,
                    allow_redirects=False,
                    headers={
                        "Range": "bytes=0-511",
                        "Referer": "https://www.blablalink.com/",
                        "User-Agent": "AstrBot-Nikke-RSS/0.1",
                    },
                ) as response:
                    emit(stage + "_headers", http_status=response.status)
                    body = await response.content.read(512)
                    ok = response.status in (200, 206) and bool(body)
                    emit(
                        stage,
                        ok=ok,
                        bytes_read=len(body),
                        seconds=round(time.monotonic() - start, 3),
                    )
                    results.append(ok)
        except Exception as exc:
            info = {
                "ok": False,
                "error_type": type(exc).__name__,
                "seconds": round(time.monotonic() - start, 3),
            }
            if isinstance(getattr(exc, "status", None), int):
                info["http_status"] = exc.status
            os_error = getattr(exc, "os_error", None)
            if isinstance(getattr(os_error, "errno", None), int):
                info["errno"] = os_error.errno
            emit(stage, **info)
            results.append(False)
    return 0 if all(results) else 1


async def plugin_probe(proxy, base, image):
    """Run the exact plugin path and retain safe transport exception metadata."""
    client = RSSClient(official_image_proxy=proxy)
    stage = "plugin_full_image"
    start = time.monotonic()
    original_request = client.session._request

    async def request(method, url, **kwargs):
        emit(
            stage + "_request",
            host=urlsplit(str(url)).hostname,
            proxy_selected=kwargs.get("proxy") == proxy,
            connect_timeout=getattr(kwargs.get("timeout"), "connect", None),
        )
        try:
            response = await original_request(method, url, **kwargs)
        except Exception as exc:
            emit(
                stage + "_connection_error",
                error_type=type(exc).__name__,
                underlying_error_type=type(getattr(exc, "os_error", None)).__name__,
            )
            raise
        emit(
            stage + "_headers",
            http_status=response.status,
            content_length=response.content_length,
            seconds=round(time.monotonic() - start, 3),
        )
        return response

    try:
        await client.fetch(base.rstrip("/") + "/nikke-blablalink/official/zh-TW")
        client.session._request = request
        data = await client.image(image)
        emit(stage, ok=True, image_bytes=len(data), seconds=round(time.monotonic() - start, 3))
        return 0
    except Exception as exc:
        info = {
            "ok": False,
            "error_type": type(exc).__name__,
            "seconds": round(time.monotonic() - start, 3),
        }
        if isinstance(exc, FeedError):
            info["error"] = str(exc)
        if isinstance(getattr(exc, "status", None), int):
            info["http_status"] = exc.status
        os_error = getattr(exc, "os_error", None)
        if os_error is not None:
            info["underlying_error_type"] = type(os_error).__name__
        if isinstance(getattr(os_error, "errno", None), int):
            info["errno"] = os_error.errno
        emit(stage, **info)
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--rsshub", default="http://localhost:1200")
    parser.add_argument("--post-id", help="只检查指定帖子的同帖繁中原图；默认最新有图动态")
    args = parser.parse_args()
    try:
        saved = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        proxy = image_proxy_url(saved.get("base", {}).get("official_image_proxy", ""))
        image = asyncio.run(latest(args.rsshub, args.post_id))
        result = probe(proxy, image)
        if result == 0:
            result = asyncio.run(aiohttp_probes(proxy, image))
        # A raw handshake can fail transiently; still exercise the actual bounded
        # retry path so the report distinguishes a recovered download from failure.
        plugin_result = asyncio.run(plugin_probe(proxy, args.rsshub, image))
        result = max(result, plugin_result)
    except Exception as exc:
        emit("setup", ok=False, error_type=type(exc).__name__)
        result = 1
    raise SystemExit(result)
