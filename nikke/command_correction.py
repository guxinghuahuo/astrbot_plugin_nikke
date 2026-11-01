"""Local command preflight. It never fetches, invokes models or dispatches handlers."""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .characters import QUERY_EXTRA, CharacterError, parse_query
from .chinese import simplified
from .config import flatten_config, resolve_targets

ACTIONS = ("帮助", "最新", "活动", "状态", "检查", "角色", "技能")
ADMIN_ACTIONS = {"状态", "检查"}
NAMED_ACTIONS = {"最新", "检查"}
CORRECTED_COMMAND = "nikke_corrected_command"


@lru_cache(maxsize=512)
def normalize(value):
    return simplified(unicodedata.normalize("NFKC", value).strip()).casefold()


def similarity(left, right):
    if not left or not right or max(len(left), len(right)) > 128:
        return 0.0
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, row[j] + 1, row[j - 1] + (a != b)))
        row = current
    return 1 - row[-1] / max(len(left), len(right))


def unique_fuzzy(scores, length):
    return bool(
        scores
        and length >= 3
        and scores[0] + 1e-12 >= 0.85
        and (len(scores) == 1 or scores[0] - scores[1] + 1e-12 >= 0.15)
    )


def source_conflict(name, sub):
    value = normalize(name)
    chinese = any(term in value for term in ("国服", "bilibili", "b站"))
    international = any(
        term in value
        for term in (
            "国际服",
            "全球",
            "blabla",
            "台服",
            "繁中",
            "英文",
            "英语",
            "日服",
            "日语",
            "日本",
            "韩服",
            "韩语",
            "韩国",
            "english",
            "global",
        )
    )
    if chinese and sub.source != "bilibili" or international and sub.source != "blablalink":
        return True
    if sub.source == "blablalink":
        hints = {
            "zh-TW": ("繁中", "台服", "台港澳"),
            "en": ("英文", "英语", "english"),
            "ja": ("日服", "日语", "日本"),
            "ko": ("韩服", "韩语", "韩国"),
        }
        for language, words in hints.items():
            if language != sub.language and any(word in value for word in words):
                return True
        for code in ("en", "ja", "ko", "zh-tw"):
            if code.casefold() != sub.language.casefold() and re.search(
                r"(?:^|[^a-z0-9])" + code + r"(?:$|[^a-z0-9])", value
            ):
                return True
    return False


@dataclass(frozen=True)
class Verdict:
    command: str = ""
    message: str = ""


def candidates_for(action, subscriptions, origin, group):
    if action == "检查":
        return list(subscriptions)
    return [sub for sub in subscriptions if group and origin in sub.targets]


def command_lines(action, candidates, query=""):
    ranked = sorted(
        candidates, key=lambda sub: (-similarity(normalize(query), normalize(sub.name)), sub.name)
    )
    return "\n".join("/nikke " + action + " " + sub.name for sub in ranked[:3])


def usage_actions(actions, subscriptions, origin, group, remaining=()):
    lines = []
    for action in actions:
        if action in NAMED_ACTIONS:
            candidates = candidates_for(action, subscriptions, origin, group)
            if not candidates:
                continue
            lines.append(
                command_lines(action, candidates, remaining[0] if remaining else "").splitlines()[0]
            )
        else:
            lines.append("/nikke " + action + (" 角色名" if action in ("角色", "技能") else ""))
    return "\n".join(lines[:3]) or "当前没有可用的子指令。"


