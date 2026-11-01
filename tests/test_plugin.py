"""Exercise the AstrBot boundary with fakes, without contacting a real QQ group."""

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nikke.classifier import Decision


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    modules = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.tool",
        "plugin_boundary_test",
    ):
        module = types.ModuleType(name)
        module.__path__ = []
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parents[1]
    modules["plugin_boundary_test"].__path__ = [str(root)]
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("test-plugin")

    def decorator(*args, **kwargs):
        return lambda func: func

    def command(name, **kwargs):
        def register(func):
            func.command_registration = (name, kwargs.get("priority", 0))
            return func

        return register

    class Group:
        def __init__(self, name, func, priority=0):
            self.name, self.description = name, func.__doc__
            self.parent_group = self
            self.priority = priority
            self.custom_filter_list = []
            self.sub_command_filters = []

        def add_custom_filter(self, item):
            self.custom_filter_list.append(item)

        def command(self, name):
            def register(func):
                # AstrBot only accepts extras when the first decorator creates metadata.
                if not hasattr(func, "registration_extras"):
                    func.registration_extras = {"sub_command": True}
                func.command_registration = (self.name + " " + name, 0)
                func.parent_group_name = self.name
                self.sub_command_filters.append(
                    types.SimpleNamespace(
                        command_name=name, handler_md=types.SimpleNamespace(enabled=True)
                    )
                )
                return func

            return register

    def command_group(name, **kwargs):
        return lambda func: Group(name, func, kwargs.get("priority", 0))

    def permission_type(permission):
        def register(func):
            if not hasattr(func, "registration_extras"):
                func.registration_extras = {}
            func.registered_permission = permission
            return func

        return register

    event = modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    event.MessageChain = list
    event.filter = types.SimpleNamespace(
        command=command,
        command_group=command_group,
        on_llm_request=decorator,
        llm_tool=decorator,
        permission_type=permission_type,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    components = modules["astrbot.api.message_components"]
    components.Plain = lambda text: ("text", text)
    components.Image = types.SimpleNamespace(fromBytes=lambda data: ("image", data))
    modules["astrbot.api.provider"].ProviderRequest = object
    modules["astrbot.core.agent.tool"].ToolSet = lambda tools: types.SimpleNamespace(tools=tools)

    class Star:
        def __init__(self, context):
            self.context = context

    star = modules["astrbot.api.star"]
    star.Star, star.Context = Star, object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: tmp_path)
    spec = importlib.util.spec_from_file_location("plugin_boundary_test.main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    yield module.NikkePlugin
    for name in list(sys.modules):
        if name.startswith("plugin_boundary_test.nikke"):
            monkeypatch.delitem(sys.modules, name)


def context(send_result=True, platform_name="aiocqhttp"):
    platform = types.SimpleNamespace(
        meta=lambda: types.SimpleNamespace(id="qq", name=platform_name)
    )
    return types.SimpleNamespace(
        get_config=lambda: {},
        platform_manager=types.SimpleNamespace(get_insts=lambda: [platform]),
        send_message=AsyncMock(return_value=send_result),
    )


def command_entries(plugin_class):
    return {
        func.command_registration[0]: (name, func.command_registration[1])
        for name, func in vars(plugin_class).items()
        if hasattr(func, "command_registration")
    }


async def dispatch_command(plugin, text, event, disabled=()):
    # Waking collects matching enabled handlers in descending priority; STOP ends dispatch.
    entries = command_entries(type(plugin))
    for command, (name, _) in sorted(entries.items(), key=lambda item: -item[1][1]):
        if command in disabled or not (text == command or text.startswith(command + " ")):
            continue
        remaining = text[len(command) :].strip()
        if command in ("nikke 角色", "nikke 技能"):
            # Native AstrBot str defaults consume one token. Preflight must
            # preserve the optional level and multi-token names on the event.
            remaining = remaining.split()[0] if remaining else ""
        if command in ("nikke 检查", "nikke 最新", "nikke 角色", "nikke 技能"):
            await getattr(plugin, name)(event, remaining)
        else:
            await getattr(plugin, name)(event)
        if event.stopped:
            break


def test_native_group_children_and_backend_permission_metadata(plugin_class):
    entries = command_entries(plugin_class)
    assert set(entries) == {
        "nikke 帮助",
        "nikke 状态",
        "nikke 活动",
        "nikke 检查",
        "nikke 最新",
        "nikke 角色",
        "nikke 技能",
    }
    assert plugin_class.nikke.name == "nikke"
    assert plugin_class.nikke.description
    assert plugin_class.nikke.priority > 0
    assert all(
        getattr(plugin_class, name).parent_group_name == "nikke" for name, _ in entries.values()
    )
    for command, (name, _) in entries.items():
        assert getattr(plugin_class, name).registration_extras.get("sub_command") is True
        assert getattr(getattr(plugin_class, name), "registered_permission", None) == (
            "admin" if command in ("nikke 状态", "nikke 检查") else None
        )
    assert all(getattr(plugin_class, name).__doc__ for name, _ in entries.values())


async def preflight_dispatch(plugin, event, text):
    event.message_str = text
    try:
        if not plugin.command_preflight.filter(event, {}):
            return
    except ValueError as exc:
        await event.send(str(exc))
        return
    await dispatch_command(plugin, event.message_str, event)


@pytest.mark.parametrize(
    "text",
    [
        "nikke 最新 nikke资讯",
        "nikke 最新",
        "nikke 最新 NIKKE国际服 多余内容",
        "nikke 最心 NIKKE国际服",
        "nikke 活动 extra",
    ],
)
def test_invalid_commands_have_no_external_or_queue_effects(plugin_class, text):
    async def scenario():
        plugin = plugin_class(
            context(),
            {
                "subscriptions": [
                    {
                        "__template_key": "blablalink",
                        "enabled": True,
                        "name": "NIKKE国际服",
                        "targets": ["100"],
                        "language": "zh-TW",
                    }
                ]
            },
        )
        await plugin.initialize()
        try:
            service = plugin.service
            service.client.fetch = AsyncMock(side_effect=AssertionError("no RSS"))
            service.client.image = AsyncMock(side_effect=AssertionError("no image"))
            service.classifier = AsyncMock(side_effect=AssertionError("no model"))
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, text)
            assert event.stopped and not event.call_llm and len(event.replies) == 1
            service.client.fetch.assert_not_awaited()
            service.client.image.assert_not_awaited()
            service.classifier.assert_not_awaited()
            assert service.preview_at == {} and service.store.image_job_count() == 0
            assert not service.store.recent_entries(
                tuple(s.key for s in service.settings.subscriptions), 0, 1e20
            )
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["nikke国际服", "ＮＩＫＫＥ國際服", "NIKKE国际腹"])
def test_auto_correction_acknowledges_then_dispatches_once(plugin_class, name):
    async def scenario():
        plugin = plugin_class(
            context(),
            {
                "subscriptions": [
                    {
                        "__template_key": "blablalink",
                        "enabled": True,
                        "name": "NIKKE国际服",
                        "targets": ["100"],
                        "language": "zh-TW",
                    }
                ]
            },
        )
        await plugin.initialize()
        try:
            plugin._handle_command = AsyncMock()
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 最新 " + name)
            plugin._handle_command.assert_awaited_once_with(event, "最新", "NIKKE国际服")
            assert event.replies == ["已识别为：/nikke 最新 NIKKE国际服"]
            assert event.stopped and not event.call_llm
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_preflight_reload_cleanup_noncommands_permissions_and_disabled_child(plugin_class):
    async def scenario():
        plugin = plugin_class(
            context(), {"subscriptions": [{"name": "秘密订阅", "uid": "1", "targets": ["100"]}]}
        )
        await plugin.initialize()
        group = plugin_class.nikke.parent_group
        old = plugin.command_preflight
        plugin._bind_command_preflight()
        assert old not in group.custom_filter_list and group.custom_filter_list == [
            plugin.command_preflight
        ]
        event = FakeEvent(admin=False)
        event.message_str = "nikke 最新 wrong"
        event.is_at_or_wake_command = False
        assert not plugin.command_preflight.filter(event, {}) and event.call_llm
        event.is_at_or_wake_command = True
        for text in ("nikkex 最新 wrong", "随便聊聊 nikke 最新", "hello"):
            event.message_str = text
            assert not plugin.command_preflight.filter(event, {}) and event.call_llm
        event.message_str = "NIKKE 检查 wrong extra"
        assert plugin.command_preflight.filter(event, {})  # native permission filter owns denial
        assert "秘密订阅" not in event.message_str and not event.call_llm
        child = next(c for c in group.sub_command_filters if c.command_name == "最新")
        child.handler_md.enabled = False
        event = FakeEvent(admin=False)
        event.message_str = "nikke 最新 秘密订阅"
        with pytest.raises(ValueError, match="停用"):
            plugin.command_preflight.filter(event, {})
        await plugin.terminate()
        assert not group.custom_filter_list

    asyncio.run(scenario())


