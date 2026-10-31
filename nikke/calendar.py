"""Official international-server calendar, timestamps and reminder presentation."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from .chinese import simplified
from .feed import FeedError

CALENDAR_URL = "https://api.blablalink.com/api/ugc/direct/standalonesite/Dynamics/GetCalendarDetail"
CALENDAR_LINK = "https://www.blablalink.com/activity-calendar"
SHANGHAI = timezone(timedelta(hours=8))
MAX_BYTES = 4 * 1024 * 1024
CATEGORIES = ("recruitment", "story", "raid", "overclock", "costume", "arena")
LABELS = {
    "recruitment": "限时招募",
    "story": "剧情/签到",
    "raid": "突袭/协同",
    "overclock": "模拟室超频",
    "costume": "时装/PASS",
    "arena": "竞技场",
}
MODULES = ("character_gacha", "version_event", "raid", "simulation_room", "skin_gacha", "arena")


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    category: str
    type: str
    title: str
    start: int
    end: int
    notice: str = "活动结束"

    def __post_init__(self):
        # ID is computed from original upstream fields before presentation conversion.
        # This also handles cached payloads loaded by reminder_pages after an upgrade.
        object.__setattr__(self, "title", simplified(self.title))
        object.__setattr__(self, "notice", simplified(self.notice))

    def to_dict(self):
        return asdict(self)


def epoch(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise FeedError("日历时间格式无效")
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise FeedError("日历时间格式无效") from exc
    if not 946684800 <= result <= 4102444800:
        raise FeedError("日历时间超出有效范围")
    return result


def normalise(payload: dict) -> list[CalendarEvent]:
    """Validate a full snapshot before replacing any previously good events.

    Missing modules / malformed known entries are not an empty calendar. Unknown
    types in known modules are ignored, allowing the official API to evolve.
    """
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise FeedError("日历响应格式无效")
    if payload["code"] != 0 or not isinstance(payload.get("data"), dict):
        raise FeedError("官方日历请求未成功")
    data = payload["data"]
    events = {}
    for module in MODULES:
        section = data.get(module)
        if not isinstance(section, dict) or not isinstance(section.get("items"), list):
            raise FeedError("日历分类数据缺失，保留上次完整结果")
        if len(section["items"]) > 1000:
            raise FeedError("日历活动数量超出限制")
        for item in section["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise FeedError("日历活动条目格式无效")
            kind = item["type"]
            notice = "活动结束"
            if kind == "LoginEvent":
                category, notice = "story", "签到截止"
            elif module == "character_gacha":
                category, notice = "recruitment", "招募结束"
            elif module == "raid":
                category = "raid"
            elif module == "skin_gacha":
                category = "costume"
            elif module == "version_event" and kind in {"StoryEvent", "FieldHubEvent"}:
                category = "story"
            elif module == "simulation_room" and kind.startswith("SimulationOverclockSeason"):
                category = "overclock"
                notice = (
                    "Buff 重置" if kind.startswith("SimulationOverclockSeason_Sub_") else "赛季结束"
                )
            elif module == "arena" and (
                kind in {"ArenaRookieSeason", "ArenaSpecialSeason", "ArenaChampionSeason"}
                or kind.startswith("ArenaChampionBetting")
            ):
                category = "arena"
                notice = "应援截止" if kind.startswith("ArenaChampionBetting") else "赛季结束"
            else:
                continue
            start, end = epoch(item.get("start_time")), epoch(item.get("end_time"))
            if end <= start:
                raise FeedError("日历活动起止时间无效")
            title = item.get("name")
            if category == "recruitment":
                characters = item.get("characters", [])
                if not isinstance(characters, list):
                    raise FeedError("日历招募角色数据无效")
                title = (
                    " / ".join(str(c.get("name", "")) for c in characters if isinstance(c, dict))
                    or title
                )
            elif category == "costume":
                title = item.get("costume_name") or item.get("theme_name") or title
            if not isinstance(title, str) or not title.strip():
                raise FeedError("日历活动标题缺失")
            title = " ".join(title.split())[:180]
            official_id = item.get("event_id") or item.get("id")
            identity = item.get("banner") or item.get("resource_id") or title
            key = [kind, str(official_id)] if official_id else [kind, start, str(identity)]
            identifier = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
            event = CalendarEvent(identifier, category, kind, title, start, end, notice)
            previous = events.get(identifier)
            if previous and (previous.start != start or previous.end != end):
                raise FeedError("同一日历活动时间冲突，保留上次结果")
            events.setdefault(identifier, event)
    seasons = [event for event in events.values() if event.type == "SimulationOverclockSeason"]
    # The final stage ends with the season; there is no following Buff reset.
    filtered = [
        event
        for event in events.values()
        if not (
            event.notice == "Buff 重置"
            and any(season.start <= event.start and event.end == season.end for season in seasons)
        )
    ]
    return sorted(filtered, key=lambda e: (e.end, e.category, e.id))


def scheduled_at(end: int, hours: int, quiet: bool) -> tuple[float, bool]:
    due = end - hours * 3600
    local = datetime.fromtimestamp(due, SHANGHAI)
    if quiet and local.hour < 8:
        early = (
            local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
        ).timestamp()
        return early, True
    return due, False


def in_quiet(now: float, quiet: bool) -> bool:
    return quiet and datetime.fromtimestamp(now, SHANGHAI).hour < 8


def date_text(value: float | None) -> str:
    return (
        datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        if value
        else "尚未获取"
    )


def remaining(end: int, now: float) -> str:
    minutes = max(0, int((end - now) // 60))
    if minutes < 1:
        return "不足 1 分钟"
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f"{days} 天 " if days else "") + f"{hours} 小时 {minutes} 分钟"


def reminder_pages(rows: list[dict], now: float, updated: float, limit=3000):
    """Each page carries exactly the reminder row IDs it acknowledges."""
    footer = f"\n数据更新：{date_text(updated)}\n官方日历：{CALENDAR_LINK}"
    prefix = "【NIKKE 国际服 · 活动到期提醒】\n时间均为北京时间\n"
    pages, ids, body = [], [], prefix
    for row in sorted(rows, key=lambda r: (r["end"], r["event_id"])):
        event = CalendarEvent(**json.loads(row["payload"]))
        early = "（夜间提醒提前汇总）" if row["early"] else ""
        text = (
            f"\n[{LABELS[event.category]}] {event.title}\n"
            f"{event.notice}{early}；剩余 {remaining(event.end, now)}\n"
            f"截止：{date_text(event.end)}\n"
        )
        if ids and len(body + text + footer) > limit:
            pages.append((body + footer, ids))
            body, ids = prefix, []
        body += text
        ids.append(row["rid"])
    if ids:
        pages.append((body + footer, ids))
    return pages


class CalendarClient:
    async def fetch(self) -> tuple[dict, list[CalendarEvent]]:
        common = {
            "game_id": "16",
            "area_id": "global",
            "source": "pc_web",
            "intl_game_id": "29080",
            "env": "prod",
            "language": "zh-TW",
            "data_statistics_scene": "outer",
            "data_statistics_lang": "zh-TW",
            "data_statistics_page_id": CALENDAR_LINK,
            "data_statistics_client_type": "pc_web",
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.blablalink.com",
            "referer": CALENDAR_LINK,
            "x-channel-type": "2",
            "x-language": "zh-TW",
            "x-common-params": json.dumps(common, separators=(",", ":")),
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=5),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            ) as session:
                async with session.post(
                    CALENDAR_URL, json={}, headers=headers, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        raise FeedError(f"官方日历 HTTP {response.status}")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise FeedError("日历响应超过 4 MiB")
            payload = json.loads(raw)
            return payload, normalise(payload)
        except FeedError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError) as exc:
            raise FeedError("官方日历网络请求或解析失败") from exc