def evaluate(text, subscriptions, disabled, enabled_actions, origin, group, admin):
    tokens = text.strip().split()
    actions = [
        action
        for action in ACTIONS
        if action in enabled_actions and (admin or action not in ADMIN_ACTIONS)
    ]
    if len(tokens) == 1:
        return Verdict(
            message="请选择子指令：\n" + usage_actions(actions, subscriptions, origin, group)
        )
    action = next((a for a in ACTIONS if normalize(a) == normalize(tokens[1])), None)
    if action is None:
        ranked = sorted(
            actions,
            key=lambda a: (-similarity(normalize(tokens[1]), normalize(a)), ACTIONS.index(a)),
        )
        strong = [a for a in ranked if similarity(normalize(tokens[1]), normalize(a)) >= 0.5]
        return Verdict(
            message=f"未知子指令“{tokens[1][:80]}”。请使用：\n"
            + usage_actions(strong or ranked, subscriptions, origin, group, tokens[2:])
        )
    if action not in enabled_actions:
        return Verdict(
            message="此子指令已停用。当前可用示例：\n"
            + usage_actions(actions, subscriptions, origin, group)
        )
    if action in ADMIN_ACTIONS and not admin:
        # Leave parameter parsing/denial to the native permission filter; never expose names.
        return Verdict(
            command="nikke " + action + (" " + " ".join(tokens[2:]) if len(tokens) > 2 else "")
        )
    if action in ("角色", "技能"):
        query = " ".join(tokens[2:])
        try:
            parse_query(query, skills=action == "技能")
        except CharacterError as exc:
            return Verdict(message=str(exc))
        return Verdict(command="nikke " + action + " " + query)
    if action not in NAMED_ACTIONS:
        if len(tokens) != 2:
            return Verdict(message="此指令不接受额外参数。请使用：\n/nikke " + action)
        return Verdict(command="nikke " + action)
    candidates = candidates_for(action, subscriptions, origin, group)
    if action == "最新" and not group:
        return Verdict(message="最新指令只能在该订阅配置的目标群使用。\n用法：/nikke 最新 订阅名称")
    examples = command_lines(action, candidates, tokens[2] if len(tokens) > 2 else "")
    if len(tokens) != 3:
        reason = "缺少订阅名称。" if len(tokens) < 3 else "参数多余；订阅名称不包含空格。"
        return Verdict(
            message=reason
            + ("请使用：\n" + examples if examples else "当前会话没有已启用的可用订阅。")
        )
    name = tokens[2]
    for sub in subscriptions:
        if sub.name == name:
            if sub not in candidates:
                return Verdict(message="请在该订阅配置的目标群中使用最新指令。")
            return Verdict(command="nikke " + action + " " + sub.name)
    if name in disabled:
        return Verdict(
            message="该订阅已停用，请联系管理员启用。"
            if disabled[name]
            else "该订阅当前不可用于本会话。"
        )
    exact = [sub for sub in candidates if normalize(sub.name) == normalize(name)]
    if len(exact) == 1 and not source_conflict(name, exact[0]):
        return Verdict(command="nikke " + action + " " + exact[0].name)
    if not exact:
        outside = [sub for sub in subscriptions if normalize(sub.name) == normalize(name)]
        if outside:
            return Verdict(message="请在该订阅配置的目标群中使用最新指令。")
        if any(normalize(value) == normalize(name) for value in disabled):
            visible = any(
                allowed
                for value, allowed in disabled.items()
                if normalize(value) == normalize(name)
            )
            return Verdict(
                message="该订阅已停用，请联系管理员启用。"
                if visible
                else "该订阅当前不可用于本会话。"
            )
        ranked = sorted(
            candidates,
            key=lambda sub: (-similarity(normalize(name), normalize(sub.name)), sub.name),
        )
        scores = [similarity(normalize(name), normalize(sub.name)) for sub in ranked]
        if unique_fuzzy(scores, len(normalize(name))) and not source_conflict(name, ranked[0]):
            return Verdict(command="nikke " + action + " " + ranked[0].name)
    reason = "订阅名称不明确。" if len(exact) > 1 else f"未找到可明确匹配的订阅“{name[:100]}”。"
    return Verdict(
        message=reason + ("请使用：\n" + examples if examples else "当前会话没有已启用的可用订阅。")
    )


class CommandPreflight:
    nikke_usage_filter = True

    def __init__(self, plugin, group, logger=None):
        self.plugin, self.group = plugin, group
        self.logger = logger

    def enabled_actions(self):
        return {
            child.command_name
            for child in self.group.sub_command_filters
            if getattr(getattr(child, "handler_md", None), "enabled", True)
        }

    def disabled_names(self, event):
        result = {}
        for item in self.plugin.config.get("subscriptions", []):
            source = item.get("source", item.get("__template_key", "bilibili"))
            if item.get("enabled", source != "blablalink") or not isinstance(item.get("name"), str):
                continue
            visible = event.is_admin()
            if not visible and event.get_group_id():
                try:
                    config = flatten_config(self.plugin.config)
                    targets = resolve_targets(
                        item.get("targets", []),
                        str(config.get("qq_platform_id", "")),
                        self.plugin._qq_platform_ids(),
                    )
                    visible = event.unified_msg_origin in targets
                except ValueError:
                    pass
            result[item["name"]] = result.get(item["name"], False) or visible
        return result

    def filter(self, event, cfg):
        if not event.is_at_or_wake_command:
            return False
        raw = event.get_message_str().strip()
        tokens = raw.split()
        if not tokens or normalize(tokens[0]) != "nikke":
            return False
        event.should_call_llm(False)
        # Preserve raw names for exact matching before applying Unicode normalization.
        text = " ".join(raw.split())
        service = self.plugin.service
        if len(raw) > 512:
            verdict = Verdict(message="命令过长。请使用 /nikke 帮助 查看用法。")
        elif service is None:
            verdict = Verdict(message="插件尚未初始化或配置有误，请联系管理员检查。")
        else:
            verdict = evaluate(
                text,
                service.settings.subscriptions,
                self.disabled_names(event),
                self.enabled_actions(),
                event.unified_msg_origin,
                event.get_group_id(),
                event.is_admin(),
            )
        if verdict.message:
            if self.logger:
                self.logger.info(
                    "[NIKKE] 命令纠错提示：会话=%s，输入=%s，回复=%s",
                    event.unified_msg_origin,
                    json.dumps(raw[:512], ensure_ascii=False),
                    json.dumps(verdict.message[:2500], ensure_ascii=False),
                )
            event.message_str = ""  # Do not let later child filters parse the rejected command.
            event.stop_event()
            raise ValueError(verdict.message)
        event.message_str = verdict.command
        if verdict.command.split()[1] in ("角色", "技能"):
            # AstrBot str arguments may consume only one token. Preserve the full
            # validated name plus optional level on this event, not global state.
            event.set_extra(QUERY_EXTRA, verdict.command.split(" ", 2)[2])
        if verdict.command != raw:
            # No acknowledgement of denied administrator commands or their parameters.
            action = verdict.command.split()[1]
            if event.is_admin() or action not in ADMIN_ACTIONS:
                event.set_extra(CORRECTED_COMMAND, "/" + verdict.command)
                if self.logger:
                    self.logger.info(
                        "[NIKKE] 命令自动纠正：会话=%s，输入=%s，标准命令=%s",
                        event.unified_msg_origin,
                        json.dumps(raw[:512], ensure_ascii=False),
                        json.dumps(verdict.command, ensure_ascii=False),
                    )
        return True