@pytest.mark.parametrize("target,expected", [("100", "已停用"), ("200", "当前不可用于本会话")])
def test_preflight_disabled_subscription_visibility_uses_resolved_group(
    plugin_class, target, expected
):
    async def scenario():
        plugin = plugin_class(
            context(),
            {
                "subscriptions": [
                    {"name": "NIKKE官方", "uid": "1", "enabled": False, "targets": [target]},
                    {
                        "__template_key": "blablalink",
                        "name": "NIKKE国际服",
                        "enabled": True,
                        "targets": ["100"],
                    },
                ]
            },
        )
        await plugin.initialize()
        try:
            plugin._handle_command = AsyncMock()
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 最新 NIKKE官方")
            assert expected in event.replies[0]
            plugin._handle_command.assert_not_awaited()
            assert not event.call_llm and event.stopped
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command,name",
    [("检查", "NIKKE官方"), ("最新", "NIKKE国际服"), ("状态", ""), ("活动", ""), ("帮助", "")],
)
def test_subcommand_routes_once_and_consumes_message(plugin_class, command, name):
    async def scenario():
        plugin = plugin_class(context(), {})
        plugin._handle_command = AsyncMock()
        event = FakeEvent()
        await dispatch_command(plugin, "nikke " + command + (" " + name if name else ""), event)
        plugin._handle_command.assert_awaited_once_with(event, command, name)
        assert event.stopped and not event.call_llm

    asyncio.run(scenario())


