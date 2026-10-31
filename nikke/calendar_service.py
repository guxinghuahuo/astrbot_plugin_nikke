"""Calendar refresh and durable reminder delivery, independent of RSS/AI switches."""

import asyncio
import random
import time

from .calendar import (
    CALENDAR_LINK,
    CalendarClient,
    date_text,
    in_quiet,
    remaining,
    reminder_pages,
)
from .chinese import simplified
from .feed import FeedError


class CalendarService:
    def __init__(
        self,
        settings,
        store,
        sender,
        logger,
        *,
        clock=time.time,
        random_uniform=random.uniform,
        client=None,
    ):
        self.settings, self.store, self.sender, self.logger = settings, store, sender, logger
        self.clock, self.random_uniform = clock, random_uniform
        self.client = client if client is not None else CalendarClient()
        self.fetch_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.planned_at = float("-inf")

    async def refresh(self, *, before_send=False):
        async with self.fetch_lock:
            now = self.clock()
            state = await asyncio.to_thread(self.store.calendar_state)
            if state["retry_at"] > now:
                return
            if before_send:
                if state["updated"] is not None and 0 <= now - state["updated"] <= 900:
                    return
            elif state["next_poll"] > now:
                return
            try:
                payload, events = await self.client.fetch()
            except Exception as exc:
                # Never retain response bodies or arbitrary remote error text.
                error = "官方日历获取失败：" + (
                    str(exc) if isinstance(exc, FeedError) else type(exc).__name__
                )
                await asyncio.to_thread(self.store.calendar_failed, self.clock(), error)
                self.logger.warning("[NIKKE] %s；继续使用有效缓存", error)
            else:
                interval = 3600 + self.random_uniform(-300, 300)
                await asyncio.to_thread(
                    self.store.calendar_success, payload, events, self.clock(), interval
                )
                self.logger.debug(
                    "[NIKKE] 活动日历更新：活动=%d，下次约 %.0f 秒后", len(events), interval
                )

    async def plan(self):
        await asyncio.to_thread(self.store.calendar_plan, self.settings, self.clock())
        self.planned_at = self.clock()

    async def tick(self):
        if self.settings.enabled:
            await self.refresh()
        await self.plan()

    async def deliver_once(self):
        if not self.settings.enabled or in_quiet(self.clock(), self.settings.quiet):
            return
        async with self.delivery_lock:
            if self.clock() - self.planned_at >= 60:
                await self.plan()
            rows = await asyncio.to_thread(self.store.calendar_due, self.settings, self.clock())
            if not rows:
                return
            await self.refresh(before_send=True)
            await self.plan()
            state = await asyncio.to_thread(self.store.calendar_state)
            now = self.clock()
            if state["updated"] is None or not 0 <= now - state["updated"] <= 21600:
                return
            # Re-read after refresh: official extensions/cancellations invalidate old rows.
            rows = await asyncio.to_thread(self.store.calendar_due, self.settings, now)
            targets = dict.fromkeys(row["target"] for row in rows)
            for target in targets:
                # Recheck on every page/target because uploads and network calls take time.
                now = self.clock()
                if in_quiet(now, self.settings.quiet) or now - state["updated"] > 21600:
                    return
                current = await asyncio.to_thread(self.store.calendar_due, self.settings, now)
                pages = reminder_pages(
                    [r for r in current if r["target"] == target], now, state["updated"]
                )
                if not pages:
                    continue
                text, ids = pages[0]  # A later loop sends the next page after the group cooldown.
                try:
                    sent = await self.sender(target, text)
                except Exception as exc:
                    await asyncio.to_thread(
                        self.store.calendar_send_failed,
                        ids,
                        self.clock(),
                        "活动提醒发送失败：" + type(exc).__name__,
                    )
                    self.logger.warning(
                        "[NIKKE] 活动提醒失败，已安排重试：目标=%s，异常=%s",
                        target,
                        type(exc).__name__,
                    )
                else:
                    if sent:
                        await asyncio.to_thread(self.store.calendar_sent, ids)
                        self.logger.info(
                            "[NIKKE] 活动提醒已发送：目标=%s，提醒数=%d", target, len(ids)
                        )

    async def listing(self) -> str:
        await self.refresh(before_send=True)
        state = await asyncio.to_thread(self.store.calendar_state)
        events = await asyncio.to_thread(self.store.calendar_events, self.clock())
        lines = ["【NIKKE 国际服活动】时间均为北京时间", "数据更新：" + date_text(state["updated"])]
        if state["last_error"]:
            lines.append("刷新失败，当前为缓存数据。")
        if state["updated"] is not None and self.clock() - state["updated"] > 21600:
            lines.append("数据已超过 6 小时，自动提醒已暂停。")
        displayed = 0
        for event in events:
            if event["category"] not in self.settings.categories:
                continue
            displayed += 1
            upcoming = "（尚未开始）" if event["start"] > self.clock() else ""
            lines.append(
                f"\n{simplified(event['title'])}{upcoming} · {simplified(event['notice'])}\n"
                f"剩余 {remaining(event['end'], self.clock())}；"
                f"截止 {date_text(event['end'])}"
            )
        if not displayed:
            lines.append("暂无可显示的活动；尚未获取数据时不代表官方没有活动。")
        lines.append("官方日历：" + CALENDAR_LINK)
        # Commands use a bounded single message; full official listing is linked above.
        result = "\n".join(lines)
        if len(result) > 3400:
            result = result[:3200] + "\n……更多活动请见官方日历：" + CALENDAR_LINK
        return result

    async def status(self) -> list[str]:
        state = await asyncio.to_thread(self.store.calendar_state)
        counts = await asyncio.to_thread(self.store.calendar_counts)
        return [
            "活动倒计时提醒："
            + ("开启" if self.settings.enabled else "关闭")
            + f"；目标群 {len(self.settings.targets)}；提前小时 {self.settings.hours}",
            "夜间提前汇总："
            + ("开启（23:00 提前，00:00～08:00 静默）" if self.settings.quiet else "关闭"),
            "日历最近成功刷新：" + date_text(state["updated"]),
            "日历下次自动刷新："
            + (date_text(state["next_poll"]) if self.settings.enabled else "已暂停"),
            f"活动提醒队列：待发送 {counts.get('pending', 0)}；已发送 {counts.get('sent', 0)}；"
            f"已跳过 {counts.get('skipped', 0)}；已取消 {counts.get('cancelled', 0)}",
            "日历获取状态："
            + (state["last_error"] or ("正常" if state["updated"] is not None else "尚未获取")),
        ]
