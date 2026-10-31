import json
import math
import re
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urlsplit

from .network import image_proxy_url
from .prompts import CATEGORIES_JSON, JEV_CRITERIA_JSON, JEV_INSTRUCTIONS, SYSTEM_PROMPT

OFFICIAL_OPENID = "29080-8468458281233829111"
OFFICIAL_LANGUAGES = {"zh-TW": "繁中官方", "en": "全球英文官方", "ja": "日本官方", "ko": "韩国官方"}

GROUP_FIELDS = {
    "base": (
        "enabled",
        "rsshub_base_url",
        "poll_interval_seconds",
        "poll_jitter_seconds",
        "send_images",
        "official_image_proxy",
        "qq_platform_id",
    ),
    "ai_filter": (
        "ai_filter_enabled",
        "ai_filter_provider_id",
        "ai_filter_timeout_seconds",
        "jev_filter_enabled",
        "jev_token",
        "jev_confidence_threshold",
        "jev_model",
        "jev_timeout_seconds",
    ),
    "chat_tools": ("chat_context_enabled", "chat_context_days", "chat_context_max_chars"),
}
GROUP_LABELS = {"base": "基础配置", "ai_filter": "AI 内容筛选", "chat_tools": "聊天查询工具"}


def migrate_layout(config: dict) -> bool:
    """Move retained hidden legacy fields once, before reading grouped settings.

    AstrBot inserts new object defaults before initializing a plugin. Hidden old
    schema fields keep the original values available until this migration runs.
    """
    if config.get("_layout_version") == 1:
        return False
    grouped = {}
    for group, fields in GROUP_FIELDS.items():
        section = config.get(group, {})
        if not isinstance(section, dict):
            raise ValueError(f"{GROUP_LABELS[group]}必须是配置对象")
        grouped[group] = dict(section)
        for key in fields:
            if key in config:
                grouped[group][key] = config[key]
    config.update(grouped)
    for fields in GROUP_FIELDS.values():
        for key in fields:
            config.pop(key, None)
    config["_layout_version"] = 1
    return True


def flatten_config(config: dict) -> dict:
    """Read legacy flat configs as well as new native WebUI object groups."""
    flat = dict(config)
    for group, fields in GROUP_FIELDS.items():
        section = config.get(group, {})
        if not isinstance(section, dict):
            raise ValueError(f"{GROUP_LABELS[group]}必须是配置对象")
        flat.update({key: section[key] for key in fields if key in section})
    return flat


