import asyncio
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.tool import ToolSet

from .nikke.chat import TOOL_NAME, build_context, query_window
from .nikke.classifier import ContentFilter
from .nikke.command_correction import CORRECTED_COMMAND, CommandPreflight
from .nikke.config import Settings, migrate_layout
from .nikke.feed import FeedError, RSSClient
from .nikke.image_fallback import ImageMatcher
from .nikke.service import SEND_TIMEOUT, Service, error_summary, timestamp
from .nikke.store import Store
from .nikke.translation import Translator


class NikkePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service: Service | None = None
        self.config_error = ""
        self.preview_lock = asyncio.Lock()
        self.command_preflight = None

    def _bind_command_preflight(self):
        group = type(self).nikke.parent_group
        group.custom_filter_list[:] = [
            item
            for item in group.custom_filter_list
            if not getattr(item, "nikke_usage_filter", False)
        ]
        self.command_preflight = CommandPreflight(self, group, logger)
        group.add_custom_filter(self.command_preflight)

    def _unbind_command_preflight(self):
        if self.command_preflight is not None:
            group = self.command_preflight.group
            group.custom_filter_list[:] = [
                item for item in group.custom_filter_list if item is not self.command_preflight
            ]
            self.command_preflight = None

    async def initialize(self):
        self._bind_command_preflight()
        try:
            if migrate_layout(self.config):
                save = getattr(self.config, "save_config", None)
                if callable(save):
                    save()
                logger.info("[NIKKE] 配置已迁移为功能分组")
            settings = Settings.from_dict(
                self.config,
                qq_platform_ids=self._qq_platform_ids(),
            )
        except ValueError as exc:
            self.config_error = str(exc)
            logger.error("[NIKKE] 配置错误：%s", self.config_error)
            return
        data_dir = StarTools.get_data_dir("astrbot_plugin_nikke")
        store = await asyncio.to_thread(Store, data_dir / "subscriptions.sqlite3")
        client = RSSClient(official_image_proxy=settings.official_image_proxy)
        content_filter = ContentFilter(self.context, settings, store, logger)
        image_matcher = ImageMatcher(self.context, settings, store, logger)
        translator = Translator(self.context, settings, store, client, logger)
        self.service = Service(
            settings,
            store,
            client,
            self._send,
            logger,
            classifier=content_filter.assess,
            image_matcher=image_matcher.match,
            translator=translator.render,
        )
        try:
            await self.service.start()
        except BaseException:
            self._unbind_command_preflight()
            await self.service.close()
            self.service = None
            raise
        logger.info(
            "[NIKKE] 插件 v1.0.0 已加载，自动推送=%s，检查间隔=%s±%s 秒",
            settings.enabled,
            settings.interval,
            settings.jitter,
        )
        if settings.ai_filter_enabled and not settings.default_llm_policy:
            logger.info(
                "[NIKKE] 自定义大模型筛选规则已生效：暂停本地中奖规则和 Jev 预筛选，"
                "bilibili 动态交给大模型按自定义分类条件判断；国际服直接推送"
            )

    def _qq_platform_ids(self) -> tuple[str, ...]:
        # AstrBot 4.10.4 loads plugins before platform instances on cold startup.
        # Use enabled platform configuration so resolution also works after reboot.
        platforms = self.context.get_config().get("platform")
        if platforms is not None:
            return tuple(
                str(p["id"])
                for p in platforms
                if p.get("enable") and p.get("type") == "aiocqhttp" and p.get("id")
            )
        return tuple(
            p.meta().id
            for p in self.context.platform_manager.get_insts()
            if p.meta().name == "aiocqhttp"
        )

    async def terminate(self):
        self._unbind_command_preflight()
        # Finish an in-flight admin preview before disposing shared HTTP/SQLite resources.
        async with self.preview_lock:
            if self.service:
                await self.service.close()
                self.service = None

    async def _send(self, target: str, text: str, images: list[bytes]):
        logger.info(
            "[NIKKE] 准备发送：目标=%s，图片=%d，图片字节=%d，正文=%s",
            target,
            len(images),
            sum(map(len, images)),
            json.dumps(text, ensure_ascii=False),
        )
        try:
            await self._send_chain(target, text, images)
        except Exception as exc:
            logger.error("[NIKKE] 发送失败：目标=%s，原因=%s", target, error_summary(exc))
            raise
        logger.info("[NIKKE] 发送成功：目标=%s，平台调用已返回", target)

    async def _send_chain(self, target: str, text: str, images: list[bytes]):
        platform_id = target.split(":", 1)[0]
        platform = next(
            (p for p in self.context.platform_manager.get_insts() if p.meta().id == platform_id),
            None,
        )
        if platform is None or platform.meta().name != "aiocqhttp":
            raise RuntimeError("目标不是已连接的 OneBot 平台")
        if text:
            found = await asyncio.wait_for(
                self.context.send_message(target, MessageChain([Plain(text)])),
                timeout=SEND_TIMEOUT,
            )
            if not found:
                raise RuntimeError("未找到目标群所属平台")
        for data in images:
            found = await asyncio.wait_for(
                self.context.send_message(target, MessageChain([Image.fromBytes(data)])),
                timeout=SEND_TIMEOUT,
            )
            if not found:
                raise RuntimeError("未找到目标群所属平台")

    async def _reply(self, event: AstrMessageEvent, text: str):
        logger.info(
            "[NIKKE] 命令回复：会话=%s，正文=%s",
            event.unified_msg_origin,
            json.dumps(text, ensure_ascii=False),
        )
        # Direct send preserves STOP on older AstrBot versions, where yielding a
        # new MessageEventResult would replace the stopped result with CONTINUE.
        await event.send(event.plain_result(text))
        logger.info("[NIKKE] 命令回复成功：会话=%s", event.unified_msg_origin)

    def _chat_subscriptions(self, event: AstrMessageEvent):
        if (
            self.service is None
            or not self.service.settings.chat_context_enabled
            or not event.get_group_id()
        ):
            return ()
        return tuple(
            sub
            for sub in self.service.settings.subscriptions
            if event.unified_msg_origin in sub.targets
        )

    @filter.on_llm_request(priority=-100)
    async def configure_official_tool(self, event: AstrMessageEvent, req: ProviderRequest):
        # Run after AstrBot's default-priority hook populates the request tools.
        # Only remove our tool from this request; never mutate a shared ToolSet.
        if req.func_tool is not None and not self._chat_subscriptions(event):
            req.func_tool = ToolSet(
                [tool for tool in req.func_tool.tools if tool.name != TOOL_NAME]
            )

    @filter.llm_tool(name="nikke_query_official_posts")
    async def query_official_posts(
        self, event: AstrMessageEvent, start_date: str, end_date: str
    ) -> str:
        """按需查询本地 SQLite 的 NIKKE 官方动态，供回答公告、活动等问题参考。

        根据对话选择相关日期，不需要资料时不要调用。日期按北京时间计算，首尾日期均包含。
        查询今天可两个参数都填今天。
        一次可查连续多天，超过配置上限会返回缩小范围提示；不访问网站，不发送群消息。
        结果仅覆盖已抓取的存档；空结果不代表官方当天没有发布，需向用户说明资料不足。

        Args:
            start_date(string): 开始日期，YYYY-MM-DD、今天、昨天或 N天前，例如 3天前。
            end_date(string): 结束日期，YYYY-MM-DD、今天、昨天或 N天前，例如 今天。
        """
        async with self.preview_lock:
            subscriptions = self._chat_subscriptions(event)
            if not subscriptions:
                return "本地官方动态查询未开启，或当前会话不在配置的目标 QQ 群中。"
            service = self.service
            try:
                now = service.clock()
                since, until, days, label = query_window(
                    start_date, end_date, now, service.settings.chat_context_days
                )
                rows = await asyncio.to_thread(
                    service.store.recent_entries,
                    tuple(sub.key for sub in subscriptions),
                    since,
                    until,
                )
                text = build_context(
                    subscriptions,
                    rows,
                    days,
                    now,
                    service.settings.chat_context_max_chars,
                    date_range=label,
                )
                logger.info(
                    "[NIKKE] 本地动态工具查询：会话=%s，范围=%s，候选动态=%d，返回字符=%d",
                    event.unified_msg_origin,
                    label,
                    len(rows),
                    len(text),
                )
                return text
            except ValueError as exc:
                return "查询参数有误：" + str(exc)
            except Exception as exc:
                logger.warning("[NIKKE] 本地动态工具查询失败：%s", error_summary(exc))
                return "本地动态资料暂时读取失败，请说明当前无法确认官方信息，稍后重试。"

    @filter.command_group("nikke", priority=100)
    async def nikke(self, event: AstrMessageEvent):
        """NIKKE 动态与活动：帮助、最新、活动所有人可用；状态、检查仅管理员。"""
        await self._run_command(event, "帮助")

    @nikke.command("帮助")
    async def nikke_help(self, event: AstrMessageEvent):
        """显示 NIKKE 动态与活动查询的完整指令用法。"""
        await self._run_command(event, "帮助")

    # Inner child registration must create metadata before the outer permission decorator.
    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("状态")
    async def nikke_status(self, event: AstrMessageEvent):
        """查看订阅、自动推送、图片代理、活动提醒和投递队列状态。"""
        await self._run_command(event, "状态")

    @nikke.command("活动")
    async def nikke_events(self, event: AstrMessageEvent):
        """查询国际服官方活动、截止时间与实际剩余时间。"""
        await self._run_command(event, "活动")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("检查")
    async def nikke_check(self, event: AstrMessageEvent, name: str = ""):
        """检查指定订阅的 RSS、最新时间与原链接；不建立基线或推送动态。"""
        await self._run_command(event, "检查", name)

    @nikke.command("最新")
    async def nikke_latest(self, event: AstrMessageEvent, name: str = ""):
        """在订阅目标群发送最新图文；支持 bilibili 和国际服订阅名称。"""
        await self._run_command(event, "最新", name)

    async def _run_command(self, event: AstrMessageEvent, action: str = "帮助", name: str = ""):
        event.should_call_llm(False)
        event.stop_event()
        logger.info(
            "[NIKKE] 收到命令：会话=%s，操作=%s，订阅=%s",
            event.unified_msg_origin,
            json.dumps(action, ensure_ascii=False),
            json.dumps(name, ensure_ascii=False),
        )
        try:
            # Check permission inside the handler so denied commands are consumed too.
            if action in ("状态", "检查") and not event.is_admin():
                await self._reply(event, "此命令仅限 AstrBot 管理员使用。")
                return
            corrected = event.get_extra(CORRECTED_COMMAND)
            if corrected:
                event.set_extra(CORRECTED_COMMAND, None)
                await self._reply(event, "已识别为：" + corrected)
            await self._handle_command(event, action, name)
        except Exception as exc:
            logger.error(
                "[NIKKE] 命令处理失败：会话=%s，原因=%s",
                event.unified_msg_origin,
                error_summary(exc),
            )
            raise
        finally:
            event.should_call_llm(False)
            event.stop_event()
            logger.info("[NIKKE] 命令结束：已拦截后续处理和默认 LLM")

    async def _handle_command(self, event: AstrMessageEvent, action: str, name: str):
        if self.config_error:
            await self._reply(event, "NIKKE 配置错误：" + self.config_error)
            return
        if self.service is None:
            await self._reply(event, "插件尚未初始化，请在 WebUI 重载。")
            return
        async with self.preview_lock:
            service = self.service
            if service is None:
                return
            if action == "活动":
                await self._reply(event, await service.calendar.listing())
                return
            if action == "状态":
                await self._reply(event, await service.status())
                return
            if action in ("检查", "最新"):
                if not name:
                    await self._reply(event, f"用法：/nikke {action} 订阅名称")
                    return
                # Manual previews are only sent to a configured target group.
                if action == "最新":
                    try:
                        sub = service.subscription(name)
                    except FeedError as exc:
                        await self._reply(event, str(exc))
                        return
                    if not event.get_group_id() or event.unified_msg_origin not in sub.targets:
                        await self._reply(event, "请在该订阅配置的目标群中预览。")
                        return
                try:
                    sub, entries = await service.inspect(name)
                    if not entries:
                        await self._reply(event, "RSS 返回有效的空订阅；没有可预览的动态。")
                        return
                    latest = max(entries, key=lambda item: item.published or 0)
                    decision = await service.assess(sub, latest)
                    filter_note = (
                        f"\nAI 判定：{decision.category}；"
                        f"{'过滤' if decision.skip else '保留'}；{decision.reason}"
                        if decision.category != "disabled"
                        else ""
                    )
                    if action == "检查":
                        await self._reply(
                            event,
                            f"{sub.name}：RSS 有效，共 {len(entries)} 条。\n"
                            f"最新时间：{timestamp(latest.published)}\n"
                            f"原链接：{latest.link}\n检查不会建立基线或自动推送。{filter_note}",
                        )
                    elif decision.skip:
                        await self._reply(event, "此条动态已被 AI 筛选，不发送图文。" + filter_note)
                    else:
                        await service.send_entry(sub, latest, event.unified_msg_origin)
                except FeedError as exc:
                    logger.warning("[NIKKE] 手动检查失败：%s", error_summary(exc))
                    await self._reply(event, str(exc))
                except Exception as exc:
                    logger.warning("[NIKKE] 检查或发送失败：%s", error_summary(exc))
                    await self._reply(event, "检查或发送失败，请查看插件状态和日志。")
                return
            await self._reply(
                event,
                "/nikke 帮助（所有人）\n/nikke 状态（管理员）\n"
                "/nikke 活动（所有人，查询国际服活动倒计时）\n"
                "/nikke 检查 订阅名称（管理员，只检查来源）\n"
                "/nikke 最新 订阅名称（所有人，发送到当前已配置的群）\n"
                "国服与国际服用法相同，名称须与 WebUI 的订阅名称一致。\n"
                "国际服示例：/nikke 检查 NIKKE国际服\n"
                "订阅请在 WebUI 配置；保存后自动重载生效。",
            )