def test_group_does_not_install_a_flat_fallback_that_bypasses_disabled_children(plugin_class):
    async def scenario():
        plugin = plugin_class(context(), {})
        plugin._handle_command = AsyncMock()
        event = FakeEvent()
        await dispatch_command(plugin, "nikke 最新 NIKKE国际服", event, ("nikke 最新",))
        plugin._handle_command.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    ["nikke 状态", "nikke 检查 国际服"],
)
def test_every_registered_command_requires_admin_and_blocks_llm(plugin_class, command):
    async def scenario():
        plugin = plugin_class(context(), {})
        plugin._handle_command = AsyncMock()
        event = FakeEvent(admin=False)
        await dispatch_command(plugin, command, event)
        plugin._handle_command.assert_not_awaited()
        assert len(event.replies) == 1 and "仅限" in event.replies[0]
        assert event.stopped and not event.call_llm

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["帮助", "活动", "最新", "角色", "技能"])
def test_public_children_allow_non_admin_and_consume_message(plugin_class, command):
    async def scenario():
        plugin = plugin_class(context(), {})
        plugin._handle_command = AsyncMock()
        event = FakeEvent(admin=False)
        await dispatch_command(plugin, "nikke " + command, event)
        plugin._handle_command.assert_awaited_once_with(event, command, "")
        assert event.stopped and not event.call_llm

    asyncio.run(scenario())


