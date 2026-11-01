"""Public international-server character reference, using BlaBlaLink zh-TW assets."""

import asyncio
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp

from .chinese import simplified
from .network import image_proxy_url

SOURCE = "https://www.blablalink.com/shiftyspad/nikke-list"
CDN = "https://sg-tools-cdn.blablalink.com"
LIST_PATH = "character/zh-tw/nikke_list_zh-TW_v2.json"
TTL = 86400
MAX_BYTES = 8 * 1024 * 1024
QUERY_EXTRA = "nikke_character_query"
logger = logging.getLogger(__name__)


class CharacterError(ValueError):
    """A safe, user-facing lookup or source error."""


def asset_url(path: str) -> str:
    """BlaBlaLink's public filename convention (signed JS djb2 and MD5).

    Only internal, fixed zh-TW paths are passed here. Directory hashes use the
    entire plaintext path; the final component is its MD5 plus file extension.
    """
    parts = path.split("/")
    directories = []
    for prime in (224737, 1000639)[: len(parts) - 1]:
        value = prime
        for char in path:
            value = (value * 33 + ord(char)) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
        # Normalised remainder is the same for Python and the frontend's JS.
        value %= prime
        letters = chr(97 + value // 26 % 26) + chr(97 + value % 26)
        directories.append(f"{letters}-{value % 99:02}")
    digest = hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()
    return CDN + "/" + "/".join([*directories, digest + ".json"])


def normal_name(value: str) -> str:
    return re.sub(r"\s+", "", simplified(unicodedata.normalize("NFKC", value))).casefold()


def parse_query(query: str, skills: bool) -> tuple[str, int]:
    query = unicodedata.normalize("NFKC", query).strip()
    level = 10
    if skills:
        parts = query.rsplit(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"[+-]?\d+", parts[1]):
            query, level = parts[0], int(parts[1])
        if not 1 <= level <= 10:
            raise CharacterError("技能等级须为 1–10，例如：/nikke 技能 拉毗：小红帽 7")
    if not query or len(query) > 100:
        action = "技能" if skills else "角色"
        raise CharacterError(f"用法：/nikke {action} 角色名" + (" [等级1–10]" if skills else ""))
    return query, level


def clean_text(value: str) -> str:
    # Strip markup only. Angle-bracketed headings such as <在階段1使用：...>
    # are actual game text and must survive.
    value = re.sub(r"</?(?:color|word_group|size|b|i)(?:=[^<>]*)?>", "", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    return simplified(html.unescape(value)).strip()


def hundredths(value) -> str:
    text = format(Decimal(str(value)) / 100, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def description(detail: dict, level: int, *, weapon=False) -> str:
    template = detail.get("description_localkey")
    if not isinstance(template, str) or not template:
        raise CharacterError("官方资料暂缺技能说明，请稍后重试。")
    values = {}
    for index, row in enumerate(detail.get("description_value_list", []), 1):
        levels = row.get("description_value", []) if isinstance(row, dict) else []
        if isinstance(levels, list) and levels:
            # The official frontend falls back to the first value for constant
            # rows that do not provide a separate entry for every level.
            values[f"description_value_{index:02}"] = str(
                levels[level - 1] if len(levels) >= level else levels[0]
            )
    if weapon:
        # Game weapon ratios are integer hundredths of a percent; times are
        # hundredths of a second. Skill description_value_list is already scaled.
        for key in ("damage", "core_damage_rate", "full_charge_damage", "charge_time"):
            if isinstance(detail.get(key), (int, float)):
                values[key] = hundredths(detail[key])

    def substitute(match):
        key = match[1]
        if key not in values:
            raise CharacterError("官方技能资料格式有变化，暂时无法准确显示数值。")
        return values[key]

    return clean_text(re.sub(r"\{([^{}]+)\}", substitute, template))


def validate(path: str, data):
    if path == LIST_PATH:
        if not isinstance(data, list) or not data:
            raise CharacterError("官方角色列表格式异常。")
        for row in data:
            if (
                not isinstance(row, dict)
                or type(row.get("resource_id")) is not int
                or row["resource_id"] <= 0
            ):
                raise CharacterError("官方角色列表格式异常。")
            local_name = row.get("name_localkey")
            if not isinstance(local_name, dict) or not isinstance(local_name.get("name"), str):
                raise CharacterError("官方角色名称格式异常。")
    else:
        resource = int(path.split("/")[1].split("-")[0])
        if not isinstance(data, dict) or data.get("resource_id") != resource:
            raise CharacterError("官方角色详情格式异常。")
        if not isinstance(data.get("name_localkey"), str):
            raise CharacterError("官方角色详情缺少名称。")
        if not isinstance(data.get("element_details", []), list):
            raise CharacterError("官方角色属性格式异常。")
        if not isinstance(data.get("shot_detail", {}), dict):
            raise CharacterError("官方武器资料格式异常。")
        for key in ("skill1_detail", "skill2_detail", "ulti_skill_detail"):
            if not isinstance(data.get(key), dict):
                raise CharacterError("官方角色详情缺少技能资料。")
            skill = data[key]
            values = skill.get("description_value_list", [])
            if not isinstance(values, list):
                raise CharacterError("官方技能数值格式异常。")


class CharacterCatalog:
    """Lazy, bounded downloads with a validated disk cache; never sends cookies."""

    def __init__(self, directory: Path, *, proxy="", clock=time.time):
        self.directory = directory
        self.proxy = image_proxy_url(proxy)
        self.clock = clock
        self.lock = asyncio.Lock()
        self.retry_after = {}

    async def _download(self, path: str, etag: str):
        headers = {"If-None-Match": etag} if etag else {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, connect=5),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        ) as session:
            async with session.get(
                asset_url(path), headers=headers, proxy=self.proxy or None, allow_redirects=False
            ) as response:
                if response.status == 304:
                    return None, etag
                if response.status != 200:
                    raise CharacterError(f"官方角色资料暂时不可用（HTTP {response.status}）。")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise CharacterError("官方角色资料超过大小限制。")
                return json.loads(raw), response.headers.get("ETag", "")

    def _read(self, file: Path, path: str):
        try:
            if file.stat().st_size > MAX_BYTES + 4096:
                return None
            cached = json.loads(file.read_text(encoding="utf-8"))
            if cached.get("version") != 1:
                return None
            validate(path, cached["data"])
            if not isinstance(cached["checked"], (int, float)):
                return None
            if not isinstance(cached.get("etag", ""), str):
                return None
            return cached
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _save(self, file: Path, cached):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = file.with_suffix(".tmp")
        temporary.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        temporary.replace(file)

    async def _load(self, path: str):
        file = self.directory / (hashlib.sha256(path.encode()).hexdigest() + ".json")
        cached = await asyncio.to_thread(self._read, file, path)
        now = self.clock()
        if cached and now - cached["checked"] < TTL:
            return cached["data"], cached["checked"], False
        if now < self.retry_after.get(path, 0):
            if cached:
                return cached["data"], cached["checked"], True
            raise CharacterError("官方角色资料暂时不可用，请几分钟后再试。")
        try:
            data, etag = await self._download(path, cached.get("etag", "") if cached else "")
            if data is None:
                if not cached:
                    raise CharacterError("官方角色资料响应异常。")
                data = cached["data"]
            validate(path, data)
        except (CharacterError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.retry_after[path] = now + 300
            if cached:
                return cached["data"], cached["checked"], True
            if isinstance(exc, CharacterError):
                raise
            raise CharacterError("官方角色资料读取失败，请稍后重试。") from exc
        saved = {"version": 1, "checked": now, "etag": etag, "data": data}
        try:
            await asyncio.to_thread(self._save, file, saved)
        except OSError:
            logger.warning("[NIKKE] 角色资料缓存写入失败")
        return data, now, False

    async def query(self, query: str, *, skills=False) -> list[str]:
        name, level = parse_query(query, skills)
        async with self.lock:
            rows, list_time, list_stale = await self._load(LIST_PATH)
            visible = [row for row in rows if row.get("is_visible", True)]
            key = normal_name(name)
            matches = [r for r in visible if normal_name(r["name_localkey"]["name"]) == key]
            if not matches:
                matches = [r for r in visible if key in normal_name(r["name_localkey"]["name"])]
            if len(matches) != 1:
                if not matches:
                    raise CharacterError(
                        "未找到角色，请使用国际服中文名；新角色资料最多延迟一天更新。"
                    )
                names = "、".join(clean_text(r["name_localkey"]["name"]) for r in matches[:8])
                raise CharacterError(f"匹配到多个角色：{names}。请使用完整名称。")
            row = matches[0]
            data, checked, stale = await self._load(f"roledata/{row['resource_id']}-v2-zh-tw.json")
            checked = min(checked, list_time)
            stamp = datetime.fromtimestamp(checked, timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%d %H:%M"
            )
            footer = (
                f"\n资料校验：{stamp}（北京时间）\n"
                f"来源：BlaBlaLink 国际服繁中资料（转简体）\n{SOURCE}"
            )
            if stale or list_stale:
                footer = "\n官方资料刷新失败，以下使用本地旧缓存。" + footer
            if skills:
                pages = []
                for title, field in (
                    ("技能1", "skill1_detail"),
                    ("技能2", "skill2_detail"),
                    ("爆裂技能", "ulti_skill_detail"),
                ):
                    skill = data[field]
                    cooldown = skill.get("skill_cooltime")
                    note = (
                        f" · 冷却 {hundredths(cooldown)} 秒"
                        if isinstance(cooldown, (int, float))
                        else ""
                    )
                    pages.append(
                        f"【{clean_text(data['name_localkey'])} · {title} Lv.{level}】\n"
                        f"{clean_text(skill.get('name_localkey', title))}{note}\n"
                        + description(skill, level)
                        + "\n（基础技能，未计珍藏品强化）"
                        + footer
                    )
                return pages
            return [format_profile(data) + footer]


def format_profile(data: dict) -> str:
    classes = {"Attacker": "火力型", "Supporter": "辅助型", "Defender": "防御型"}
    corps = {
        "ELYSION": "极乐净土",
        "MISSILIS": "米西利斯",
        "TETRA": "泰特拉",
        "PILGRIM": "朝圣者",
        "ABNORMAL": "反常",
    }
    bursts = {
        "Step1": "Ⅰ",
        "Step2": "Ⅱ",
        "Step3": "Ⅲ",
        "StepFull": "全阶段",
        "AllStep": "Ⅰ / Ⅱ / Ⅲ",
    }
    shot = data.get("shot_detail", {})
    element = (
        " / ".join(
            item.get("element_name_localekey", "未知")
            for item in data.get("element_details", [])
            if isinstance(item, dict)
        )
        or "未知"
    )
    burst = bursts.get(data.get("use_burst_skill"), data.get("use_burst_skill", "未知"))
    name = clean_text(data["name_localkey"])
    corporation = corps.get(data.get("corporation"), data.get("corporation", "未知"))
    lines = [
        f"【{name} · 国际服角色资料】",
        f"{data.get('original_rare', '未知')} · {clean_text(element)} · "
        + classes.get(data.get("class"), "未知"),
        f"企业：{corporation} · 爆裂阶段：{burst}",
    ]
    for label, field in (
        ("体力", "character_level_hp_list"),
        ("攻击", "character_level_attack_list"),
        ("防御", "character_level_defence_list"),
    ):
        values = data.get(field, [])
        if values:
            lines.append(f"Lv.1 {label}：{values[0]}")
    if shot:
        lines.append("武器：" + clean_text(shot.get("name_localkey", "未知")))
        if isinstance(shot.get("max_ammo"), (int, float)):
            lines.append(f"最大装弹数：{shot['max_ammo']}")
        if isinstance(shot.get("reload_time"), (int, float)):
            lines.append(f"换弹时间：{hundredths(shot['reload_time'])} 秒")
        lines.append(description(shot, 1, weapon=True))
    for field in ("skill1_detail", "skill2_detail", "ulti_skill_detail"):
        skill = data[field]
        lines.append(
            clean_text(skill.get("info_description_localkey", "技能"))
            + "："
            + clean_text(skill.get("name_localkey", "未知"))
        )
    lines.append(f"详细技能：/nikke 技能 {name} [等级1–10，默认10]")
    return "\n".join(lines)
