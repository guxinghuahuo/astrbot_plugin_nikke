import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .calendar_service import CalendarService
from .classifier import Decision
from .config import Settings, Subscription
from .feed import Entry, FeedError, RSSClient
from .image_fallback import ImageFallbackService, original_image_pages
from .presentation import display_entry
from .store import Store
from .translation import ReferencePending, TranslationValidationError

SHANGHAI = timezone(timedelta(hours=8))
SEND_TIMEOUT = 120


def error_summary(exc: Exception) -> str:
    """Keep diagnostics bounded and exclude credentials / message image payloads."""
    detail = str(exc)[:500]
    detail = re.sub(
        r"(?is)(?:cookie|authorization|access_token|token|sessdata|bili_jct)"
        r"[\s\"']*[:=].*",
        "[凭据已隐藏]",
        detail,
    )
    detail = re.sub(r"(?is)base64://.*", "[图片数据已隐藏]", detail)
    if isinstance(exc, asyncio.TimeoutError):
        detail = "请求或发送超时"
    return type(exc).__name__ + ": " + json.dumps(detail, ensure_ascii=False)


def timestamp(value: float | None) -> str:
    if value is None:
        return "未提供时间"
    return datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def message_text(name: str | Subscription, entry: Entry) -> str:
    label = "bilibili"
    if isinstance(name, Subscription):
        entry = display_entry(name, entry)
        label, name = name.label, name.name
    text = entry.text
    # RSSHub flattens line breaks in titles while preserving them in descriptions.
    compact_title = "".join(entry.title.split())
    compact_text = "".join(text.split())
    if compact_title and not compact_text.startswith(compact_title):
        text = entry.title + "\n" + text
    text = text.strip()
    if len(text) > 3500:
        text = text[:3500] + "\n……内容较长，全文请见原链接。"
    author = entry.author[:150] or name
    return (
        f"【{name} · {label}】\n{author}\n{timestamp(entry.published)}（北京时间）"
        f"\n\n{text}\n\n原链接：{entry.link}"
    )


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: RSSClient,
        sender: Callable[[str, str, list[bytes]], Awaitable[None]],
        logger,
        clock=time.time,
        random_uniform=random.uniform,
        classifier: Callable[[Subscription, Entry], Awaitable[Decision]] | None = None,
        image_matcher=None,
        translator=None,
    ):
        self.settings, self.store, self.client = settings, store, client
        self.sender, self.logger, self.clock = sender, logger, clock
        self.random_uniform = random_uniform
        self.classifier = classifier
        self.translator = translator
        self.tasks: list[asyncio.Task] = []
        self.locks = {s.key: asyncio.Lock() for s in settings.subscriptions}
        self.preview_at: dict[str, float] = {}
        self.send_at: dict[str, float] = {}
        self.target_locks: dict[str, asyncio.Lock] = {}
        self.calendar = CalendarService(
            settings.calendar,
            store,
            self._send_calendar,
            logger,
            clock=clock,
            random_uniform=random_uniform,
        )
        self.image_fallback = ImageFallbackService(
            settings,
            store,
            client,
            self._send_supplement,
            lambda target: (
                not self.target_lock(target).locked()
                and self.send_at.get(target, 0) <= self.clock()
            ),
            logger,
            image_matcher,
            clock,
        )

    async def start(self):
        await asyncio.to_thread(self.store.sync, self.settings.subscriptions)
        await self.calendar.plan()
        self._start_tasks()

    def _start_tasks(self):
        if self.tasks:
            return
        if self.settings.enabled:
            self.tasks.extend(
                [
                    asyncio.create_task(self._poll_loop(), name="nikke-rss-poll"),
                    asyncio.create_task(self._send_loop(), name="nikke-rss-send"),
                ]
            )
        if self.settings.calendar.enabled:
            self.tasks.extend(
                [
                    asyncio.create_task(self._calendar_poll_loop(), name="nikke-calendar-poll"),
                    asyncio.create_task(self._calendar_send_loop(), name="nikke-calendar-send"),
                ]
            )
        if self.settings.image_fallback.enabled and self.settings.send_images:
            self.tasks.append(
                asyncio.create_task(self._image_fallback_loop(), name="nikke-image-fallback")
            )

    @asynccontextmanager
    async def pause(self):
        """Drain background work before changing targets, then resume even on failure."""
        try:
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()
            yield
        finally:
            self.tasks.clear()
            self._start_tasks()

    def set_subscriptions(self, subscriptions):
        # All background tasks are paused and command handlers are serialized here.
        self.settings = replace(self.settings, subscriptions=subscriptions)
        self.image_fallback.settings = self.settings

    async def _image_fallback_loop(self):
        while True:
            try:
                await self.image_fallback.deliver_once()
            except Exception as exc:
                self.logger.error("[NIKKE] 配图补充调度异常：%s", error_summary(exc))
            await asyncio.sleep(5)

    async def _calendar_poll_loop(self):
        while True:
            try:
                await self.calendar.tick()
            except Exception as exc:
                self.logger.error("[NIKKE] 活动日历调度异常：%s", error_summary(exc))
            await asyncio.sleep(60)

    async def _calendar_send_loop(self):
        while True:
            try:
                await self.calendar.deliver_once()
            except Exception as exc:
                self.logger.error("[NIKKE] 活动提醒调度异常：%s", error_summary(exc))
            await asyncio.sleep(1)

    def target_lock(self, target: str):
        return self.target_locks.setdefault(target, asyncio.Lock())

    async def _send_calendar(self, target: str, text: str) -> bool:
        return await self._send_supplement(target, text, [])

    async def _send_supplement(self, target, text, images) -> bool:
        lock = self.target_lock(target)
        if lock.locked() or self.send_at.get(target, 0) > self.clock():
            return False
        async with lock:
            try:
                await asyncio.wait_for(self.sender(target, text, images), timeout=SEND_TIMEOUT + 5)
            finally:
                self.send_at[target] = self.clock() + 3
        return True

    async def close(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.client.close()
        await asyncio.to_thread(self.store.close)

    async def _poll_loop(self):
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                self.logger.error("[NIKKE] 轮询异常：%s", error_summary(exc))
            await asyncio.sleep(5)

    async def _send_loop(self):
        while True:
            try:
                await self.deliver_once()
            except Exception as exc:
                self.logger.error("[NIKKE] 投递循环异常：%s", error_summary(exc))
            await asyncio.sleep(1)

    async def poll_once(self):
        if not self.settings.enabled:
            return
        for subscription in self.settings.subscriptions:
            if not subscription.targets:
                continue
            state = await asyncio.to_thread(self.store.source, subscription.key)
            if state.get("next_poll", 0) > self.clock():
                continue
            async with self.locks[subscription.key]:
                self.logger.debug("[NIKKE] 开始轮询：订阅=%s", subscription.name)
                try:
                    entries = await self.client.fetch(subscription.url)
                except Exception as exc:
                    error = str(exc) if isinstance(exc, FeedError) else type(exc).__name__
                    await asyncio.to_thread(
                        self.store.fetch_failed,
                        subscription.key,
                        self.clock(),
                        self.settings.interval,
                        error,
                        self._poll_jitter(),
                    )
                    self.logger.warning(
                        "[NIKKE] %s 获取失败：%s", subscription.name, error_summary(exc)
                    )
                else:
                    await asyncio.to_thread(
                        self.store.ingest,
                        subscription,
                        entries,
                        self.clock(),
                        max(60, min(3600, self.settings.interval + self._poll_jitter())),
                    )
                    status = await asyncio.to_thread(self.store.status, subscription.key)
                    self.logger.debug(
                        "[NIKKE] 轮询成功：订阅=%s，条目=%d，已建基线群=%d，待投递=%d",
                        subscription.name,
                        len(entries),
                        status["initialized"],
                        status["pending"],
                    )
                scheduled = await asyncio.to_thread(self.store.source, subscription.key)
                self.logger.debug(
                    "[NIKKE] 下次自动检查：订阅=%s，时间=%s，约 %.1f 秒后",
                    subscription.name,
                    timestamp(scheduled["next_poll"]),
                    max(0, scheduled["next_poll"] - self.clock()),
                )

    def _poll_jitter(self) -> float:
        if self.settings.jitter == 0:
            return 0
        return self.random_uniform(-self.settings.jitter, self.settings.jitter)

    async def send_entry(
        self,
        subscription: Subscription,
        entry: Entry,
        target: str,
        *,
        queue_id: int | None = None,
        sent_parts: set[str] | None = None,
    ):
        translated = None
        if (
            self.settings.translation.enabled
            and subscription.source == "blablalink"
            and subscription.language != "zh-TW"
            and "text" not in (sent_parts or ())
            and self.translator is None
        ):
            raise FeedError("翻译器未初始化，动态保留待重试")
        if self.translator is not None and "text" not in (sent_parts or ()):
            translated = await self.translator(subscription, entry, target)
        async with self.target_lock(target):
            if queue_id is not None:
                delay = self.send_at.get(target, 0) - self.clock()
                if delay > 0:
                    await asyncio.sleep(min(3, delay))
            try:
                return await self._send_entry(
                    subscription,
                    entry,
                    target,
                    queue_id=queue_id,
                    sent_parts=sent_parts,
                    translated=translated,
                )
            finally:
                self.send_at[target] = self.clock() + 3

    async def _send_entry(
        self, subscription, entry, target, *, queue_id=None, sent_parts=None, translated=None
    ):
        parts = set() if sent_parts is None else set(sent_parts)
        media_entry = entry
        snapshots = [part for part in parts if part.startswith("translation_media:")]
        if translated:
            media_entry = replace(entry, images=translated.entry.images)
        elif "text" in parts and snapshots:
            images = tuple(json.loads(snapshots[0].split(":", 1)[1]))
            media_entry = replace(entry, images=images or entry.images)
            if not images and entry.images and queue_id is not None:
                await asyncio.to_thread(self.store.set_translation_media, queue_id, entry.images)

        async def mark(part):
            if queue_id is not None:
                await asyncio.to_thread(self.store.part_sent, queue_id, part)
            parts.add(part)

        if "text" not in parts:
            if queue_id is not None and (translated or snapshots):
                await asyncio.to_thread(
                    self.store.set_translation_media,
                    queue_id,
                    media_entry.images if translated else None,
                )
            text = message_text(subscription, translated.entry if translated else entry)
            if self.settings.send_images and len(media_entry.images) > 9:
                text += "\n仅附前 9 张图片，其余图片请打开原链接查看。"
            await asyncio.wait_for(self.sender(target, text, []), timeout=SEND_TIMEOUT + 5)
            await mark("text")
            self.logger.info(
                "[NIKKE] 文字已发送：订阅=%s，动态=%s，目标=%s", subscription.name, entry.id, target
            )
        if not self.settings.send_images:
            return
        fallback_enabled = (
            self.settings.image_fallback.enabled and subscription.source == "blablalink"
        )
        if fallback_enabled and entry.images:
            for index, page in enumerate(original_image_pages(entry)):
                token = "original_links:" + str(index)
                if token not in parts:
                    await asyncio.wait_for(self.sender(target, page, []), timeout=SEND_TIMEOUT + 5)
                    await mark(token)
        failures = 0
        download_failures = 0
        for index, url in enumerate(media_entry.images[:9], 1):
            part = "image:" + url
            if part in parts:
                continue
            data = None
            stage = "download"
            try:
                data = await self.client.image(url)
                stage = "upload"
                await asyncio.wait_for(self.sender(target, "", [data]), timeout=SEND_TIMEOUT + 5)
                await mark(part)
                self.logger.info(
                    "[NIKKE] 原图已发送：订阅=%s，动态=%s，目标=%s，序号=%d/%d，字节=%d",
                    subscription.name,
                    entry.id,
                    target,
                    index,
                    min(len(media_entry.images), 9),
                    len(data),
                )
            except Exception as exc:
                failures += 1
                download_failures += int(stage == "download")
                self.logger.warning(
                    "[NIKKE] 原图获取或发送失败：订阅=%s，动态=%s，序号=%d，原因=%s",
                    subscription.name,
                    entry.id,
                    index,
                    error_summary(exc),
                )
            finally:
                data = None  # Never accumulate all original images in memory.
        if failures:
            if (
                fallback_enabled
                and download_failures == min(9, len(media_entry.images))
                and not any(part.startswith("image:") for part in parts)
            ):
                await asyncio.to_thread(
                    self.store.defer_images,
                    subscription,
                    media_entry,
                    target,
                    self.clock(),
                    queue_id,
                )
                self.logger.info(
                    "[NIKKE] 原图全部下载失败，进入独立配图回退队列："
                    "订阅=%s，动态=%s，目标=%s，间隔=%d 分钟",
                    subscription.name,
                    entry.id,
                    target,
                    self.settings.image_fallback.retry_minutes,
                )
                return False
            next_step = (
                "自动推送将保留并重试未完成部分" if queue_id is not None else "请查看日志或重新预览"
            )
            raise FeedError(f"{failures} 张原图未发送成功，{next_step}")

    async def assess(self, subscription: Subscription, entry: Entry) -> Decision:
        if subscription.source == "blablalink":
            return Decision("disabled", "国际服官方动态直接推送，跳过全部内容筛选")
        if not self.settings.ai_filter_enabled:
            return Decision("disabled", "AI 筛选已关闭")
        if self.classifier is None:
            raise FeedError("AI 筛选器未初始化，动态保留待重试")
        return await self.classifier(subscription, entry)

    async def deliver_once(self):
        if not self.settings.enabled:
            return
        subscriptions = {s.key: s for s in self.settings.subscriptions}
        for item in await asyncio.to_thread(self.store.due, self.clock()):
            target = item["target"]
            if self.send_at.get(target, 0) > self.clock():
                continue
            subscription = subscriptions.get(item["source"])
            if subscription is None or target not in subscription.targets:
                continue
            try:
                entry = Entry.from_dict(json.loads(item["payload"]))
                parts = set(json.loads(item["sent_parts"]))
                # A partly sent entry has already passed filtering; resume its media.
                decision = (
                    await self.assess(subscription, entry)
                    if not parts
                    else Decision("news", "继续未完成投递")
                )
                if decision.skip:
                    await asyncio.to_thread(self.store.filtered, item["queue_id"])
                    self.logger.info(
                        "[NIKKE] 已过滤：订阅=%s，动态=%s，目标=%s，类别=%s，理由=%s",
                        subscription.name,
                        entry.id,
                        target,
                        decision.category,
                        json.dumps(decision.reason, ensure_ascii=False),
                    )
                    continue
                completed = await self.send_entry(
                    subscription, entry, target, queue_id=item["queue_id"], sent_parts=parts
                )
            except ReferencePending as exc:
                await asyncio.to_thread(
                    self.store.translation_pending, item["queue_id"], exc.next_try
                )
                self.logger.debug(
                    "[NIKKE] 等待同帖繁中参考：订阅=%s，动态=%s，下次=%s",
                    subscription.name,
                    entry.id,
                    timestamp(exc.next_try),
                )
                continue
            except Exception as exc:
                error = (
                    "翻译校验失败：" + str(exc)
                    if isinstance(exc, TranslationValidationError)
                    else "群消息投递失败：" + type(exc).__name__
                )
                await asyncio.to_thread(
                    self.store.delivery_failed,
                    item["queue_id"],
                    self.clock(),
                    error,
                )
                self.logger.warning(
                    "[NIKKE] 自动投递失败，已安排重试：订阅=%s，目标=%s，动态=%s，原因=%s",
                    subscription.name,
                    target,
                    item["entry_id"],
                    error_summary(exc),
                )
            else:
                if completed is False:
                    continue
                await asyncio.to_thread(self.store.delivered, item["queue_id"])
                self.logger.info(
                    "[NIKKE] 已记录投递成功：订阅=%s，目标=%s，动态=%s",
                    subscription.name,
                    target,
                    item["entry_id"],
                )
            self.send_at[target] = self.clock() + 3

    def subscription(self, name: str) -> Subscription:
        for sub in self.settings.subscriptions:
            if sub.name == name:
                return sub
        raise FeedError("没有找到此启用中的订阅，请检查 WebUI 配置")

    async def inspect(self, name: str) -> tuple[Subscription, list[Entry]]:
        """Archive fetched content without advancing the baseline or delivery state."""
        subscription = self.subscription(name)
        async with self.locks[subscription.key]:
            if self.preview_at.get(subscription.key, 0) > self.clock():
                raise FeedError("手动检查间隔为 30 秒，请稍后重试")
            self.preview_at[subscription.key] = self.clock() + 30
            self.logger.info("[NIKKE] 开始手动获取：订阅=%s", subscription.name)
            entries = await self.client.fetch(subscription.url)
            await asyncio.to_thread(self.store.archive, subscription, entries, self.clock())
            latest = max(entries, key=lambda item: item.published or 0) if entries else None
            self.logger.info(
                "[NIKKE] RSS 获取成功：订阅=%s，条目=%d，最新时间=%s，原链接=%s",
                subscription.name,
                len(entries),
                timestamp(latest.published) if latest else "无",
                latest.link if latest else "无",
            )
            return subscription, entries

    async def status(self) -> str:
        lower, upper = self.settings.poll_range
        lines = [
            "NIKKE 自动推送：" + ("开启" if self.settings.enabled else "关闭"),
            "国际服图片代理："
            + ("已配置" if self.settings.official_image_proxy else "未配置（直连）"),
            f"检查间隔：{self.settings.interval} ± {self.settings.jitter} 秒"
            f"（正常轮询 {lower}～{upper} 秒）",
            "AI 内容筛选（仅 bilibili）："
            + ("开启" if self.settings.ai_filter_enabled else "关闭"),
            "Jev 前置筛选："
            + (
                f"开启，正常放行阈值 {self.settings.jev_confidence_threshold:.2f}"
                if self.settings.ai_filter_enabled and self.settings.jev_filter_enabled
                else "关闭（需同时开启 AI 内容筛选与 Jev 过滤）"
            ),
            "本地动态查询工具："
            + (
                f"开启，单次最多 {self.settings.chat_context_days} 天，"
                f"最多 {self.settings.chat_context_max_chars} 字符"
                if self.settings.chat_context_enabled
                else "关闭"
            ),
        ]
        lines.extend(await self.calendar.status())
        lines.append(
            "国际服非中文翻译："
            + (
                "开启，简体译文，"
                + (
                    "优先关闭思考（不支持则允许），"
                    if self.settings.translation.disable_thinking
                    else "允许思考（模型默认），"
                )
                + "繁中参考最多等待 "
                f"{self.settings.translation.wait_minutes} 分钟"
                if self.settings.translation.enabled
                else "关闭"
            )
        )
        lines.append(
            "国际服配图回退："
            + (
                f"开启，每 {self.settings.image_fallback.retry_minutes} 分钟重试，"
                f"最多等待 {self.settings.image_fallback.max_wait_minutes} 分钟，"
                f"等待补图 {await asyncio.to_thread(self.store.image_job_count)} 条"
                if self.settings.image_fallback.enabled
                else "关闭"
            )
        )
        if not self.settings.subscriptions:
            lines.append("请在 WebUI 添加订阅。")
        for sub in self.settings.subscriptions:
            state = await asyncio.to_thread(self.store.status, sub.key)
            archive = await asyncio.to_thread(self.store.archive_status, sub.key)
            lines.extend(
                [
                    f"\n{sub.name} / {sub.label} / UID {sub.uid}",
                    f"目标会话 {len(sub.targets)}；已建立基线 {state['initialized']}；"
                    f"待投递 {state['pending']}；已过滤投递 {state['filtered']}",
                    f"最近成功获取：{timestamp(state.get('last_success'))}",
                    f"动态存档：{archive['count']} 条；最早 {timestamp(archive['oldest'])}；"
                    f"最新 {timestamp(archive['newest'])}",
                ]
            )
            if state.get("last_error"):
                lines.append("获取错误：" + state["last_error"])
            if not self.settings.enabled:
                lines.append("下次自动检查：已暂停")
            elif not sub.targets:
                lines.append("下次自动检查：未配置目标群")
            elif state.get("next_poll"):
                lines.append("下次自动检查：" + timestamp(state.get("next_poll")))
            else:
                lines.append("下次自动检查：等待首次获取并建立基线")
            if state.get("delivery_error"):
                lines.append(state["delivery_error"])
        return "\n".join(lines)
