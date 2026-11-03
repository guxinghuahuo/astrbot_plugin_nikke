import asyncio
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
from defusedxml import ElementTree as ET

from .network import image_proxy_url

MAX_FEED_BYTES = 4 * 1024 * 1024
# Original official artwork can exceed 16 MiB; download/send one image at a time.
MAX_IMAGE_BYTES = 64 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 90
INTERNATIONAL_IMAGE_HOSTS = {"sg-cdn.blablalink.com", "i.ytimg.com"}


def post_images(ident, text, images):
    """Recover a YouTube cover for older official RSS/archive entries only."""
    if images or not isinstance(ident, str) or not ident.startswith("blablalink:"):
        return tuple(images)
    covers = []
    for url in re.findall(r"https?://[^\s<>]+", text):
        try:
            parts = urlsplit(url.rstrip(".,;!?)]}，。！？；）】"))
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if parts.username or parts.password:
            continue
        if host == "youtu.be":
            video = parts.path.strip("/")
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            path = parts.path.strip("/").split("/")
            video = (
                parse_qs(parts.query).get("v", [""])[0]
                if path == ["watch"]
                else path[1]
                if len(path) == 2 and path[0] in ("shorts", "embed")
                else ""
            )
        else:
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            cover = f"https://i.ytimg.com/vi_webp/{video}/maxresdefault.webp"
            if cover not in covers:
                covers.append(cover)
    return tuple(covers[:30])


class FeedError(Exception):
    """Short diagnostics without response bodies or credentials."""


def web_url(value: str, base: str = "") -> str:
    if not value.strip():
        return ""
    value = urljoin(base, value.strip())
    parts = urlsplit(value)
    if parts.scheme in ("https", "http") and parts.hostname and not parts.username:
        return value
    return ""