def test_character_query_preserves_full_arguments_and_disabled_child(plugin_class):
    async def scenario():
        plugin = plugin_class(context(), {})
        await plugin.initialize()
        try:
            plugin.characters.query = AsyncMock(return_value=["技能一", "技能二", "爆裂技能"])
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 技能 拉毗：小红帽 7")
            plugin.characters.query.assert_awaited_once_with("拉毗：小红帽 7", skills=True)
            assert event.replies == ["技能一", "技能二", "爆裂技能"]
            assert event.stopped and not event.call_llm
            assert event.get_extra("nikke_character_query") is None
            plugin.characters.query.reset_mock()
            child = next(
                c for c in plugin_class.nikke.sub_command_filters if c.command_name == "技能"
            )
            child.handler_md.enabled = False
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 技能 拉毗：小红帽 7")
            assert "停用" in event.replies[0]
            plugin.characters.query.assert_not_awaited()
        finally:
            await plugin.terminate()
        assert plugin.characters is None

    asyncio.run(scenario())


def test_character_source_error_is_a_consumed_reply(plugin_class):
    async def scenario():
        plugin = plugin_class(context(), {})
        await plugin.initialize()
        try:
            error = plugin._handle_command.__globals__["CharacterError"]
            plugin.characters.query = AsyncMock(side_effect=error("官方角色资料读取失败"))
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 角色 拉毗")
            assert "官方角色资料读取失败" in event.replies[0]
            assert event.stopped and not event.call_llm
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("modern", [False, True])
def test_character_image_switch_renders_combined_card_with_native_t2i(plugin_class, modern):
    async def scenario():
        plugin = plugin_class(context(), {"base": {"character_image_enabled": True}})
        await plugin.initialize()
        renderer = AsyncMock(return_value="/tmp/character.png")

        async def legacy(tmpl, data, return_url=True, options=None):
            return await renderer(tmpl, data, return_url=return_url, options=options)

        async def current(tmpl, data, return_url=True, options=None, umo=None):
            return await renderer(tmpl, data, return_url=return_url, options=options, umo=umo)

        plugin.html_render = current if modern else legacy
        try:
            plugin.characters.query = AsyncMock(
                return_value=[
                    "【小红帽 · 技能1 Lv.10】\n怒目相视\n攻击力10%",
                    "【小红帽 · 技能2 Lv.10】\n野兽之牙\n持续10秒",
                    "【小红帽 · 爆裂技能 Lv.10】\n大红狼\n冷却40秒",
                ]
            )
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 技能 小红帽")
            assert event.replies == [("image", "/tmp/character.png")]
            renderer.assert_awaited_once()
            call = renderer.await_args
            assert len(call.args[1]["sections"]) == 3
            assert call.kwargs["return_url"] is False
            assert call.kwargs["options"]["type"] == "jpeg"
            assert call.kwargs["options"]["quality"] == 90
            if modern:
                assert call.kwargs["umo"] == event.unified_msg_origin
            assert event.stopped and not event.call_llm
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("offline"), TimeoutError(), None])
def test_character_render_failure_does_not_dump_long_text(plugin_class, failure):
    async def scenario():
        plugin = plugin_class(context(), {"base": {"character_image_enabled": True}})
        await plugin.initialize()
        plugin.html_render = AsyncMock(side_effect=failure, return_value="")
        try:
            plugin.characters.query = AsyncMock(
                return_value=["【小红帽 · 资料】\n" + "攻击力" * 200]
            )
            event = FakeEvent(admin=False)
            await preflight_dispatch(plugin, event, "nikke 角色 小红帽")
            assert len(event.replies) == 1 and "t2i" in event.replies[0]
            assert "攻击力" not in event.replies[0]
            assert event.stopped and not event.call_llm
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("group", ["100", "200", ""])
def test_public_latest_remains_limited_to_subscription_target_group(plugin_class, group):
    async def scenario():
        plugin = plugin_class(context(), {})
        event = FakeEvent(group=group, admin=False)
        sub = types.SimpleNamespace(name="国际服", targets=("qq:GroupMessage:100",))
        plugin.service = types.SimpleNamespace(
            subscription=lambda name: sub,
            inspect=AsyncMock(return_value=(sub, [types.SimpleNamespace(published=100)])),
            assess=AsyncMock(return_value=Decision("disabled", "国际服直接推送")),
            send_entry=AsyncMock(),
        )
        await dispatch_command(plugin, "nikke 最新 国际服", event)
        if group == "100":
            plugin.service.send_entry.assert_awaited_once()
        else:
            plugin.service.inspect.assert_not_awaited()
            plugin.service.send_entry.assert_not_awaited()
            assert "目标群" in event.replies[0] or "配置的群" in event.replies[0]
        assert event.stopped and not event.call_llm

    asyncio.run(scenario())


