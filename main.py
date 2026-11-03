import asyncio
import inspect
import json
from copy import deepcopy

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.tool import ToolSet

from .nikke.character_cards import CARD_OPTIONS, TEMPLATE, build_cards
from .nikke.characters import QUERY_EXTRA, CharacterCatalog, CharacterError
from .nikke.chat import TOOL_NAME, build_context, query_window
from .nikke.classifier import ContentFilter
from .nikke.command_correction import CORRECTED_COMMAND, CommandPreflight
from .nikke.config import Settings, migrate_layout, resolve_targets
from .nikke.feed import FeedError, RSSClient
from .nikke.image_fallback import ImageMatcher
from .nikke.outpost import OutpostError
from .nikke.outpost import query as query_outpost
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
        self.characters: CharacterCatalog | None = None

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
        self.characters = CharacterCatalog(
            data_dir / "characters", logger, proxy=settings.official_image_proxy
        )
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
            "[NIKKE] 插件 v1.3.0 已加载，自动推送=%s，检查间隔=%s±%s 秒",
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
            self.characters = None

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

    async def _reply_character(self, event: AstrMessageEvent, pages: list[str], *, subtitle=None):
        if not self.service.settings.character_image_enabled:
            for page in pages:
                await self._reply(event, page)
            return
        try:
            cards = build_cards(pages, **({"subtitle": subtitle} if subtitle else {}))
            kwargs = {"return_url": False, "options": CARD_OPTIONS}
            # AstrBot 4.10.4 uses the global renderer. Newer releases can also
            # resolve the t2i endpoint from a session's bound configuration.
            if "umo" in inspect.signature(self.html_render).parameters:
                kwargs["umo"] = event.unified_msg_origin
            images = []
            for card in cards:
                path = await asyncio.wait_for(self.html_render(TEMPLATE, card, **kwargs), 45)
                if not isinstance(path, str) or not path:
                    raise ValueError("文转图未返回图片")
                images.append(path)
        except Exception as exc:
            logger.warning("[NIKKE] 查询资料图片渲染失败：%s", type(exc).__name__)
            await self._reply(
                event,
                "查询资料图片渲染失败，请检查 AstrBot 配置的本地 t2i 服务。"
                "关闭插件的“角色与技能图片回复”可查看文字。",
            )
            return
        for path in images:
            await event.send(event.image_result(path))
        logger.info(
            "[NIKKE] 角色图片回复成功：会话=%s，图片=%d", event.unified_msg_origin, len(images)
        )

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
        """NIKKE 动态与活动；状态、检查、订阅和退订仅限 AstrBot 管理员。"""
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

    @nikke.command("角色")
    async def nikke_character(self, event: AstrMessageEvent, name: str = ""):
        """查询国际服角色基础资料，官方繁中资料转简体，无需登录。"""
        await self._run_command(event, "角色", name)

    @nikke.command("技能")
    async def nikke_skill(self, event: AstrMessageEvent, name: str = ""):
        """查询角色技能，可在名称后指定 1–10 级，默认 10 级。"""
        await self._run_command(event, "技能", name)

    @nikke.command("基地等级")
    async def nikke_outpost(self, event: AstrMessageEvent, level: str = ""):
        """查询基地满学院每小时产出，示例：基地等级 533。"""
        await self._run_command(event, "基地等级", level)

    @nikke.command("红球")
    async def nikke_core_dust(self, event: AstrMessageEvent, stage: str = ""):
        """按通关进度查询红球，示例：红球 h48-32 或 红球 33-23 h18-32。"""
        await self._run_command(event, "红球", stage)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("检查")
    async def nikke_check(self, event: AstrMessageEvent, name: str = ""):
        """检查指定订阅的 RSS、最新时间与原链接；不建立基线或推送动态。"""
        await self._run_command(event, "检查", name)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("订阅")
    async def nikke_subscribe(self, event: AstrMessageEvent, name: str = ""):
        """为当前 QQ 群或私聊账号订阅指定来源；省略名称列出来源。"""
        await self._run_command(event, "订阅", name)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("退订")
    async def nikke_unsubscribe(self, event: AstrMessageEvent, name: str = ""):
        """取消当前会话的指定来源订阅，不影响其他群或账号。"""
        await self._run_command(event, "退订", name)

    @nikke.command("最新")
    async def nikke_latest(self, event: AstrMessageEvent, name: str = ""):
        """在订阅目标群发送最新图文；支持 bilibili 和国际服订阅名称。"""
        await self._run_command(event, "最新", name)

    async def _run_command(self, event: AstrMessageEvent, action: str = "帮助", name: str = ""):
        event.should_call_llm(False)
        event.stop_event()
        if action in ("角色", "技能", "红球"):
            name = event.get_extra(QUERY_EXTRA) or name
            event.set_extra(QUERY_EXTRA, None)
        logger.info(
            "[NIKKE] 收到命令：会话=%s，操作=%s，订阅=%s",
            event.unified_msg_origin,
            json.dumps(action, ensure_ascii=False),
            json.dumps(name, ensure_ascii=False),
        )
        try:
            # Check permission inside the handler so denied commands are consumed too.
            if action in ("状态", "检查", "订阅", "退订") and not event.is_admin():
                await self._reply(event, "此命令仅限 AstrBot 管理员使用。")
                return
            corrected = event.get_extra(CORRECTED_COMMAND)
            if corrected:
                event.set_extra(CORRECTED_COMMAND, None)
                if action not in ("基地等级", "红球"):
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
            if action in ("订阅", "退订"):
                await self._change_subscription(event, action, name)
                return
            if action in ("基地等级", "红球"):
                try:
                    pages = await asyncio.to_thread(query_outpost, action, name)
                except OutpostError as exc:
                    await self._reply(event, str(exc))
                    return
                for page in pages:
                    await self._reply(event, page)
                return
            if action in ("角色", "技能"):
                if self.characters is None:
                    await self._reply(event, "角色资料服务尚未初始化，请重载插件。")
                    return
                try:
                    pages = await self.characters.query(name, skills=action == "技能")
                except CharacterError as exc:
                    await self._reply(event, str(exc))
                    return
                await self._reply_character(event, pages)
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
                "/nikke 基地等级 533（查询基地每小时产出）\n"
                "/nikke 红球 h48-32（默认普通全通）\n"
                "/nikke 红球 33-23 h18-32（普通、困难顺序不限）\n"
                "/nikke 角色 角色名（国际服基础资料，繁中转简体）\n"
                "/nikke 技能 角色名 [等级1–10]（默认10级，无需登录）\n"
                "/nikke 检查 订阅名称（管理员，只检查来源）\n"
                "/nikke 订阅 [订阅名称]（管理员，订阅当前群或私聊账号）\n"
                "/nikke 退订 [订阅名称]（管理员，退订当前会话）\n"
                "/nikke 最新 订阅名称（所有人，发送到当前已配置的群）\n"
                "国服与国际服用法相同，名称须与 WebUI 的订阅名称一致。\n"
                "国际服示例：/nikke 检查 NIKKE国际服\n"
                "省略订阅/退订名称时先列出来源；来源在 WebUI 配置。",
            )

    async def _change_subscription(self, event: AstrMessageEvent, action: str, name: str):
        service = self.service
        origin = event.unified_msg_origin
        platforms = self._qq_platform_ids()
        try:
            target = resolve_targets([origin], "", platforms)[0]
            if target.split(":", 1)[0] not in platforms:
                raise ValueError("平台不受支持")
        except (ValueError, IndexError):
            await self._reply(event, "请在已启用的 OneBot QQ 群或私聊中使用订阅指令。")
            return
        subs = service.settings.subscriptions
        available = subs if action == "订阅" else tuple(s for s in subs if target in s.targets)
        if not name:
            lines = [
                f"/nikke {action} {s.name}" + ("（已订阅）" if target in s.targets else "")
                for s in available
            ]
            await self._reply(
                event,
                "请指定订阅名称：\n" + "\n".join(lines)
                if lines
                else (
                    "没有已启用的来源，请先在 WebUI 添加并启用订阅来源。"
                    if action == "订阅"
                    else "当前会话没有已启用的动态订阅。"
                ),
            )
            return
        sub = next((s for s in subs if s.name == name), None)
        if sub is None:
            await self._reply(event, f"未找到已启用的订阅来源。请执行 /nikke {action} 查看名称。")
            return
        adding = action == "订阅"
        if (target in sub.targets) == adding:
            state = "已订阅" if adding else "尚未订阅"
            await self._reply(event, f"当前会话{state} {name}，无需修改。")
            return
        candidate = deepcopy(dict(self.config))
        raw = next(
            item for item in candidate["subscriptions"]
            if str(item.get("name", "")).strip() == name
            and item.get(
                "enabled",
                item.get("source", item.get("__template_key", "bilibili")) != "blablalink",
            )
        )
        raw["targets"] = list(sub.targets) + [target] if adding else [
            value for value in sub.targets if value != target
        ]
        try:
            settings = Settings.from_dict(candidate, qq_platform_ids=platforms)
        except ValueError as exc:
            await self._reply(event, "无法修改订阅：" + str(exc))
            return
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            await self._reply(event, "配置保存接口不可用，订阅未修改，请重载插件后重试。")
            return
        previous = self.config["subscriptions"]

        def commit():
            self.config["subscriptions"] = candidate["subscriptions"]
            try:
                save()
                service.store.sync(settings.subscriptions)
            except Exception:
                self.config["subscriptions"] = previous
                save()
                raise

        async def apply():
            await asyncio.to_thread(commit)
            service.set_subscriptions(settings.subscriptions)

        try:
            async with service.pause():
                # Do not resume polling while a cancelled worker is still saving/syncing.
                task = asyncio.create_task(apply())
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        except Exception as exc:
            logger.error("[NIKKE] 保存订阅失败：%s", error_summary(exc))
            await self._reply(event, "订阅修改失败，请查看日志并重试。")
            return
        note = (
            "首次成功获取只建立基线，不补发旧动态。"
            if adding
            else "已取消该来源在当前会话的待发送动态，不影响其他会话。"
        )
        if adding and not settings.enabled:
            note += "\n自动推送总开关当前关闭，请在 WebUI 开启后接收新动态。"
        await self._reply(event, f"当前会话已{action} {name}。\n{note}")
