import asyncio

import pytest
from aiohttp import web

from nikke.feed import Entry, FeedError, RSSClient, parse_feed
from nikke.service import message_text


def feed(body: str) -> bytes:
    return (
        '<rss version="2.0"><channel><title>官方动态</title>' + body + "</channel></rss>"
    ).encode()


def test_rss_images_repost_video_and_unicode():
    entries = parse_feed(
        feed("""
        <item><guid>12345678901234567890</guid><title>更新公告</title>
        <link>https://t.bilibili.com/123</link><pubDate>Fri, 23 Oct 2026 04:00:00 GMT</pubDate>
        <description><![CDATA[<p>更新公告</p><p>妮姬 &amp; 指挥官</p>
        <br>//转发自：官方：活动开放<br><img src="//i0.hdslb.com/a.jpg">
        <img src="https://i0.hdslb.com/a.jpg"><img src="https://i1.hdslb.com/b.png">
        <script>do_not_include()</script><style>do_not_include</style>
        <iframe src="//player.bilibili.com/player.html?bvid=BV1234"></iframe>
        ]]></description></item>""")
    )
    entry = entries[0]
    assert entry.id == "12345678901234567890"
    assert "妮姬 & 指挥官" in entry.text and "转发自" in entry.text
    assert "do_not_include" not in entry.text
    assert "https://www.bilibili.com/video/BV1234" in entry.text
    assert entry.images == ("https://i0.hdslb.com/a.jpg", "https://i1.hdslb.com/b.png")
    text = message_text("官方", entry)
    assert "2026-10-23 12:00:00" in text
    assert text.count("更新公告") == 1


def test_guid_fallback_duplicate_and_missing_dates():
    item = "<item><title>新动态</title><link>https://t.bilibili.com/1</link></item>"
    entries = parse_feed(feed(item + item))
    assert len(entries) == 1
    assert entries[0].id == entries[0].link
    assert entries[0].published is None


def test_atom_html():
    entry = parse_feed(b"""<feed xmlns="http://www.w3.org/2005/Atom">
        <title>Official</title><entry><id>tag:one</id><title>News</title>
        <link href="https://example.com/post"/>
        <published>2026-10-23T04:00:00Z</published>
        <content type="html">&lt;p&gt;Hello&lt;/p&gt;</content></entry></feed>""")[0]
    assert entry.text == "Hello" and entry.published is not None


@pytest.mark.parametrize(
    "data",
    [
        b"<html><body>this route is empty</body></html>",
        b"not xml",
        b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>',
        feed("<item><title>No link or id</title></item>"),
    ],
)
def test_errors_are_not_empty_feeds(data):
    with pytest.raises(FeedError):
        parse_feed(data)


def test_valid_empty_feed():
    assert parse_feed(feed("")) == []


def test_long_message_preserves_link():
    entry = Entry("1", "", "文" * 5000, "https://t.bilibili.com/1", "官方", None, ())
    text = message_text("NIKKE", entry)
    assert "全文请见原链接" in text and text.endswith(entry.link)
    assert len(text) < 4000


def test_real_http_error_html_and_valid_response():
    async def scenario():
        app = web.Application()

        async def endpoint(request):
            mode = request.match_info["mode"]
            if mode == "error":
                return web.Response(status=503, text="secret_body_not_in_error")
            if mode == "html":
                return web.Response(text="<html>Error</html>")
            return web.Response(body=feed(""))

        app.router.add_get("/{mode}", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = RSSClient()
        try:
            with pytest.raises(FeedError, match="503") as err:
                await client.fetch(f"http://127.0.0.1:{port}/error")
            assert "secret_body" not in str(err.value)
            with pytest.raises(FeedError):
                await client.fetch(f"http://127.0.0.1:{port}/html")
            assert await client.fetch(f"http://127.0.0.1:{port}/valid") == []
            with pytest.raises(FeedError, match="域名"):
                await client.image(f"http://127.0.0.1:{port}/valid")
            with pytest.raises(FeedError, match="域名"):
                await client.image("http://hdslb.com.evil.invalid/test.png")
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_stream_size_limit_is_enforced():
    class Content:
        async def iter_chunked(self, size):
            yield b"1234"
            yield b"5678"

    class Response:
        content = Content()

    async def scenario():
        assert await RSSClient._read(Response(), 8) == b"12345678"
        with pytest.raises(FeedError, match="大小限制"):
            await RSSClient._read(Response(), 7)

    asyncio.run(scenario())