def test_number_config_resolves_before_preview_and_send(plugin_class):
    async def scenario():
        plugin = plugin_class(
            context(), {"subscriptions": [{"name": "NIKKE", "uid": "1", "targets": ["100"]}]}
        )
        await plugin.initialize()
        try:
            assert not plugin.config_error
            sub = plugin.service.subscription("NIKKE")
            assert sub.targets == ("qq:GroupMessage:100",)
            event = FakeEvent("100")
            plugin.service.inspect = AsyncMock(return_value=(sub, []))
            await plugin._run_command(event, "最新", "NIKKE")
            plugin.service.inspect.assert_awaited_once_with("NIKKE")
            assert "空订阅" in event.replies[0]
            await plugin._send(sub.targets[0], "test", [])
            plugin.context.send_message.assert_awaited_once_with(
                "qq:GroupMessage:100", [("text", "test")]
            )
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("second_enabled", [False, True])
def test_number_config_resolves_at_cold_start(plugin_class, second_enabled):
    async def scenario():
        ctx = context()
        ctx.platform_manager.get_insts = lambda: []
        ctx.get_config = lambda: {
            "platform": [
                {"id": "qq", "type": "aiocqhttp", "enable": True},
                {"id": "second", "type": "aiocqhttp", "enable": second_enabled},
                {"id": "other", "type": "telegram", "enable": True},
            ]
        }
        plugin = plugin_class(
            ctx, {"subscriptions": [{"name": "NIKKE", "uid": "1", "targets": ["100"]}]}
        )
        await plugin.initialize()
        try:
            if second_enabled:
                assert "多个 OneBot" in plugin.config_error
                assert plugin.service is None
            else:
                assert not plugin.config_error
                assert plugin.service.subscription("NIKKE").targets == ("qq:GroupMessage:100",)
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


class FakeEvent:
    """Model the old result-based STOP API as well as explicit LLM suppression."""

    def __init__(self, group="100", admin=True):
        self.group = group
        self.admin = admin
        self.unified_msg_origin = "qq:GroupMessage:" + group
        self.stopped = False
        self.call_llm = True
        self.replies = []
        self._extras = {}
        self.message_str = ""
        self.is_at_or_wake_command = True

    def get_message_str(self):
        return self.message_str

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        return self._extras if key is None else self._extras.get(key, default)

    def should_call_llm(self, value):
        self.call_llm = value

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return self.admin

    def get_group_id(self):
        return self.group

    def plain_result(self, text):
        return text

    def image_result(self, path):
        return ("image", path)

    async def send(self, result):
        assert self.stopped and not self.call_llm  # Before any reply, not just after.
        self.replies.append(result)


