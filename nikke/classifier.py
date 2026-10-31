"""Optional text-only classification using an existing AstrBot model provider."""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass

from .config import Settings, Subscription
from .feed import Entry, FeedError
from .jev import CACHE_SECONDS, JevResult, evaluate
from .jev import POLICY_VERSION as JEV_POLICY_VERSION
from .store import Store

POLICY_VERSION = "nikke-content-v2"

REPOST_MARKER = re.compile(r"(?://\s*)?转发自\s*[:：]")


def split_post(text: str) -> tuple[str, str, bool]:
    """RSSHub flattens reposts; isolate the latest author's words before quoting."""
    parts = REPOST_MARKER.split(text, maxsplit=1)
    return parts[0].strip(), parts[1].strip() if len(parts) == 2 else "", len(parts) == 2


def obvious_winner_notice(current_text: str) -> bool:
    """Only shortcut explicit congratulations plus prize-collection instructions.

    Broader cases stay with the model. Never inspect the quoted original here.
    """
    # Leave negation and quoted dialogue to semantic classification.
    if re.search(r"未|没有|并非|不是|不要|无需|不必|[“「]", current_text):
        return False
    congratulations = re.match(r"^恭喜[^。！？\n]{0,60}(?:中奖|获奖)", current_text)
    collection = re.search(
        r"(?:私信|联系|回复|提交|填写)[^。！？\n]{0,30}(?:收货|地址|领奖|领取奖品)",
        current_text,
    )
    return bool(congratulations and collection)


@dataclass(frozen=True)
class Decision:
    category: str
    reason: str
    blocked: tuple[str, ...] = ("giveaway_result", "giveaway", "engagement")

    @property
    def skip(self) -> bool:
        return self.category in self.blocked


def parse_decision(
    text: str,
    categories: tuple[str, ...] = ("giveaway_result", "giveaway", "engagement", "news", "other"),
    blocked: tuple[str, ...] = ("giveaway_result", "giveaway", "engagement"),
) -> Decision:
    if not isinstance(text, str) or len(text) > 4096:
        raise FeedError("AI 筛选返回无效，稍后重试")
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise FeedError("AI 筛选未返回有效 JSON，稍后重试") from exc
    if (
        not isinstance(data, dict)
        or data.get("category") not in categories
        or not isinstance(data.get("reason"), str)
        or not data["reason"].strip()
        or len(data["reason"]) > 300
    ):
        raise FeedError("AI 筛选分类或理由无效，稍后重试")
    return Decision(data["category"], data["reason"].strip(), blocked)


