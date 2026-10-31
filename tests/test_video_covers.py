import asyncio

import pytest

from nikke.feed import Entry, FeedError, RSSClient, parse_feed, post_images

VIDEO = "r9rOkQcYzzU"
COVER = f"https://i.ytimg.com/vi_webp/{VIDEO}/maxresdefault.webp"
IDENT = "blablalink:29080-8468458281233829111:ja:4842560997316691343"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://youtu.be/{VIDEO}",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://m.youtube.com/embed/{VIDEO}",
    ],
)
def test_legacy_video_cover_from_exact_id(url):
    assert post_images(IDENT, url + "\n" + url, []) == (COVER,)
    item = Entry.from_dict(
        dict(
            id=IDENT,
            title="视频",
            text=url,
            link="https://www.blablalink.com/post/detail",
            author="官方",
            published=1,
            images=[],
        )
    )
    assert item.images == (COVER,) and item.text == url
    assert post_images("bilibili-id", url, []) == ()
    assert post_images(IDENT, url, ["https://sg-cdn.blablalink.com/official.jpg"]) == (
        "https://sg-cdn.blablalink.com/official.jpg",
    )


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtube.com.evil.invalid/watch?v={VIDEO}",
        f"https://youtube.com@evil.invalid/watch?v={VIDEO}",
        "https://www.youtube.com/watch?v=short",
        "https://[invalid",
        f"https://www.youtube.com/redirect?v={VIDEO}",
    ],
)
def test_invalid_or_non_video_links_not_inferred(url):
    assert post_images(IDENT, url, []) == ()


def test_legacy_rss_video_without_image_has_cover_and_keeps_link():
    xml = f"""<rss><channel><item><guid>{IDENT}</guid><title>動画</title>
    <link>https://www.blablalink.com/post/detail?post_uuid=4842560997316691343</link>
    <description><![CDATA[<p>動画</p><a href="https://www.youtube.com/watch?v={VIDEO}">视频</a>]]></description>
    </item></channel></rss>""".encode()
    item = parse_feed(xml)[0]
    assert item.images == (COVER,) and f"watch?v={VIDEO}" in item.text


def test_youtube_download_proxy_webp_and_404_jpeg_fallback():
    async def run():
        calls = []
        png = b"\x89PNG\r\n\x1a\noriginal"
        jpeg = b"\xff\xd8\xfforiginal"

        class Response:
            def __init__(self, status=200, headers=None, body=png):
                self.status, self.headers, self.body = status, headers or {}, body
                self.content = self

            async def iter_chunked(self, size):
                yield self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            responses = []

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return self.responses.pop(0)

            async def close(self):
                pass

        proxy = "http://127.0.0.1:7890"
        client = RSSClient(official_image_proxy=proxy)
        await client.session.close()
        client.session = Session()
        try:
            webp = b"RIFF\x00\x00\x00\x00WEBPoriginal"
            client.session.responses = [Response(body=webp)]
            assert await client.image(COVER) == webp
            assert calls[-1][0] == COVER and calls[-1][1]["proxy"] == proxy
            assert calls[-1][1]["headers"]["Referer"] == "https://www.youtube.com/"
            client.session.responses = [Response(404), Response(body=jpeg)]
            assert await client.image(COVER) == jpeg
            assert calls[-1][0] == f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"
            assert calls[-1][1]["proxy"] == proxy
            client.session.responses = [
                Response(302, {"Location": "https://i0.hdslb.com/a.png"}),
                Response(),
            ]
            assert await client.image(COVER) == png
            assert calls[-1][1]["proxy"] is None
            client.session.responses = [Response(302, {"Location": "http://127.0.0.1/private"})]
            before = len(calls)
            with pytest.raises(FeedError, match="域名"):
                await client.image(COVER)
            assert len(calls) == before + 1
            before = len(calls)
            with pytest.raises(FeedError, match="域名"):
                await client.image(COVER.replace("i.ytimg.com", "i.ytimg.com.evil.invalid"))
            assert len(calls) == before
        finally:
            await client.close()

    asyncio.run(run())