def test_send_validates_platform_and_propagates_failure(plugin_class):
    async def scenario():
        ctx = context()
        plugin = plugin_class(ctx, {})
        await plugin._send("qq:GroupMessage:100", "hello", [b"image"])
        assert ctx.send_message.await_args_list == [
            (("qq:GroupMessage:100", [("text", "hello")]), {}),
            (("qq:GroupMessage:100", [("image", b"image")]), {}),
        ]
        ctx.send_message.return_value = False
        with pytest.raises(RuntimeError, match="未找到"):
            await plugin._send("qq:GroupMessage:100", "hello", [])
        ctx.send_message.side_effect = RuntimeError("offline")
        with pytest.raises(RuntimeError, match="offline"):
            await plugin._send("qq:GroupMessage:100", "hello", [])
        for target in ("missing:GroupMessage:100",):
            with pytest.raises(RuntimeError, match="OneBot"):
                await plugin._send(target, "hello", [])
        wrong = plugin_class(context(platform_name="webchat"), {})
        with pytest.raises(RuntimeError, match="OneBot"):
            await wrong._send("qq:GroupMessage:100", "hello", [])
        wrong.context.send_message.assert_not_awaited()

    asyncio.run(scenario())


def test_initialize_disabled_and_cleanup(plugin_class):
    async def scenario():
        ctx = context()
        plugin = plugin_class(ctx, {"enabled": False})
        await plugin.initialize()
        service = plugin.service
        assert service is not None and not service.tasks
        await plugin.terminate()
        assert plugin.service is None
        ctx.send_message.assert_not_awaited()
        invalid = plugin_class(ctx, {"rsshub_base_url": "invalid"})
        await invalid.initialize()
        assert invalid.config_error and invalid.service is None
        await invalid.terminate()

    asyncio.run(scenario())


@pytest.mark.parametrize("admin", [True, False])
def test_calendar_command_keeps_permission_and_llm_interception(plugin_class, admin):
    async def scenario():
        plugin, event = plugin_class(context(), {}), FakeEvent(admin=admin)
        listing = AsyncMock(return_value="国际服活动倒计时")
        plugin.service = types.SimpleNamespace(calendar=types.SimpleNamespace(listing=listing))
        await plugin._run_command(event, "活动")
        assert event.stopped and not event.call_llm
        assert len(event.replies) == 1
        listing.assert_awaited_once()
        assert event.replies[0] == "国际服活动倒计时"

    asyncio.run(scenario())


def test_preview_rejects_unconfigured_group_without_fetch(plugin_class):
    async def scenario():
        plugin = plugin_class(context(), {})
        plugin.service = types.SimpleNamespace(
            subscription=lambda name: types.SimpleNamespace(targets=("qq:GroupMessage:100",)),
            inspect=AsyncMock(),
        )
        event = FakeEvent(group="200")
        await plugin._run_command(event, "最新", "NIKKE官方")
        assert event.replies == ["请在该订阅配置的目标群中预览。"]
        assert event.stopped and not event.call_llm
        plugin.service.inspect.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()

    asyncio.run(scenario())


def test_latest_stops_pipeline_before_fetch_and_logs_body(plugin_class, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        ctx, event = context(), FakeEvent()
        plugin = plugin_class(ctx, {})
        sub = types.SimpleNamespace(name="NIKKE官方", targets=(event.unified_msg_origin,))
        latest = types.SimpleNamespace(published=100)

        async def inspect(name):
            assert event.stopped and not event.call_llm
            return sub, [latest]

        async def send_entry(sub, entry, target):
            await plugin._send(target, "官方动态正文\n原链接：https://t.bilibili.com/1", [b"img"])

        plugin.service = types.SimpleNamespace(
            subscription=lambda name: sub,
            inspect=inspect,
            assess=AsyncMock(return_value=Decision("disabled", "off")),
            send_entry=AsyncMock(side_effect=send_entry),
        )
        await plugin._run_command(event, "最新", "NIKKE官方")
        assert event.stopped and not event.call_llm
        assert ctx.send_message.await_count == 2
        assert event.replies == []  # Only the actual preview, no redundant confirmation.

    asyncio.run(scenario())
    assert "收到命令" in caplog.text
    assert "官方动态正文" in caplog.text and "图片=1" in caplog.text
    assert "发送成功" in caplog.text and "已拦截" in caplog.text


@pytest.mark.parametrize("action,name", [("帮助", ""), ("状态", ""), ("最新", ""), ("未知", "")])
def test_non_preview_replies_are_logged_and_consumed(plugin_class, caplog, action, name):
    caplog.set_level(logging.INFO)

    async def scenario():
        plugin, event = plugin_class(context(), {}), FakeEvent()
        plugin.service = types.SimpleNamespace(status=AsyncMock(return_value="自动推送关闭"))
        await plugin._run_command(event, action, name)
        assert event.stopped and not event.call_llm
        assert len(event.replies) == 1
        assert event.replies[0].splitlines()[0] in caplog.text
        assert "命令回复成功" in caplog.text
        plugin.context.send_message.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["permission", "config", "uninitialized", "feed", "send", "reply"])