class ContentFilter:
    def __init__(
        self,
        context,
        settings: Settings,
        store: Store,
        logger,
        clock=time.time,
        jev_evaluator=evaluate,
    ):
        self.context, self.settings, self.store = context, settings, store
        self.logger, self.clock = logger, clock
        self.lock = asyncio.Lock()
        self.failed_until: dict[str, float] = {}
        self.jev_evaluator = jev_evaluator
        self.jev_retry_at = 0

    async def assess(self, sub: Subscription, entry: Entry) -> Decision:
        if sub.source == "blablalink":
            return Decision("disabled", "国际服官方动态直接推送，跳过全部内容筛选")
        if not self.settings.ai_filter_enabled:
            return Decision("disabled", "AI 筛选已关闭")
        current, quoted, is_repost = split_post(entry.text[:12000])
        if self.settings.default_llm_policy and obvious_winner_notice(current):
            decision = Decision(
                "giveaway_result", "本次正文明确恭喜中奖并通知提交领奖信息，忽略引用旧文的资讯"
            )
            self.logger.info(
                "[NIKKE] 内容筛选规则命中：订阅=%s，动态=%s，分类=%s，过滤=True，理由=%s",
                sub.name,
                entry.id,
                decision.category,
                decision.reason,
            )
            return decision
        payload = json.dumps(
            {
                "title": entry.title[:1000],
                "current_text": current,
                "quoted_text": quoted,
                "is_repost": is_repost,
            },
            ensure_ascii=False,
        )
        if self.settings.jev_filter_enabled and self.settings.default_llm_policy:
            decision = await self._jev_precheck(sub, entry, payload)
            if decision is not None:
                return decision
        # Resolve the LLM only after Jev: confident normal posts need no provider.
        try:
            provider = self.settings.ai_provider_id
            if not provider:
                if not sub.targets:
                    raise FeedError("请指定 AI 模型提供商 ID 或配置目标群")
                provider = await asyncio.wait_for(
                    self.context.get_current_chat_provider_id(umo=sub.targets[0]),
                    timeout=self.settings.ai_timeout,
                )
        except FeedError:
            raise
        except Exception as exc:
            raise FeedError("无法获取 AI 筛选模型，请检查 AstrBot 提供商配置") from exc
        key = hashlib.sha256(
            json.dumps(
                [
                    POLICY_VERSION,
                    provider,
                    sub.key,
                    entry.id,
                    payload,
                    self.settings.classification_prompt,
                    self.settings.blocked_categories,
                ]
            ).encode()
        ).hexdigest()
        async with self.lock:
            cached = await asyncio.to_thread(self.store.filter_result, key)
            if cached is not None:
                return Decision(
                    cached["category"], cached["reason"], self.settings.blocked_categories
                )
            if self.failed_until.get(key, 0) > self.clock():
                raise FeedError("AI 筛选暂不可用，此条动态将在稍后重试")
            try:
                self.logger.info("[NIKKE] AI 开始分类：订阅=%s，动态=%s", sub.name, entry.id)
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider,
                        prompt=payload,
                        system_prompt=self.settings.classification_prompt,
                        contexts=[],
                        tools=None,
                        max_tokens=256,
                    ),
                    timeout=self.settings.ai_timeout,
                )
                decision = parse_decision(
                    response.completion_text,
                    tuple(self.settings.categories),
                    self.settings.blocked_categories,
                )
            except Exception as exc:
                self.failed_until[key] = self.clock() + 60
                # Do not log provider exception bodies: they may include credentials.
                self.logger.warning("[NIKKE] AI 分类失败：异常类型=%s", type(exc).__name__)
                if isinstance(exc, FeedError):
                    raise
                raise FeedError("AI 筛选调用失败或超时，动态保留待重试") from exc
            await asyncio.to_thread(
                self.store.save_filter_result,
                key,
                sub.key,
                decision.category,
                decision.reason,
            )
            self.failed_until.pop(key, None)
            self.logger.info(
                "[NIKKE] AI 判定：订阅=%s，动态=%s，分类=%s，过滤=%s，理由=%s",
                sub.name,
                entry.id,
                decision.category,
                decision.skip,
                json.dumps(decision.reason, ensure_ascii=False),
            )
            return decision

    async def _jev_precheck(self, sub: Subscription, entry: Entry, payload: str) -> Decision | None:
        key = hashlib.sha256(
            json.dumps(
                [
                    JEV_POLICY_VERSION,
                    self.settings.jev_model,
                    sub.key,
                    entry.id,
                    payload,
                    self.settings.jev_instructions,
                    json.loads(self.settings.jev_criteria),
                ]
            ).encode()
        ).hexdigest()
        async with self.lock:
            try:
                cached = await asyncio.to_thread(self.store.jev_result, key)
                if cached and 0 <= self.clock() - cached["created_at"] <= CACHE_SECONDS:
                    result = JevResult(cached["choice"], cached["confidence"], cached["model"])
                elif self.clock() < self.jev_retry_at:
                    self.logger.info(
                        "[NIKKE] Jev 暂缓请求，转交大模型：订阅=%s，动态=%s", sub.name, entry.id
                    )
                    return None
                else:
                    result = await asyncio.wait_for(
                        self.jev_evaluator(self.settings, json.loads(payload)),
                        timeout=self.settings.jev_timeout,
                    )
                    await asyncio.to_thread(
                        self.store.save_jev_result,
                        key,
                        sub.key,
                        result.choice,
                        result.confidence,
                        result.model,
                        self.clock(),
                    )
            except Exception as exc:
                self.jev_retry_at = self.clock() + 60
                # Exception messages or HTTP bodies may contain credentials.
                status = getattr(exc, "status", None)
                self.logger.warning(
                    "[NIKKE] Jev 请求或结果异常，转交大模型：异常类型=%s，HTTP=%s",
                    type(exc).__name__,
                    status if type(status) is int else "无",
                )
                return None
            passed = (
                result.choice == "normal"
                and result.confidence >= self.settings.jev_confidence_threshold
            )
            self.logger.info(
                "[NIKKE] Jev 判定：订阅=%s，动态=%s，分类=%s，置信度=%.4f，阈值=%.4f，处理=%s",
                sub.name,
                entry.id,
                result.choice,
                result.confidence,
                self.settings.jev_confidence_threshold,
                "直接放行" if passed else "转交大模型",
            )
            if passed:
                return Decision(
                    "news",
                    f"Jev 判定正常，置信度 {result.confidence:.4f} 达到阈值 "
                    f"{self.settings.jev_confidence_threshold:.4f}，跳过大模型",
                )
            return None
