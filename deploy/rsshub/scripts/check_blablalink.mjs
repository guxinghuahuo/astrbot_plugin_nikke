// Read-only: run inside RSSHub. Does not load config files or send messages.
import { readFile } from 'node:fs/promises';
import { ACCOUNT, LANGUAGES, fetchOfficial } from '../routes-user/nikke-blablalink.mjs';

if (process.argv.includes('--capabilities')) {
    const candidates = ['/app/lib/user-routes.ts', '/app/dist/index.mjs', '/app/dist/index.js'];
    let supported = false;
    for (const file of candidates) {
        try {
            const text = await readFile(file, 'utf8');
            if (text.includes('loadUserRoutes') || text.includes('USER_ROUTES_PATH')) supported = true;
        } catch { /* Different images keep different build artifacts. */ }
    }
    let version = 'unknown';
    try { version = JSON.parse(await readFile('/app/package.json', 'utf8')).version; } catch { /* Local probe. */ }
    console.log(JSON.stringify({ rsshub_version: version, private_route_code_found: supported, node: process.version, messages_sent: 0 }));
    if (!supported) process.exitCode = 1;
} else {
    let failed = false;
    for (const language of Object.keys(LANGUAGES)) {
        try {
            const feed = await fetchOfficial(language);
            console.log(JSON.stringify({ language, account: ACCOUNT, items: feed.item.length, latest: feed.item[0]?.title, images: feed.item.reduce((n, item) => n + (item.description.match(/<img\b/gi) || []).length, 0), messages_sent: 0 }));
        } catch (error) {
            failed = true;
            console.error(JSON.stringify({ language, error: error.message, messages_sent: 0 }));
        }
    }
    if (failed) process.exitCode = 1;
}