def test_error_paths_do_not_fall_through_to_llm(plugin_class, caplog, mode):
    caplog.set_level(logging.INFO)

    async def scenario():
        ctx, event = context(), FakeEvent(admin=mode != "permission")
        plugin = plugin_class(ctx, {})
        sub = types.SimpleNamespace(name="NIKKE官方", targets=(event.unified_msg_origin,))

        async def send_entry(sub, entry, target):
            await plugin._send(target, "动态", [])

        plugin.service = types.SimpleNamespace(
            subscription=lambda name: sub,
            inspect=AsyncMock(return_value=(sub, [types.SimpleNamespace(published=100)])),
            assess=AsyncMock(return_value=Decision("disabled", "off")),
            send_entry=AsyncMock(side_effect=send_entry),
        )
        if mode == "config":
            plugin.config_error = "配置无效"
        elif mode == "uninitialized":
            plugin.service = None
        elif mode == "feed":
            feed_error = plugin._run_command.__globals__["FeedError"]
            plugin.service.inspect.side_effect = feed_error("RSSHub HTTP 503")
        elif mode == "send":
            ctx.send_message.side_effect = RuntimeError("OneBot offline")
        elif mode == "reply":
            plugin.service = None
            event.send = AsyncMock(side_effect=RuntimeError("reply offline"))
        if mode == "reply":
            with pytest.raises(RuntimeError, match="reply offline"):
                await plugin._run_command(event, "最新", "NIKKE官方")
        else:
            await plugin._run_command(
                event, "检查" if mode == "permission" else "最新", "NIKKE官方"
            )
            assert len(event.replies) == 1
        assert event.stopped and not event.call_llm
        if mode != "send":
            ctx.send_message.assert_not_awaited()
        if mode == "permission":
            plugin.service.inspect.assert_not_awaited()
        if mode in ("feed", "send", "reply"):
            assert "失败" in caplog.text
            assert "发送成功" not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["最新", "检查"])