def resolve_targets(
    raw_targets, platform_id: str, qq_platform_ids: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(raw_targets, list):
        raise ValueError("目标 QQ 群号必须是列表")
    targets = []
    for target in raw_targets:
        target = str(target).strip()
        if target.isascii() and target.isdigit() and int(target) > 0:
            selected = platform_id
            if selected:
                if selected not in qq_platform_ids:
                    raise ValueError("指定的 QQ 平台 ID 不存在，请检查已启用的 OneBot 平台")
            elif len(set(qq_platform_ids)) == 1:
                selected = qq_platform_ids[0]
            elif not qq_platform_ids:
                raise ValueError("未找到 OneBot QQ 平台，请启用平台后重载插件")
            else:
                raise ValueError("存在多个 OneBot QQ 平台，请填写 QQ 平台 ID 以选择发送账号")
            target = f"{selected}:GroupMessage:{int(target)}"
        parts = target.split(":")
        if (
            len(parts) != 3
            or not parts[0]
            or parts[1] != "GroupMessage"
            or not parts[2].split("_")[-1].isdigit()
        ):
            raise ValueError("目标群格式不正确，请填写正整数 QQ 群号，例如 123456789")
        if target not in targets:
            targets.append(target)
    return tuple(targets)


@dataclass(frozen=True)
class Subscription:
    name: str
    uid: str
    url: str
    targets: tuple[str, ...]
    source: str = "bilibili"
    language: str = ""
    simplify: bool = True

    @property
    def key(self) -> str:
        if self.source == "blablalink":
            return sha256(f"blablalink:{self.uid}:{self.language}".encode()).hexdigest()
        return sha256(self.url.encode()).hexdigest()

    @property
    def label(self) -> str:
        if self.source == "blablalink":
            return "国际服 / " + OFFICIAL_LANGUAGES[self.language]
        return "bilibili"


@dataclass(frozen=True)
class CalendarSettings:
    enabled: bool = False
    targets: tuple[str, ...] = ()
    hours: tuple[int, ...] = (24, 6)
    categories: tuple[str, ...] = ("recruitment", "story", "raid", "overclock", "costume")
    quiet: bool = True

    @classmethod
    def from_dict(cls, section: dict, platform_id: str, qq_platform_ids: tuple[str, ...]):
        if not isinstance(section, dict):
            raise ValueError("活动倒计时提醒必须是配置对象")
        raw_hours = section.get("hours", [24, 6])
        if not isinstance(raw_hours, list) or not 1 <= len(raw_hours) <= 20:
            raise ValueError("活动提醒提前小时数须为 1 至 20 项列表")
        hours = set()
        for value in raw_hours:
            try:
                hour = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("活动提醒提前小时数须为 1 至 720 的整数") from exc
            if isinstance(value, bool) or str(hour) != str(value).strip() or not 1 <= hour <= 720:
                raise ValueError("活动提醒提前小时数须为 1 至 720 的整数")
            hours.add(hour)
        categories = []
        for category in ("recruitment", "story", "raid", "overclock", "costume", "arena"):
            if bool(section.get(category, category != "arena")):
                categories.append(category)
        return cls(
            bool(section.get("enabled", False)),
            resolve_targets(section.get("targets", []), platform_id, qq_platform_ids),
            tuple(sorted(hours, reverse=True)),
            tuple(categories),
            bool(section.get("quiet", True)),
        )


@dataclass(frozen=True)
class ImageFallbackSettings:
    enabled: bool = False
    uid: str = "3546734206650905"
    url: str = ""
    provider: str = ""
    retry_minutes: int = 5
    confidence: float = 0.9
    window_hours: int = 0
    max_wait_minutes: int = 30

    @classmethod
    def from_dict(cls, value: dict, base: str):
        if not isinstance(value, dict):
            raise ValueError("国际服配图回退必须为配置对象")
        uid = str(value.get("uid", "3546734206650905")).strip()
        if not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
            raise ValueError("配图回退 bilibili UID 必须是正整数")
        numbers = []
        for name, default, lower, upper in (
            ("retry_minutes", 5, 1, 60),
            ("window_hours", 0, 0, 720),
            ("max_wait_minutes", 30, 1, 10080),
        ):
            raw = value.get(name, default)
            try:
                number = int(raw)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"配图回退 {name} 须为 {lower}～{upper} 的整数") from None
            if (
                isinstance(raw, bool)
                or str(number) != str(raw).strip()
                or not lower <= number <= upper
            ):
                raise ValueError(f"配图回退 {name} 须为 {lower}～{upper} 的整数")
            numbers.append(number)
        try:
            confidence = float(value.get("confidence", 0.9))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("配图匹配置信度须为 0～1 的数值") from None
        if (
            isinstance(value.get("confidence"), bool)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("配图匹配置信度须为 0～1 的数值")
        return cls(
            bool(value.get("enabled", False)),
            uid,
            f"{base}/bilibili/user/dynamic/{uid}/showEmoji=0&embed=0",
            str(value.get("provider", "")).strip(),
            numbers[0],
            confidence,
            numbers[1],
            numbers[2],
        )


@dataclass(frozen=True)
class TranslationSettings:
    enabled: bool = False
    provider: str = ""
    wait_minutes: int = 10
    timeout: int = 60
    thinking_mode: str = "auto"
    disable_thinking: bool = True
    custom_parameters: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, section):
        if not isinstance(section, dict):
            raise ValueError("国际服动态翻译必须是配置对象")
        if type(section.get("enabled", False)) is not bool:
            raise ValueError("启用非中文翻译必须为开关")
        values = []
        for key, default, low, high in (("wait_minutes", 10, 0, 1440), ("timeout", 60, 10, 300)):
            value = section.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"翻译 {key} 必须为 {low}～{high} 的整数")
            values.append(value)
        mode = section.get("thinking_mode", "auto")
        if mode not in ("auto", "enable_thinking", "thinking", "custom"):
            raise ValueError("翻译关闭思考方式无效")
        disable = section.get("disable_thinking", True)
        if type(disable) is not bool:
            raise ValueError("关闭翻译思考必须为开关")
        parameters = section.get("custom_parameters", "{}")
        try:
            if isinstance(parameters, str):
                if len(parameters) > 10000:
                    raise ValueError()
                parameters = json.loads(parameters)
            serialized = json.dumps(parameters, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("翻译自定义参数必须为有效 JSON 对象，最多 10000 字符") from exc
        reserved = {
            "model",
            "messages",
            "stream",
            "tools",
            "tool_choice",
            "functions",
            "function_call",
            "api_key",
            "api_base",
            "base_url",
            "extra_headers",
            "extra_query",
            "extra_body",
        }
        if (
            not isinstance(parameters, dict)
            or len(serialized) > 10000
            or any(not isinstance(k, str) or not k.strip() or k in reserved for k in parameters)
        ):
            raise ValueError("翻译自定义参数须为 JSON 键值对，不能覆盖模型、消息、工具或认证字段")
        return cls(
            section.get("enabled", False),
            str(section.get("provider", "")).strip(),
            *values,
            mode,
            disable,
            parameters,
        )


@dataclass(frozen=True)
class Settings:
    enabled: bool
    interval: int
    send_images: bool
    subscriptions: tuple[Subscription, ...]
    jitter: int = 60
    ai_filter_enabled: bool = False
    ai_provider_id: str = ""
    ai_timeout: int = 45
    chat_context_enabled: bool = False
    chat_context_days: int = 7
    chat_context_max_chars: int = 12000
    jev_filter_enabled: bool = False
    jev_token: str = field(default="", repr=False)
    jev_confidence_threshold: float = 0.8
    jev_model: str = "jev-latest"
    jev_timeout: int = 10
    llm_system_prompt: str = SYSTEM_PROMPT
    llm_categories: str = CATEGORIES_JSON
    jev_instructions: str = JEV_INSTRUCTIONS
    jev_criteria: str = JEV_CRITERIA_JSON
    calendar: CalendarSettings = field(default_factory=CalendarSettings)
    official_image_proxy: str = field(default="", repr=False)
    image_fallback: ImageFallbackSettings = field(default_factory=ImageFallbackSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)

    @property
    def default_llm_policy(self) -> bool:
        return self.llm_system_prompt == SYSTEM_PROMPT and json.loads(
            self.llm_categories
        ) == json.loads(CATEGORIES_JSON)

    @property
    def categories(self) -> dict:
        return json.loads(self.llm_categories)

    @property
    def blocked_categories(self) -> tuple[str, ...]:
        return tuple(key for key, value in self.categories.items() if value["filter"])

    @property
    def classification_prompt(self) -> str:
        rules = json.dumps(self.categories, ensure_ascii=False, indent=2)
        return self.llm_system_prompt.replace("${classification_rules}", rules) + (
            '\n只返回 JSON 对象 {"category":"类别标识","reason":"简短中文理由"}。'
            "category 必须是分类条件中定义的标识；filter 由程序处理，不要输出。"
        )

    @property
    def poll_range(self) -> tuple[int, int]:
        return max(60, self.interval - self.jitter), min(3600, self.interval + self.jitter)

    @classmethod
    def from_dict(cls, config: dict, *, qq_platform_ids: tuple[str, ...] = ()) -> "Settings":
        config = flatten_config(config)
        templates = config.get("prompt_templates", {})
        if not isinstance(templates, dict):
            raise ValueError("提示词模板必须是配置对象")
        llm = templates.get("llm", {})
        jev = templates.get("jev", {})
        if not isinstance(llm, dict) or not isinstance(jev, dict):
            raise ValueError("大模型和 Jev 提示词模板必须是配置对象")
        prompt_values = []
        for section, key, default in (
            (llm, "llm_system_prompt", SYSTEM_PROMPT),
            (llm, "llm_categories", CATEGORIES_JSON),
            (jev, "jev_instructions", JEV_INSTRUCTIONS),
            (jev, "jev_criteria", JEV_CRITERIA_JSON),
        ):
            value = section.get(key, default)
            if not isinstance(value, str) or not value.strip() or len(value) > 20000:
                raise ValueError(f"{key} 必须为非空文本，最多 20000 字符")
            prompt_values.append(value)
        if "${classification_rules}" not in prompt_values[0]:
            raise ValueError("大模型提示词必须保留 ${classification_rules} 占位符")
        try:
            categories = json.loads(prompt_values[1])
            criteria = json.loads(prompt_values[3])
        except ValueError as exc:
            raise ValueError("分类条件必须为有效 JSON 对象") from exc
        if not isinstance(categories, dict) or not 1 <= len(categories) <= 20:
            raise ValueError("大模型分类条件必须包含 1 至 20 个类别")
        for key, value in categories.items():
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
                or not isinstance(value, dict)
                or set(value) != {"description", "filter"}
                or not isinstance(value["description"], str)
                or not value["description"].strip()
                or type(value["filter"]) is not bool
            ):
                raise ValueError("类别须使用小写英文标识，包含非空 description 和布尔 filter")
        if (
            not isinstance(criteria, dict)
            or set(criteria) != {"normal", "lottery", "uncertain"}
            or any(not isinstance(v, str) or not v.strip() for v in criteria.values())
        ):
            raise ValueError("Jev 分类条件必须包含 normal、lottery、uncertain 三项非空说明")
        base = str(config.get("rsshub_base_url", "http://localhost:1200")).rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("RSSHub 地址应为不含账号、查询参数和片段的 HTTP(S) 地址")
        try:
            interval = int(config.get("poll_interval_seconds", 300))
        except (TypeError, ValueError) as exc:
            raise ValueError("检查间隔必须是整数") from exc
        if not 60 <= interval <= 3600:
            raise ValueError("检查间隔必须为 60 至 3600 秒")
        raw_jitter = config.get("poll_jitter_seconds", 60)
        try:
            jitter = int(raw_jitter)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("随机扰动必须是 0 至 300 的整数秒数") from exc
        if (
            isinstance(raw_jitter, bool)
            or isinstance(raw_jitter, float)
            and raw_jitter != jitter
            or not 0 <= jitter <= 300
        ):
            raise ValueError("随机扰动必须是 0 至 300 的整数秒数")
        try:
            ai_timeout = int(config.get("ai_filter_timeout_seconds", 45))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("AI 筛选超时必须为 10 至 120 秒") from exc
        if not 10 <= ai_timeout <= 120:
            raise ValueError("AI 筛选超时必须为 10 至 120 秒")
        jev_enabled = bool(config.get("jev_filter_enabled", False))
        raw_token = config.get("jev_token", "")
        jev_token = raw_token.strip() if isinstance(raw_token, str) else ""
        if jev_enabled and (not jev_token or any(c.isspace() for c in jev_token)):
            raise ValueError("启用 Jev 过滤时须填写有效的 Jev Token（不含空白字符）")
        raw_threshold = config.get("jev_confidence_threshold", 0.8)
        try:
            threshold = float(raw_threshold)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Jev 置信度阈值必须为 0 至 1 的数值") from exc
        if (
            isinstance(raw_threshold, bool)
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("Jev 置信度阈值必须为 0 至 1 的数值")
        model = str(config.get("jev_model", "jev-latest")).strip()
        if not model or len(model) > 128 or any(c.isspace() for c in model):
            raise ValueError("Jev 模型 ID 必须非空、不含空白字符且不超过 128 字符")
        raw_timeout = config.get("jev_timeout_seconds", 10)
        try:
            jev_timeout = int(raw_timeout)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Jev 超时必须为 1 至 60 的整数秒数") from exc
        if (
            isinstance(raw_timeout, bool)
            or str(jev_timeout) != str(raw_timeout).strip()
            or not 1 <= jev_timeout <= 60
        ):
            raise ValueError("Jev 超时必须为 1 至 60 的整数秒数")
        context_values = []
        for key, label, default, lower, upper in (
            ("chat_context_days", "单次工具查询天数上限", 7, 1, 365),
            ("chat_context_max_chars", "单次工具返回字符上限", 12000, 1000, 50000),
        ):
            raw_value = config.get(key, default)
            try:
                value = int(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{label}必须为 {lower} 至 {upper} 的整数") from exc
            if (
                isinstance(raw_value, bool)
                or str(value) != str(raw_value).strip()
                or not lower <= value <= upper
            ):
                raise ValueError(f"{label}必须为 {lower} 至 {upper} 的整数")
            context_values.append(value)
        subscriptions = []
        names, sources = set(), set()
        international_targets = {}
        raw = config.get("subscriptions", [])
        if not isinstance(raw, list):
            raise ValueError("订阅配置必须是列表")
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("订阅列表中存在无效项目")
            source = item.get("source", item.get("__template_key", "bilibili"))
            if not item.get("enabled", source != "blablalink"):
                continue
            name = str(item.get("name", "")).strip()
            if not name or any(c.isspace() for c in name) or name in names:
                raise ValueError("订阅名称必须非空、不含空格且不能重复")
            if source not in ("bilibili", "blablalink"):
                raise ValueError("订阅来源必须为 bilibili 或 blablalink")
            language = str(item.get("language", "zh-TW")) if source == "blablalink" else ""
            uid = OFFICIAL_OPENID if source == "blablalink" else str(item.get("uid", "")).strip()
            if source == "blablalink":
                if language not in OFFICIAL_LANGUAGES:
                    raise ValueError("国际服语言须为 zh-TW、en、ja 或 ko")
                url = f"{base}/nikke-blablalink/official/{language}"
            else:
                if not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
                    raise ValueError("bilibili UID 必须是正整数")
                url = f"{base}/bilibili/user/dynamic/{uid}/showEmoji=0&embed=0"
            identity = (source, uid, language)
            if identity in sources:
                raise ValueError("同一 UID 或官方语言请只添加一项订阅，在目标群列表中添加多个群")
            names.add(name)
            sources.add(identity)
            targets = resolve_targets(
                item.get("targets", []),
                str(config.get("qq_platform_id", "")).strip(),
                qq_platform_ids,
            )
            if source == "blablalink":
                for target in targets:
                    # One QQ group must not receive different languages through different bots.
                    group = str(int(target.split(":")[-1].split("_")[-1]))
                    if group in international_targets:
                        other = international_targets[group]
                        raise ValueError(f"QQ 群 {group} 的国际服语言冲突：{other} 与 {name}")
                    international_targets[group] = name
            simplify = item.get("simplify", True)
            if type(simplify) is not bool:
                raise ValueError("繁体转简体必须为开关")
            subscriptions.append(Subscription(name, uid, url, targets, source, language, simplify))
        return cls(
            bool(config.get("enabled", False)),
            interval,
            bool(config.get("send_images", True)),
            tuple(subscriptions),
            jitter,
            bool(config.get("ai_filter_enabled", False)),
            str(config.get("ai_filter_provider_id", "")).strip(),
            ai_timeout,
            bool(config.get("chat_context_enabled", False)),
            *context_values,
            jev_enabled,
            jev_token,
            threshold,
            model,
            jev_timeout,
            *prompt_values,
            CalendarSettings.from_dict(
                config.get("calendar", {}),
                str(config.get("qq_platform_id", "")).strip(),
                qq_platform_ids,
            ),
            image_proxy_url(config.get("official_image_proxy", "")),
            ImageFallbackSettings.from_dict(config.get("image_fallback", {}), base),
            TranslationSettings.from_dict(config.get("translation", {})),
        )
