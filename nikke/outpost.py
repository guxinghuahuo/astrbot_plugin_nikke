"""Offline international outpost reference. No network or account information."""

import json
import re
from functools import lru_cache
from pathlib import Path

USAGE = {
    "基地等级": "/nikke 基地等级 533",
    "红球": "/nikke 红球 h48-32 或 /nikke 红球 33-23 h18-32",
}
SUGGESTIONS = {
    "前哨": "基地等级",
    "前哨基地": "基地等级",
    "基地": "基地等级",
    "防御等级": "基地等级",
    "前哨等级": "基地等级",
    "防御基地等级": "基地等级",
    "芯尘表": "红球",
    "芯尘": "红球",
    "红球表": "红球",
    "红球查询": "红球",
}


class OutpostError(ValueError):
    """Short, actionable error suitable for a command reply."""


def parse_query(action: str, query: str):
    if action == "基地等级":
        if not re.fullmatch(r"[1-9][0-9]{0,2}", query) or not 1 <= int(query) <= 800:
            raise OutpostError("请填写 1–800 的基地等级。用法：" + USAGE[action])
        return int(query)
    tokens = query.split()
    if not 1 <= len(tokens) <= 2:
        raise OutpostError("用法：" + USAGE[action])
    normal, hard = None, None
    for token in tokens:
        match = re.fullmatch(r"([hH]?)([0-9]+-[1-9][0-9]*(?:[AB]-[1-9][0-9]*)?)", token)
        if not match:
            raise OutpostError("用法：" + USAGE[action])
        if match[1]:
            if hard is not None:
                raise OutpostError("困难进度只能填写一次。用法：" + USAGE[action])
            hard = match[2]
        else:
            if normal is not None:
                raise OutpostError("普通进度只能填写一次。用法：" + USAGE[action])
            normal = match[2]
    if hard is None:
        raise OutpostError("困难关卡须加 h 前缀。用法：" + USAGE[action])
    return normal, hard


@lru_cache(maxsize=1)
def load_data():
    directory = Path(__file__).with_name("data")
    rewards = json.loads((directory / "outpost.json").read_text(encoding="utf-8"))
    chapters = json.loads((directory / "chapters.json").read_text(encoding="utf-8"))
    rows = {int(row["level"]): row for row in rewards["outpost"]}
    stages = [
        (section["Section"].split()[0], int(section["id"]))
        for chapter in chapters["Chapters"]
        for section in chapter["Sections"]
    ]
    return rows, stages


def level_from_progress(normal: int, hard: int) -> int:
    """Mirror the source's effective-stage count, including its initial offset."""
    return 1 + (max(normal - 19, 0) + hard + 1) // 5


def query(action: str, argument: str) -> list[str]:
    value = parse_query(action, argument)
    try:
        rows, stages = load_data()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OutpostError("本地基地产出资料读取失败，请重新安装插件。") from exc
    if action == "基地等级":
        row = rows.get(value)
        if row is None:
            raise OutpostError("内置资料未覆盖该基地等级，请等待资料更新。")
        lines = [f"基地等级：{value}"]
        for title, field in (
            ("信用点", "credit"),
            ("战斗数据", "battle_data_set"),
            ("芯尘", "core_dust"),
        ):
            lines.append(f"{title}：{row[field + '_mul']}/小时")
        return ["\n".join(lines)]
    mapping = dict(stages)
    normal_name, hard_name = value
    normal = mapping.get(normal_name) if normal_name is not None else stages[-1][1]
    hard = mapping.get(hard_name)
    if normal is None or normal < 19:
        raise OutpostError("普通关卡未收录，支持 2-12 至 50-36。")
    if hard is None:
        raise OutpostError("困难关卡未收录，支持 h0-1 至 h50-36。")
    if hard > normal:
        raise OutpostError("困难通关进度不能超过普通通关进度。")
    level = level_from_progress(normal, hard)
    row = rows.get(level)
    if row is None:
        raise OutpostError("内置资料未覆盖该基地等级。")
    return [f"基地等级：{level}\n红球：{row['core_dust_mul']}/小时"]