def test_manual_filtered_preview_does_not_send_dynamic(plugin_class, action):
    async def scenario():
        ctx, event = context(), FakeEvent()
        plugin = plugin_class(ctx, {})
        sub = types.SimpleNamespace(name="NIKKE官方", targets=(event.unified_msg_origin,))
        plugin.service = types.SimpleNamespace(
            subscription=lambda name: sub,
            inspect=AsyncMock(
                return_value=(
                    sub,
                    [
                        types.SimpleNamespace(
                            published=100,
                            link="https://t.bilibili.com/1",
                        )
                    ],
                )
            ),
            assess=AsyncMock(return_value=Decision("giveaway", "纯转发抽奖")),
            send_entry=AsyncMock(),
        )
        await plugin._run_command(event, action, "NIKKE官方")
        assert len(event.replies) == 1 and "纯转发抽奖" in event.replies[0]
        assert "过滤" in event.replies[0]
        assert event.stopped and not event.call_llm
        ctx.send_message.assert_not_awaited()
        plugin.service.send_entry.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["enabled", "disabled", "other_group", "private", "empty", "store_error"]
)
def test_local_tool_scope_and_no_automatic_data_injection(plugin_class, mode):
    async def scenario():
        plugin = plugin_class(
            context(),
            {
                "chat_context_enabled": mode != "disabled",
                "chat_context_days": 7,
                "chat_context_max_chars": 1000,
                "subscriptions": [{"name": "NIKKE", "uid": "1", "targets": ["100"]}],
            },
        )
        await plugin.initialize()
        try:
            from datetime import datetime, timedelta, timezone

            from plugin_boundary_test.nikke.feed import Entry

            service = plugin.service
            now = datetime(2026, 10, 24, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
            service.clock = lambda: now
            sub = service.subscription("NIKKE")
            if mode != "empty":
                service.store.archive(
                    sub,
                    [
                        Entry(
                            "chosen",
                            "昨天公告",
                            "角色更新",
                            "https://t.bilibili.com/chosen",
                            "官方",
                            now - 86400,
                            (),
                        ),
                        Entry(
                            "today",
                            "今天公告",
                            "今天资讯",
                            "https://t.bilibili.com/today",
                            "官方",
                            now,
                            (),
                        ),
                    ],
                    now,
                )
            calls = []
            original_query = service.store.recent_entries

            def query(*args):
                calls.append(args)
                if mode == "store_error":
                    raise RuntimeError("busy")
                return original_query(*args)

            service.store.recent_entries = query
            service.client.fetch = AsyncMock()
            event = FakeEvent(
                "other" if mode == "other_group" else "" if mode == "private" else "100"
            )
            own_tool = types.SimpleNamespace(name="nikke_query_official_posts")
            other_tool = types.SimpleNamespace(name="other_tool")
            shared_tools = types.SimpleNamespace(tools=[own_tool, other_tool])
            extra = [types.SimpleNamespace(text="其他插件资料")]
            req = types.SimpleNamespace(
                prompt="普通聊天",
                system_prompt="人设",
                contexts=[],
                extra_user_content_parts=extra,
                func_tool=shared_tools,
            )
            await plugin.configure_official_tool(event, req)
            await plugin.configure_official_tool(event, req)
            assert calls == []  # No SQLite reads before a model tool call.
            assert req.prompt == "普通聊天" and req.system_prompt == "人设"
            assert req.contexts == [] and req.extra_user_content_parts is extra and len(extra) == 1
            assert shared_tools.tools == [own_tool, other_tool]  # Shared set is unchanged.
            allowed = mode not in ("disabled", "other_group", "private")
            assert req.func_tool.tools == ([own_tool, other_tool] if allowed else [other_tool])
            result = await plugin.query_official_posts(event, "昨天", "昨天")
            assert not event.stopped
            if mode == "enabled":
                assert len(result) <= 1000
                assert "昨天公告" in result and "今天公告" not in result
                assert len(calls) == 1
            elif mode == "empty":
                assert result.endswith("[]") and "并非完整历史" in result
            elif mode == "store_error":
                assert "读取失败" in result
            else:
                assert "未开启" in result and calls == []
            service.client.fetch.assert_not_awaited()
            plugin.context.send_message.assert_not_awaited()
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_tool_validates_dates_before_reading_and_handles_no_tools(plugin_class):
    async def scenario():
        plugin = plugin_class(
            context(),
            {
                "chat_context_enabled": True,
                "subscriptions": [{"name": "NIKKE", "uid": "1", "targets": ["100"]}],
            },
        )
        await plugin.initialize()
        try:
            plugin.service.store.recent_entries = AsyncMock()
            event = FakeEvent()
            req = types.SimpleNamespace(func_tool=None)
            await plugin.configure_official_tool(event, req)
            assert req.func_tool is None  # Do not force tools on unsupported providers.
            result = await plugin.query_official_posts(event, "garbage", "今天")
            assert "查询参数有误" in result
            plugin.service.store.recent_entries.assert_not_called()
            await plugin.terminate()
            assert "未开启" in await plugin.query_official_posts(event, "今天", "今天")
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
