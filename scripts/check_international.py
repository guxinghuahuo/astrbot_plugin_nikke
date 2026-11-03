"""Read-only AstrBot container probe: official API / RSSHub feeds and complete original images.

Optionally reads only the configured image proxy; no database, model or QQ send.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nikke.config import OFFICIAL_LANGUAGES  # noqa: E402
from nikke.feed import FeedError, RSSClient  # noqa: E402
from nikke.network import image_proxy_url  # noqa: E402


def configured_proxy(config_path: str | None = None, proxy_env: str | None = None) -> str:
    if config_path:
        try:
            config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
            base = config.get("base", {})
            return image_proxy_url(
                base.get("official_image_proxy", config.get("official_image_proxy", ""))
            )
        except (OSError, ValueError, AttributeError):
            raise ValueError("无法读取配置中的国际服图片代理，请检查配置文件及代理格式") from None
    if proxy_env:
        value = os.environ.get(proxy_env, "")
        if not value:
            raise ValueError("指定的代理环境变量为空")
        return image_proxy_url(value)
    return ""


async def check(base: str, skip_images: bool, proxy: str = "", source: str = "rsshub") -> int:
    client = RSSClient(official_image_proxy=proxy)
    failed = False
    try:
        for language in OFFICIAL_LANGUAGES:
            stage = "official_api" if source == "direct" else "rss"
            result = {"language": language, "image_proxy_enabled": bool(proxy), "messages_sent": 0}
            try:
                entries = await client.fetch(
                    "blablalink://official/" + language if source == "direct"
                    else base.rstrip("/") + "/nikke-blablalink/official/" + language
                )
                result["entries"] = len(entries)
                if not entries:
                    raise FeedError("RSS 为空，无法验证官方动态")
                stage = "original_image"
                url = next((url for entry in entries for url in entry.images), None)
                if not skip_images:
                    if not url:
                        raise FeedError("本页无图片，无法完成原图验收")
                    data = await client.image(url)
                    result["image_bytes"] = len(data)
                    del data
                else:
                    result["image_check"] = "skipped"
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as exc:
                failed = True
                # Network exceptions can contain proxy credentials in other environments.
                detail = str(exc) if isinstance(exc, FeedError) else type(exc).__name__
                print(
                    json.dumps(
                        {**result, "failed_stage": stage, "error": detail},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        await client.close()
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("direct", "rsshub"), default="direct")
    parser.add_argument("--rsshub", default="http://localhost:1200")
    parser.add_argument("--skip-images", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--config", help="读取插件配置中的国际服图片代理，不改写配置")
    group.add_argument("--proxy-env", help="从指定环境变量读取图片代理，不输出其值")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        proxy = configured_proxy(args.config, args.proxy_env)
    except ValueError as exc:
        parser.error(str(exc))
    raise SystemExit(asyncio.run(check(args.rsshub, args.skip_images, proxy, args.source)))
