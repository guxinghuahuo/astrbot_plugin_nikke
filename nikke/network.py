"""Validate explicitly configured image proxy URLs without exposing credentials."""

import asyncio
import base64
import json
import re
import socket
import time
import traceback
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp


class ResponseTooLarge(ValueError):
    pass


def _redact_diagnostic(text: str) -> str:
    text = re.sub(r"(https?://)[^\s/@]+@", r"\1[凭据已隐藏]@", text)
    return re.sub(
        r'''(?i)((?:authorization|cookie|access_token|token|sessdata|bili_jct)[\s"']*[:=]\s*)'''
        r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&,}\r\n]+)''',
        r'\1"[凭据已隐藏]"', text,
    )


class OfficialRequestDiagnostics:
    """Capture a failed public API exchange through the entry point's logger."""

    def __init__(self, logger, label: str, url: str):
        self.logger, self.label, self.url = logger, label, url
        self.trace_id = uuid4().hex[:12]
        self.started = time.monotonic()
        self.stage = "连接/等待响应"
        self.status = None
        self.headers = {}
        self.body = bytearray()
        self.complete = False

    async def read(self, response, limit: int) -> bytes:
        self.status = response.status
        self.headers = {
            key: response.headers[key]
            for key in ("Content-Type", "Content-Length", "Server", "X-Request-Id", "Traceparent")
            if key in response.headers
        }
        self.stage = "读取响应"
        async for chunk in response.content.iter_chunked(65536):
            self.body.extend(chunk)
            if len(self.body) > limit:
                # Retain only the configured response limit, including on oversized errors.
                del self.body[limit:]
                raise ResponseTooLarge(
                    f"响应体超过 {limit} 字节上限，原始响应仅保留上限以内部分"
                )
        self.complete = True
        return bytes(self.body)

    def failure(self, exc: Exception):
        if self.logger is None:
            return
        context = {
            "id": self.trace_id, "api": self.label, "method": "POST", "url": self.url,
            "stage": self.stage, "seconds": round(time.monotonic() - self.started, 3),
            "http_status": self.status, "response_headers": self.headers,
            "received_bytes": len(self.body), "response_complete": self.complete,
            "response_received": self.status is not None,
        }
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.logger.warning(
            "[NIKKE] 官方接口故障 %s\n%s",
            _redact_diagnostic(json.dumps(context, ensure_ascii=False)), _redact_diagnostic(stack),
        )
        if self.status is None:
            self.logger.warning("[NIKKE] API原始响应 id=%s：未收到响应", self.trace_id)
            return
        try:
            body = _redact_diagnostic(self.body.decode("utf-8"))
            encoding = "utf-8"
        except UnicodeError:
            body = base64.b64encode(self.body).decode("ascii")
            encoding = "base64"
        # JSON escaping keeps control characters visible and prevents forged log lines.
        chunks = [body[i:i + 4096] for i in range(0, len(body), 4096)] or [""]
        for index, chunk in enumerate(chunks, 1):
            self.logger.warning(
                "[NIKKE] API原始响应 id=%s part=%d/%d encoding=%s data=%s",
                self.trace_id, index, len(chunks), encoding, json.dumps(chunk, ensure_ascii=False),
            )


def request_error_reason(exc: Exception) -> str:
    """Classify failures without exposing URLs, response bodies or credentials."""
    if isinstance(exc, ResponseTooLarge):
        reason = "响应体超过大小上限（原始响应仅记录上限以内部分）"
    elif isinstance(exc, asyncio.TimeoutError):
        reason = "请求超时（连接或读取响应超时）"
    elif isinstance(exc, aiohttp.ClientConnectorCertificateError):
        reason = "TLS 证书验证失败"
    elif isinstance(exc, aiohttp.ClientSSLError):
        reason = "TLS 握手失败"
    elif isinstance(exc, aiohttp.ClientConnectorError) and isinstance(
        exc.os_error, socket.gaierror
    ):
        reason = "DNS 解析失败"
    elif isinstance(exc, aiohttp.ClientConnectionError):
        reason = "网络连接失败或中断"
    elif isinstance(exc, aiohttp.ClientPayloadError):
        reason = "响应体传输失败"
    elif isinstance(exc, aiohttp.ClientError):
        reason = "HTTP 客户端请求失败"
    elif isinstance(exc, UnicodeError):
        reason = "响应字符编码无效"
    else:
        reason = "响应 JSON 或内容解析失败"
    return f"{reason}（{type(exc).__name__}）"


def image_proxy_url(value: str) -> str:
    message = "国际服图片代理须为 HTTP 代理地址，例如 http://192.168.0.2:7890"
    if not isinstance(value, str):
        raise ValueError(message)
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            len(value) > 4096
            or any(c.isspace() for c in value)
            or parsed.scheme != "http"
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    return value.rstrip("/")
