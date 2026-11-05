"""Public international-server character reference, using BlaBlaLink zh-TW assets."""

import asyncio
import hashlib
import html
import json
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
TREASURE_LIST_PATH = "equip/favorite_rare_map.json"
TREASURE_TIMEOUT = 45
SKILL_USAGE = "/nikke 技能 角色名 [等级1–10] [珍藏品1–3]"
TTL = 86400
MAX_BYTES = 8 * 1024 * 1024
QUERY_EXTRA = "nikke_character_query"
SKILL_TOOL_NAME = "nikke_query_character_skills"


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


def parse_skill_query(query: str) -> tuple[str, int, int]:
    query = unicodedata.normalize("NFKC", query).strip()
    tokens = query.split()
    phase = 0
    if any("珍藏" in token for token in tokens):
        match = re.fullmatch(r"珍藏品([1-3])", tokens[-1])
        if not match or any("珍藏" in token for token in tokens[:-1]):
            raise CharacterError("用法：" + SKILL_USAGE + "，例如：/nikke 技能 拉普拉斯 10 珍藏品3")
        phase = int(match[1])
        query = " ".join(tokens[:-1])
    name, level = _parse_name_level(query, skills=True)
    return name, level, phase


def parse_query(query: str, skills: bool) -> tuple[str, int]:
    if skills:
        name, level, _ = parse_skill_query(query)
        return name, level
    return _parse_name_level(query, skills=False)


def _parse_name_level(query: str, skills: bool) -> tuple[str, int]:
    query = unicodedata.normalize("NFKC", query).strip()
    level = 10
    if skills:
        parts = query.rsplit(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"[+-]?\d+", parts[1]):
            query, level = parts[0], int(parts[1])
        if not 1 <= level <= 10:
            raise CharacterError("技能等级须为 1–10，例如：/nikke 技能 拉毗：小红帽 7")
    if not query or len(query) > 100:
        raise CharacterError("用法：" + (SKILL_USAGE if skills else "/nikke 角色 角色名"))
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
    if path == TREASURE_LIST_PATH:
        ids = data.get("SSR") if isinstance(data, dict) else None
        if (
            not isinstance(ids, list) or len(ids) > 200
            or any(type(item) is not int or item <= 0 for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise CharacterError("官方珍藏品索引格式异常。")
    elif match := re.fullmatch(r"equip/zh-tw/favorite_(\d+)\.json", path):
        if (
            not isinstance(data, dict) or type(data.get("id")) is not int
            or data["id"] != int(match[1]) or data.get("favorite_rare") != "SSR"
            or type(data.get("name_code")) is not int or data["name_code"] <= 0
            or not isinstance(data.get("name_localkey"), str) or not data["name_localkey"]
            or data.get("grade") != [1, 2, 3] or data.get("max_level") != 2
        ):
            raise CharacterError("官方珍藏品详情格式异常。")
        groups = data.get("favoriteitem_skill_group_data")
        if not isinstance(groups, list) or len(groups) != 3:
            raise CharacterError("官方珍藏品技能阶段格式异常。")
        slots = []
        for group in groups:
            if not isinstance(group, dict) or type(group.get("skill_change_slot")) is not int:
                raise CharacterError("官方珍藏品技能槽位格式异常。")
            slots.append(group["skill_change_slot"])
            info = group.get("info")
            if (
                not isinstance(info, dict) or not isinstance(info.get("name_localkey"), str)
                or not isinstance(info.get("description_localkey"), str)
                or not isinstance(info.get("description_value_list"), list)
            ):
                raise CharacterError("官方珍藏品技能资料格式异常。")
            # Reject incomplete data before caching or returning any skill page.
            for level in range(1, 11):
                description(info, level)
        if set(slots) != {1, 2, 3}:
            raise CharacterError("官方珍藏品技能槽位格式异常。")
    elif path == LIST_PATH:
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

    def __init__(self, directory: Path, logger, *, proxy="", clock=time.time):
        self.directory = directory
        self.logger = logger
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
            self.logger.warning("[NIKKE] 角色资料缓存写入失败")
        return data, now, False

    async def _treasure(self, name_code: int):
        index, index_time, index_stale = await self._load(TREASURE_LIST_PATH)
        semaphore = asyncio.Semaphore(4)

        async def load(item_id):
            async with semaphore:
                return await self._load(f"equip/zh-tw/favorite_{item_id}.json")

        # The public index only lists item IDs, so resolve their character codes
        # with bounded concurrency. Each detail reuses the 24-hour disk cache.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(load(item) for item in index["SSR"]), return_exceptions=True),
                timeout=TREASURE_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise CharacterError("珍藏品资料读取超时，请稍后重试。") from exc
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        matches = [
            result for result in results
            if not isinstance(result, BaseException) and result[0]["name_code"] == name_code
        ]
        if len(matches) > 1:
            raise CharacterError("官方珍藏品角色关联存在歧义，暂时无法准确查询。")
        if matches:
            item, checked, stale = matches[0]
            return item, min(checked, index_time), stale or index_stale
        if any(isinstance(result, BaseException) for result in results):
            raise CharacterError("部分珍藏品资料读取失败，暂时无法确认该角色的珍藏品，请稍后重试。")
        note = "官方资料刷新失败，当前使用旧缓存。" if index_stale else ""
        raise CharacterError(
            note + "BlaBlaLink 当前资料未收录该角色的珍藏品，请查询基础技能或稍后重试。"
        )

    async def query(self, query: str, *, skills=False) -> list[str]:
        name, level, phase = (
            parse_skill_query(query) if skills else (*parse_query(query, False), 0)
        )
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
            enhanced = {}
            treasure_name = ""
            if phase:
                name_code = data.get("name_code")
                if type(name_code) is not int or name_code <= 0:
                    raise CharacterError("官方角色资料缺少珍藏品关联，暂时无法准确查询。")
                treasure, item_time, item_stale = await self._treasure(name_code)
                checked = min(checked, item_time)
                stale = stale or item_stale
                treasure_name = clean_text(treasure["name_localkey"])
                # Source order is the upgrade order, NOT skill slot order.
                # Diesel/Laplace unlock slots 2, 3, 1; Frima unlocks 1, 3, 2.
                enhanced = {
                    group["skill_change_slot"]: group["info"]
                    for group in treasure["favoriteitem_skill_group_data"][:phase]
                }
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
                for slot, (title, field) in enumerate((
                    ("技能1", "skill1_detail"),
                    ("技能2", "skill2_detail"),
                    ("爆裂技能", "ulti_skill_detail"),
                ), 1):
                    # Favorite-item info omits the base cooldown in current data.
                    skill = {**data[field], **enhanced.get(slot, {})}
                    cooldown = skill.get("skill_cooltime")
                    note = (
                        f" · 冷却 {hundredths(cooldown)} 秒"
                        if isinstance(cooldown, (int, float))
                        else ""
                    )
                    state = (
                        f"（珍藏品：{treasure_name}；阶段{phase}；"
                        + ("本技能已强化）" if slot in enhanced else "本阶段未强化，沿用基础技能）")
                        if phase else "（基础技能，未计珍藏品强化）"
                    )
                    stage = f"（珍藏品阶段{phase}）" if phase else ""
                    pages.append(
                        f"【{clean_text(data['name_localkey'])} · {title} Lv.{level}{stage}】\n"
                        f"{clean_text(skill.get('name_localkey', title))}{note}\n"
                        + description(skill, level)
                        + "\n" + state
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
    lines.append(f"详细技能：/nikke 技能 {name} [等级1–10，默认10] [珍藏品1–3]")
    return "\n".join(lines)
