"""Official API transport, normalized through the same RSS parser as the private route."""

import asyncio
import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

import aiohttp

from .config import OFFICIAL_LANGUAGES, OFFICIAL_OPENID
from .feed import MAX_FEED_BYTES, FeedError, parse_feed, web_url

ENDPOINT = (
    "https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/GetUserPostList"
)


def request_data(language):
    common = {
        "game_id": "16", "area_id": "global", "source": "pc_web", "intl_game_id": "29080",
        "env": "prod", "language": language, "data_statistics_scene": "outer",
        "data_statistics_lang": language, "data_statistics_page_id": "https://www.blablalink.com/",
        "data_statistics_client_type": "pc_web",
    }
    return {
        "accept": "application/json, text/plain, */*", "content-type": "application/json",
        "origin": "https://www.blablalink.com", "referer": "https://www.blablalink.com/",
        "x-language": language, "x-channel-type": "2",
        "x-common-params": json.dumps(common, separators=(",", ":")),
    }, {"plate_id": 0, "order_by": 1, "limit": "10", "intl_openid": OFFICIAL_OPENID,
        "page_type": 0}


def post_date(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        return None
    seconds = int(value)
    return seconds if 946684800 <= seconds < 4102444800 else None


def normalize(payload, language):
    if language not in OFFICIAL_LANGUAGES:
        raise FeedError("国际服语言无效")
    if (
        not isinstance(payload, dict) or type(payload.get("code")) is not int
        or payload["code"] != 0 or not isinstance(payload.get("data"), dict)
        or not isinstance(payload["data"].get("list"), list)
        or len(payload["data"]["list"]) > 500
    ):
        raise FeedError("官方动态响应结构或业务状态无效")
    items = {}
    for post in payload["data"]["list"]:
        if not isinstance(post, dict):
            raise FeedError("官方动态条目无效")
        if any(type(post.get(k)) is int and post[k] == 1 for k in ("is_del", "is_hide")):
            continue
        if (
            post.get("intl_openid") != OFFICIAL_OPENID
            or type(post.get("is_official")) is not int or post["is_official"] != 1
            or post.get("language") != language
            or not isinstance(post.get("post_uuid"), str)
            or not re.fullmatch(r"[0-9]{1,32}", post["post_uuid"])
            or not isinstance(post.get("title"), str)
            or not isinstance(post.get("content"), str)
            or not isinstance(post.get("pic_urls"), list)
            or not isinstance(post.get("user"), dict)
            or not isinstance(post["user"].get("username"), str)
        ):
            raise FeedError("官方动态身份、语言或内容无效")
        description = post["content"]
        for image in post["pic_urls"]:
            if not isinstance(image, str) or not web_url(image):
                raise FeedError("官方图片地址无效")
            if image not in description and escape(image, quote=True) not in description:
                description += f'<p><img src="{escape(image, quote=True)}"></p>'
        if post.get("type") == 3 and post.get("ext_info"):
            try:
                videos = json.loads(post["ext_info"])
                if not isinstance(videos, list):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise FeedError("官方视频元数据无效") from exc
            for video in videos:
                video = video if isinstance(video, dict) else {}
                cover = video.get("video_cover", "")
                cover = web_url(cover) if isinstance(cover, str) else ""
                if cover and cover not in description and escape(cover) not in description:
                    description += f'<p><img src="{escape(cover)}"></p>'
                url = video.get("video_url", "")
                url = web_url(url) if isinstance(url, str) else ""
                if url and url not in description and escape(url) not in description:
                    title = video.get("video_title") or "视频原链接"
                    description += f'<p><a href="{escape(url)}">{escape(str(title))}</a></p>'
        ident = f"blablalink:{OFFICIAL_OPENID}:{language}:{post['post_uuid']}"
        item = {
            "title": post["title"], "author": post["user"]["username"], "guid": ident,
            "link": "https://www.blablalink.com/post/detail?"
            f"post_uuid={post['post_uuid']}&lang={language}",
            "description": description,
            "published": post_date(post.get("publish_on")) or post_date(post.get("created_on")),
        }
        if ident in items and items[ident] != item:
            raise FeedError("官方动态重复 ID 的内容冲突")
        items[ident] = item
    # Reuse HTML/link/image normalization, limits and video fallback from the RSS path.
    root = Element("rss", version="2.0")
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = f"NIKKE 国际服 · {OFFICIAL_LANGUAGES[language]}"
    for item in sorted(items.values(), key=lambda v: (-(v["published"] or 0), v["guid"])):
        node = SubElement(channel, "item")
        for key, value in item.items():
            if key != "published":
                SubElement(node, key).text = value
        if item["published"] is not None:
            SubElement(node, "pubDate").text = format_datetime(
                datetime.fromtimestamp(item["published"], timezone.utc), usegmt=True
            )
    return parse_feed(tostring(root, encoding="utf-8"))


async def fetch_official(client, url):
    parts = urlsplit(url)
    language = parts.path.removeprefix("/")
    if (
        parts.netloc != "official" or parts.scheme != "blablalink"
        or parts.query or parts.fragment or language not in OFFICIAL_LANGUAGES
    ):
        raise FeedError("国际服直连接口标识无效")
    headers, body = request_data(language)
    try:
        async with client.session.post(
            ENDPOINT, json=body, headers=headers, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        ) as response:
            if response.status != 200:
                raise FeedError(f"官方动态 HTTP {response.status}")
            raw = await client._read(response, MAX_FEED_BYTES)
        return await asyncio.to_thread(
            lambda: normalize(json.loads(raw.decode("utf-8")), language)
        )
    except FeedError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError) as exc:
        raise FeedError("官方动态网络请求或解析失败") from exc
