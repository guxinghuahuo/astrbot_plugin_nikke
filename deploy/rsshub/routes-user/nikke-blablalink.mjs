// Standalone RSSHub private route. No repository aliases or runtime dependencies.
export const ACCOUNT = '29080-8468458281233829111';
export const ENDPOINT = 'https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/GetUserPostList';
export const LANGUAGES = { 'zh-TW': '繁中官方', en: '全球英文官方', ja: '日本官方', ko: '韩国官方' };
const MAX_BYTES = 4 * 1024 * 1024;

export const namespace = { name: 'NIKKE BlaBlaLink', url: 'www.blablalink.com' };

function languageCheck(language) {
    if (!Object.hasOwn(LANGUAGES, language)) throw new Error('Unsupported BlaBlaLink language');
}

function escape(text) {
    return text.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

function webUrl(value) {
    if (typeof value !== 'string') return '';
    try {
        const url = new URL(value);
        return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? value : '';
    } catch { return ''; }
}

function date(value) {
    if (!/^[0-9]+$/.test(String(value))) return undefined;
    const seconds = Number(value);
    return Number.isSafeInteger(seconds) && seconds >= 946684800 && seconds < 4102444800
        ? new Date(seconds * 1000).toUTCString() : undefined;
}

export function normalize(payload, language) {
    languageCheck(language);
    if (payload?.code !== 0 || !Array.isArray(payload?.data?.list) || payload.data.list.length > 500) {
        throw new Error('Invalid BlaBlaLink response or business error');
    }
    const items = new Map();
    for (const post of payload.data.list) {
        if (!post || typeof post !== 'object' || Array.isArray(post)) throw new Error('Invalid official post');
        if (post.is_del === 1 || post.is_hide === 1) continue;
        if (post.intl_openid !== ACCOUNT || post.is_official !== 1 || post.language !== language ||
            typeof post.post_uuid !== 'string' || !/^[0-9]{1,32}$/.test(post.post_uuid) ||
            typeof post.title !== 'string' || typeof post.content !== 'string' ||
            !Array.isArray(post.pic_urls) || typeof post.user?.username !== 'string') {
            throw new Error('Official post identity, language or content is invalid');
        }
        let description = post.content;
        for (const image of [...new Set(post.pic_urls)]) {
            if (!webUrl(image)) throw new Error('Invalid official image URL');
            if (!description.includes(image) && !description.includes(escape(image))) {
                description += `<p><img src="${escape(image)}"></p>`;
            }
        }
        if (post.type === 3 && post.ext_info) {
            let videos;
            try { videos = JSON.parse(post.ext_info); } catch { throw new Error('Invalid video metadata'); }
            if (!Array.isArray(videos)) throw new Error('Invalid video metadata');
            for (const video of videos) {
                const cover = webUrl(video?.video_cover);
                if (cover && !description.includes(cover) && !description.includes(escape(cover))) {
                    description += `<p><img src="${escape(cover)}"></p>`;
                }
                const url = webUrl(video?.video_url);
                if (url && !description.includes(url) && !description.includes(escape(url))) {
                    description += `<p><a href="${escape(url)}">${escape(video.video_title || '视频原链接')}</a></p>`;
                }
            }
        }
        const item = {
            title: post.title,
            author: post.user.username,
            guid: `blablalink:${ACCOUNT}:${language}:${post.post_uuid}`,
            link: `https://www.blablalink.com/post/detail?post_uuid=${post.post_uuid}&lang=${language}`,
            description,
            pubDate: date(post.publish_on) || date(post.created_on),
        };
        const previous = items.get(item.guid);
        if (previous && JSON.stringify(previous) !== JSON.stringify(item)) {
            throw new Error('Conflicting duplicate official post');
        }
        items.set(item.guid, item);
    }
    return {
        title: `NIKKE 国际服 · ${LANGUAGES[language]}`,
        link: `https://www.blablalink.com/?lang=${language}`,
        description: 'BlaBlaLink NIKKE 官方动态；最近一页，不包含全量历史。',
        language,
        item: [...items.values()].sort((a, b) => (Date.parse(b.pubDate) || 0) - (Date.parse(a.pubDate) || 0) || a.guid.localeCompare(b.guid)),
    };
}

export async function fetchOfficial(language, request = globalThis.fetch) {
    languageCheck(language);
    const common = {
        game_id: '16', area_id: 'global', source: 'pc_web', intl_game_id: '29080',
        env: 'prod', language, data_statistics_scene: 'outer', data_statistics_lang: language,
        data_statistics_page_id: 'https://www.blablalink.com/', data_statistics_client_type: 'pc_web',
    };
    try {
        const response = await request(ENDPOINT, {
            method: 'POST',
            headers: {
                accept: 'application/json, text/plain, */*', 'content-type': 'application/json',
                origin: 'https://www.blablalink.com', referer: 'https://www.blablalink.com/',
                'x-language': language, 'x-channel-type': '2', 'x-common-params': JSON.stringify(common),
            },
            body: JSON.stringify({ plate_id: 0, order_by: 1, limit: '10', intl_openid: ACCOUNT, page_type: 0 }),
            redirect: 'error', signal: AbortSignal.timeout(30_000),
        });
        if (response.status !== 200) {
            await response.body?.cancel();
            throw new Error(`BlaBlaLink HTTP ${response.status}`);
        }
        const reader = response.body.getReader();
        const chunks = [];
        let size = 0;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                size += value.byteLength;
                if (size > MAX_BYTES) throw new Error('BlaBlaLink response exceeds 4 MiB');
                chunks.push(value);
            }
        } finally { await reader.cancel(); }
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
        let payload;
        try { payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
        catch { throw new Error('Invalid BlaBlaLink JSON'); }
        return normalize(payload, language);
    } catch (error) {
        // Never expose upstream bodies, transport diagnostics or environment credentials.
        if (error instanceof Error && /^(BlaBlaLink HTTP \d{3}|BlaBlaLink response exceeds|Invalid BlaBlaLink|Invalid official|Official post|Conflicting duplicate|Invalid video)/.test(error.message)) throw error;
        throw new Error('BlaBlaLink request failed or timed out');
    }
}

export const routes = [{
    path: '/official/:language', name: '国际服官方动态', maintainers: [],
    handler: (ctx) => fetchOfficial(ctx.req.param('language')),
}];