class HTMLContent(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.text: list[str] = []
        self.images: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("br", "p", "div", "li", "blockquote"):
            self.text.append("\n")
        if tag == "img":
            url = web_url(attrs.get("src") or attrs.get("data-src") or "", self.base)
            if url and url not in self.images:
                self.images.append(url)
        if tag == "a":
            url = web_url(attrs.get("href") or "", self.base)
            if url and url not in self.links:
                self.links.append(url)
        if tag == "iframe":
            parts = urlsplit(web_url(attrs.get("src") or "", self.base))
            bvid = parse_qs(parts.query).get("bvid", [""])[0]
            if parts.hostname == "player.bilibili.com" and re.fullmatch(r"BV\w+", bvid):
                self.links.append(f"https://www.bilibili.com/video/{bvid}")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in ("script", "style"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ("p", "div", "li", "blockquote"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)

    def plain(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.text)).strip()


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    text: str
    link: str
    author: str
    published: float | None
    images: tuple[str, ...]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(
            **{
                **value,
                "images": post_images(
                    value.get("id", ""), value.get("text", ""), value.get("images", [])
                ),
            }
        )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element, *names):
    for name in names:
        for child in element:
            if local_name(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def publication(value: str) -> float | None:
    if not value:
        return None
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    try:
        return date.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_feed(data: bytes) -> list[Entry]:
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("RSS 内容超过 4 MiB，已停止处理")
    try:
        root = ET.fromstring(data)
    except Exception as exc:
        raise FeedError("响应不是有效的安全 XML RSS/Atom") from exc
    kind = local_name(root.tag)
    if kind == "rss":
        channel = next((e for e in root if local_name(e.tag) == "channel"), None)
        if channel is None:
            raise FeedError("RSS 缺少 channel")
        nodes = [e for e in channel if local_name(e.tag) == "item"]
        feed_author = child_text(channel, "title")
    elif kind == "feed":
        nodes = [e for e in root if local_name(e.tag) == "entry"]
        feed_author = child_text(root, "title")
    else:
        raise FeedError("响应不是 RSS/Atom；可能是 RSSHub 错误页面")
    if len(nodes) > 500:
        raise FeedError("单次 RSS 超过 500 条，已停止处理以避免截断遗漏")
    entries, ids = [], set()
    for node in nodes:
        link = child_text(node, "link")
        if kind == "feed":
            link = next(
                (
                    e.get("href", "")
                    for e in node
                    if local_name(e.tag) == "link" and e.get("rel", "alternate") == "alternate"
                ),
                "",
            )
        link = web_url(link)
        ident = child_text(node, "guid", "id") or link
        if not ident or not link:
            raise FeedError("RSS 条目缺少稳定标识或原链接，未更新基线")
        if ident in ids:
            continue
        ids.add(ident)
        content = HTMLContent(link)
        content.feed(child_text(node, "encoded", "content", "description", "summary"))
        title_parser = HTMLContent(link)
        title_parser.feed(child_text(node, "title"))
        text = content.plain()
        for href in content.links:
            if href != link and href not in text:
                text += "\n" + href
        for child in node:
            if local_name(child.tag) in ("content", "thumbnail", "enclosure"):
                if (
                    child.get("type", "").startswith("image/")
                    or local_name(child.tag) == "thumbnail"
                ):
                    url = web_url(child.get("url", ""), link)
                    if url and url not in content.images:
                        content.images.append(url)
        entries.append(
            Entry(
                ident,
                title_parser.plain()[:1000],
                text[:30000],
                link,
                child_text(node, "creator", "author") or feed_author,
                publication(child_text(node, "pubDate", "published", "updated")),
                post_images(ident, text[:30000], content.images[:30]),
            )
        )
    return entries


class RSSClient:
    def __init__(self, *, official_image_proxy: str = ""):
        self.official_image_proxy = image_proxy_url(official_image_proxy)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=10),
            headers={"User-Agent": "AstrBot-Nikke-RSS/0.1"},
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )

    async def close(self):
        await self.session.close()

    async def fetch(self, url: str) -> list[Entry]:
        if url.startswith("blablalink:"):
            from .blablalink import fetch_official

            return await fetch_official(self, url)
        try:
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise FeedError(
                        f"RSSHub HTTP {response.status}；检查 RSSHub 路由及上游接口日志"
                    )
                body = await self._read(response, MAX_FEED_BYTES)
            return await asyncio.to_thread(parse_feed, body)
        except FeedError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise FeedError("RSSHub 连接失败或请求超时") from exc

    @staticmethod
    async def _read(response, limit: int) -> bytes:
        result = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            result.extend(chunk)
            if len(result) > limit:
                raise FeedError(f"响应体超过大小限制（上限 {limit / 1024 / 1024:g} MiB）")
        return bytes(result)

    async def image(self, url: str) -> bytes:
        attempts = 0
        proxied = bool(
            self.official_image_proxy and urlsplit(url).hostname in INTERNATIONAL_IMAGE_HOSTS
        )

        async def download():
            nonlocal attempts
            for attempt in range(3 if proxied else 1):
                attempts += 1
                try:
                    return await self._image(url)
                except (
                    aiohttp.ClientConnectionError,
                    aiohttp.ClientPayloadError,
                    asyncio.TimeoutError,
                ) as exc:
                    if (
                        not proxied
                        or attempt == 2
                        or isinstance(
                            exc, (aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch)
                        )
                    ):
                        raise
                    # Retry only the download, never the QQ send. Keep TLS verification
                    # and the configured proxy; all attempts share one total deadline.
                    await asyncio.sleep(attempt + 1)

        try:
            return await asyncio.wait_for(download(), timeout=IMAGE_DOWNLOAD_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # aiohttp errors may include the authenticated proxy URL or headers.
            reason = type(exc).__name__
            if isinstance(exc, aiohttp.ClientHttpProxyError):
                reason += f"，CONNECT HTTP {exc.status}"
            os_error = getattr(exc, "os_error", None)
            if os_error is not None:
                reason += "，底层=" + type(os_error).__name__
            if type(getattr(os_error, "errno", None)) is int:
                reason += f"，errno={os_error.errno}"
            if attempts > 1:
                reason += f"，已尝试 {attempts} 次"
            hint = (
                "已使用国际服图片代理，请检查代理及其上游连接"
                if proxied
                else "请检查 NAS 到图片 CDN 的连通性"
            )
            raise FeedError(f"图片网络连接失败或超时（{reason}）；" + hint) from None

    async def _image(self, url: str) -> bytes:
        # Validate each redirect; feed HTML must not request arbitrary/NAS hosts.
        for _ in range(4):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            bilibili = any(
                host == h or host.endswith("." + h) for h in ("hdslb.com", "bilibili.com")
            )
            official = host in INTERNATIONAL_IMAGE_HOSTS
            if (
                parts.scheme not in ("https", "http")
                or parts.username
                or parts.password
                or parts.port not in (None, 80, 443)
                or not (bilibili or official)
            ):
                raise FeedError("图片域名不在 bilibili、BlaBlaLink 或 YouTube 缩略图 CDN 范围")
            # Bilibili puts thumbnail/format transforms after the original extension.
            if bilibili:
                url = parts._replace(
                    path=re.sub(r"(?i)(\.(?:jpg|jpeg|png|gif|webp))@[^/]*$", r"\1", parts.path)
                ).geturl()
            async with self.session.get(
                url,
                proxy=self.official_image_proxy if official and self.official_image_proxy else None,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=90, connect=10),
                headers={
                    "Referer": "https://www.bilibili.com/"
                    if bilibili
                    else "https://www.youtube.com/"
                    if host == "i.ytimg.com"
                    else "https://www.blablalink.com/"
                },
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status == 404 and host == "i.ytimg.com":
                    match = re.fullmatch(
                        r"/vi_webp/([A-Za-z0-9_-]{11})/maxresdefault\.webp", parts.path
                    )
                    if match:
                        url = f"https://i.ytimg.com/vi/{match[1]}/hqdefault.jpg"
                        continue
                if response.status != 200:
                    raise FeedError(f"图片下载失败（HTTP {response.status}）")
                data = await self._read(response, MAX_IMAGE_BYTES)
                valid = (
                    data.startswith(b"\xff\xd8\xff")
                    or data.startswith(b"\x89PNG\r\n\x1a\n")
                    or data.startswith((b"GIF87a", b"GIF89a"))
                    or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
                )
                if not valid:
                    raise FeedError("图片格式不支持")
                return data
        raise FeedError("图片重定向次数过多")
