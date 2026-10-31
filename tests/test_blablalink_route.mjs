import assert from 'node:assert/strict';
import test from 'node:test';
import { ACCOUNT, ENDPOINT, LANGUAGES, fetchOfficial, normalize, routes } from '../deploy/rsshub/routes-user/nikke-blablalink.mjs';

function post(language = 'zh-TW', changes = {}) {
    return {
        intl_openid: ACCOUNT, is_official: 1, post_uuid: '9223372036854775807', language,
        title: '勝利女神', content: '<div>官方<br>公告</div>', user: { username: '妮姬官方' },
        pic_urls: ['https://sg-cdn.blablalink.com/test.jpeg?height=1080&width=1920'],
        publish_on: 1792810800, created_on: '1792807200', ...changes,
    };
}
const payload = (list) => ({ code: 0, data: { list } });

test('four languages keep localized content, distinct IDs, and original image parameters', async () => {
    const ids = [];
    for (const language of Object.keys(LANGUAGES)) {
        const feed = await fetchOfficial(language, async (url, init) => {
            assert.equal(url, ENDPOINT);
            assert.equal(init.method, 'POST');
            assert.equal(init.redirect, 'error');
            assert.equal(init.headers['x-language'], language);
            assert.equal(JSON.parse(init.headers['x-common-params']).language, language);
            assert.ok(!Object.keys(init.headers).some((key) => /cookie|authorization/i.test(key)));
            assert.deepEqual(JSON.parse(init.body), { plate_id: 0, order_by: 1, limit: '10', intl_openid: ACCOUNT, page_type: 0 });
            // Real endpoint declares text/plain, although its body is JSON.
            return new Response(JSON.stringify(payload([post(language)])), { headers: { 'content-type': 'text/plain' } });
        });
        const item = feed.item[0];
        ids.push(item.guid);
        assert.ok(item.guid.endsWith(':9223372036854775807'));
        assert.equal(new URL(item.link).searchParams.get('lang'), language);
        assert.match(item.description, /height=1080&amp;width=1920/);
        assert.equal(item.title, '勝利女神');
    }
    assert.equal(new Set(ids).size, 4);
});

test('all eleven rows, pinned duplicates, changed order, and publication fallback', () => {
    const list = Array.from({ length: 11 }, (_, i) => post('en', {
        post_uuid: String(900000 + i), publish_on: i === 0 ? 0 : 1792810800 + i,
    }));
    const feed = normalize(payload([...list, list[0]]), 'en');
    assert.equal(feed.item.length, 11);
    assert.deepEqual(feed, normalize(payload([...list].reverse()), 'en'));
    assert.equal(feed.item.at(-1).pubDate, new Date(1792807200000).toUTCString());
    const unknown = normalize(payload([post('en', { publish_on: -1, created_on: null })]), 'en');
    assert.equal(unknown.item[0].pubDate, undefined);
});

test('video links are supplied without downloading video or duplicating existing images', () => {
    const image = 'https://sg-cdn.blablalink.com/a.png';
    const feed = normalize(payload([post('ja', {
        type: 3, content: `<p>新動画</p><img src="${image}">`, pic_urls: [image],
        ext_info: JSON.stringify([{ video_url: 'https://www.youtube.com/watch?v=demo', video_title: '生放送' }]),
    })]), 'ja');
    assert.equal((feed.item[0].description.match(/<img /g) || []).length, 1);
    assert.match(feed.item[0].description, /https:\/\/www.youtube.com\/watch\?v=demo/);
});

test('video metadata cover is emitted even with empty pic_urls, preserving parameters and deduplicating', () => {
    for (const cover of [
        'https://sg-cdn.blablalink.com/lip/ugc/public/image/cover.jpg?height=720&width=1280',
        'https://i.ytimg.com/vi_webp/r9rOkQcYzzU/maxresdefault.webp',
    ]) {
        const video = { video_url: 'https://www.youtube.com/watch?v=r9rOkQcYzzU', video_cover: cover };
        const item = normalize(payload([post('ja', {
            type: 3, pic_urls: [], ext_info: JSON.stringify([video, video]),
        })]), 'ja').item[0];
        assert.equal((item.description.match(/<img /g) || []).length, 1);
        assert.ok(item.description.includes(cover.replaceAll('&', '&amp;')));
        const existing = normalize(payload([post('ja', {
            type: 3, pic_urls: [cover], ext_info: JSON.stringify([video]),
        })]), 'ja').item[0];
        assert.equal((existing.description.match(/<img /g) || []).length, 1);
    }
    const item = normalize(payload([post('ja', {
        type: 3, pic_urls: [], ext_info: JSON.stringify([{ video_cover: 'file:///private' }]),
    })]), 'ja').item[0];
    assert.ok(!item.description.includes('file:///private'));
});

test('valid empty feed and deleted posts differ from malformed/upstream errors', () => {
    assert.deepEqual(normalize(payload([]), 'ko').item, []);
    assert.deepEqual(normalize(payload([post('ko', { is_del: 1 })]), 'ko').item, []);
    for (const value of [{ code: 1, msg: 'private body' }, {}, { code: 0, data: { list: {} } }, payload([null])]) {
        assert.throws(() => normalize(value, 'ko'));
    }
    for (const changes of [
        { post_uuid: 9223372036854775807 }, { language: 'en' }, { intl_openid: 'other' },
        { is_official: 0 }, { pic_urls: ['file:///private'] }, { content: null },
        { type: 3, ext_info: '{oops' }, { type: 3, ext_info: '{}' },
    ]) assert.throws(() => normalize(payload([post('zh-TW', changes)]), 'zh-TW'));
    assert.throws(() => normalize(payload([post(), post('zh-TW', { title: 'conflict' })]), 'zh-TW'));
});

test('transport, non-200, malformed JSON, size limits, and language are safe failures', async () => {
    await assert.rejects(fetchOfficial('xx'), /Unsupported/);
    await assert.rejects(fetchOfficial('en', async () => { throw new Error('token=secret'); }), (error) => !error.message.includes('secret'));
    await assert.rejects(fetchOfficial('en', async () => new Response('secret', { status: 503 })), /HTTP 503/);
    await assert.rejects(fetchOfficial('en', async () => new Response('not JSON secret')), /Invalid BlaBlaLink JSON/);
    await assert.rejects(fetchOfficial('en', async () => new Response('x'.repeat(4 * 1024 * 1024 + 1))), /exceeds/);
    assert.equal(routes[0].path, '/official/:language');
});
