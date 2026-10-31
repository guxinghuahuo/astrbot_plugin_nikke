"""Validate explicitly configured image proxy URLs without exposing credentials."""

from urllib.parse import urlsplit


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
