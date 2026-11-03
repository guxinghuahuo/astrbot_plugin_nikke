import json
from pathlib import Path

import pytest

from nikke.config import GROUP_FIELDS, Settings, migrate_layout


def config():
    return {
        "subscriptions": [
            {
                "name": "NIKKE官方",
                "uid": "3546734206650905",
                "targets": ["test-bot:GroupMessage:123456789"],
            }
        ]
    }


def test_schema_defaults_and_subscription():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    settings = Settings.from_dict(schema_load(schema, {}))
    assert not settings.enabled and settings.interval == 300
    assert settings.jitter == 60 and settings.poll_range == (240, 360)
    assert not settings.ai_filter_enabled and settings.ai_timeout == 45
    assert settings.jev_confidence_threshold == 0.8
    s = Settings.from_dict(config()).subscriptions[0]
    assert s.url.endswith("/3546734206650905/showEmoji=0&embed=0")
    assert s.targets == ("test-bot:GroupMessage:123456789",)


def test_character_image_reply_is_optional_and_grouped():
    assert not Settings.from_dict({}).character_image_enabled
    assert Settings.from_dict({"base": {"character_image_enabled": True}}).character_image_enabled
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    assert not schema["base"]["items"]["character_image_enabled"]["default"]


def test_group_numbers_resolve_and_deduplicate_legacy_targets():
    cfg = config()
    cfg["subscriptions"][0]["targets"] += [" 123456789 ", 123456789, "123456"]
    settings = Settings.from_dict(cfg, qq_platform_ids=("test-bot",))
    assert settings.subscriptions[0].targets == (
        "test-bot:GroupMessage:123456789",
        "test-bot:GroupMessage:123456",
    )


@pytest.mark.parametrize(
    "platforms,selected,expected",
    [
        ((), "", "未找到 OneBot"),
        (("a", "b"), "", "多个 OneBot"),
        (("a", "b"), "missing", "不存在"),
    ],
)
def test_group_number_requires_unambiguous_platform(platforms, selected, expected):
    cfg = config()
    cfg["subscriptions"][0]["targets"] = ["123456789"]
    cfg["qq_platform_id"] = selected
    with pytest.raises(ValueError, match=expected):
        Settings.from_dict(cfg, qq_platform_ids=platforms)


def test_explicit_platform_for_multiple_accounts():
    cfg = config()
    cfg["subscriptions"][0]["targets"] = ["123456789"]
    cfg["qq_platform_id"] = "b"
    assert Settings.from_dict(cfg, qq_platform_ids=("a", "b")).subscriptions[0].targets == (
        "b:GroupMessage:123456789",
    )


def test_private_subscription_is_distinct_from_same_numbered_group():
    cfg = {"subscriptions": [
        {"name": "英文", "source": "blablalink", "enabled": True, "language": "en",
         "targets": ["qq:GroupMessage:100"]},
        {"name": "日文", "source": "blablalink", "enabled": True, "language": "ja",
         "targets": ["qq:FriendMessage:100"]},
    ]}
    assert len(Settings.from_dict(cfg).subscriptions) == 2
    cfg["subscriptions"][0]["targets"] = ["qq:FriendMessage:100"]
    with pytest.raises(ValueError, match="账号 100 的国际服语言冲突"):
        Settings.from_dict(cfg)


@pytest.mark.parametrize("target", ["0", "-1", "１２３", True, None, 123.5, "abc"])
def test_invalid_group_number(target):
    cfg = config()
    cfg["subscriptions"][0]["targets"] = [target]
    with pytest.raises(ValueError, match="目标群格式"):
        Settings.from_dict(cfg, qq_platform_ids=("test-bot",))


@pytest.mark.parametrize(
    "changes",
    [
        {"rsshub_base_url": "file:///etc/passwd"},
        {"rsshub_base_url": "http://user:secret@rss/"},
        {"poll_interval_seconds": 1},
        {"poll_interval_seconds": "no"},
        {"poll_jitter_seconds": -1},
        {"poll_jitter_seconds": 301},
        {"poll_jitter_seconds": "bad"},
        {"poll_jitter_seconds": 0.5},
        {"poll_jitter_seconds": True},
        {"ai_filter_timeout_seconds": 0},
        {"ai_filter_timeout_seconds": "bad"},
        {"subscriptions": [{"name": "x", "uid": "invalid"}]},
        {"subscriptions": [{"name": "x", "uid": "1", "targets": ["123456789"]}]},
        {"subscriptions": [{"name": "x", "uid": "1", "targets": ["bot:OtherMessage:1"]}]},
        {"subscriptions": [{"name": "x", "uid": "1"}, {"name": "y", "uid": "1"}]},
    ],
)
def test_invalid_config(changes):
    with pytest.raises(ValueError):
        Settings.from_dict({**config(), **changes})


@pytest.mark.parametrize(
    "key,value",
    [
        ("chat_context_days", 0),
        ("chat_context_days", 366),
        ("chat_context_days", 1.5),
        ("chat_context_days", True),
        ("chat_context_max_chars", 999),
        ("chat_context_max_chars", 50001),
        ("chat_context_max_chars", "bad"),
    ],
)
def test_invalid_chat_context_config(key, value):
    with pytest.raises(ValueError):
        Settings.from_dict({key: value})


@pytest.mark.parametrize(
    "change",
    [
        {"jev_token": ""},
        {"jev_token": None},
        {"jev_token": "bad\nvalue"},
        {"jev_confidence_threshold": -0.1},
        {"jev_confidence_threshold": 1.1},
        {"jev_confidence_threshold": "NaN"},
        {"jev_confidence_threshold": "inf"},
        {"jev_confidence_threshold": True},
        {"jev_model": ""},
        {"jev_timeout_seconds": 0},
        {"jev_timeout_seconds": 61},
        {"jev_timeout_seconds": 1.5},
    ],
)
def test_invalid_jev_config(change):
    cfg = {"jev_filter_enabled": True, "jev_token": "private-test-token", **change}
    with pytest.raises(ValueError) as error:
        Settings.from_dict(cfg)
    assert "private-test-token" not in str(error.value)


def test_jev_token_is_secret_in_schema_and_settings_repr():
    cfg = {
        "jev_filter_enabled": True,
        "jev_token": "private-test-token",
        "jev_confidence_threshold": "0.95",
    }
    settings = Settings.from_dict(cfg)
    assert settings.jev_confidence_threshold == 0.95 and settings.jev_timeout == 10
    assert settings.jev_token == cfg["jev_token"] and cfg["jev_token"] not in repr(settings)
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["ai_filter"]["items"]["jev_token"]["secret"] is True


def schema_load(schema, saved):
    """Model AstrBot's default insertion and unknown-key removal before plugin init."""
    result = {}
    for key, item in schema.items():
        if item["type"] == "object":
            result[key] = schema_load(item["items"], saved.get(key, {}))
        else:
            result[key] = saved.get(key, item["default"])
    return result


def test_layout_section_order_and_preserves_field_widgets():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    assert [k for k, v in schema.items() if not v.get("invisible")] == [
        "base",
        "subscriptions",
        "translation",
        "image_fallback",
        "ai_filter",
        "chat_tools",
        "prompt_templates",
        "calendar",
    ]
    assert schema["base"]["type"] == schema["ai_filter"]["type"] == "object"
    assert schema["subscriptions"]["type"] == "template_list"
    ai = schema["ai_filter"]["items"]
    assert ai["jev_confidence_threshold"]["default"] == 0.8
    assert ai["ai_filter_provider_id"]["_special"] == "select_provider"
    assert ai["jev_token"]["secret"]
    for group, fields in GROUP_FIELDS.items():
        assert set(schema[group]["items"]) == set(fields)


def test_upgrade_preserves_token_and_all_old_settings_before_and_after_framework_reload():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    original = {
        **config(),
        "enabled": True,
        "poll_interval_seconds": 600,
        "ai_filter_enabled": True,
        "ai_filter_provider_id": "remote",
        "jev_filter_enabled": True,
        "jev_token": "keep-private-token",
        "jev_confidence_threshold": 0.95,
        "chat_context_enabled": True,
        "chat_context_days": 30,
    }
    expected = Settings.from_dict(original)
    loaded = schema_load(schema, original)
    assert loaded["jev_token"] == "keep-private-token"
    assert loaded["ai_filter"]["jev_token"] == ""
    assert migrate_layout(loaded)
    assert Settings.from_dict(loaded) == expected
    assert "jev_token" not in loaded
    assert loaded["ai_filter"]["jev_token"] == "keep-private-token"
    # AstrBot inserts empty hidden legacy defaults again, but these must not win.
    reloaded = schema_load(schema, loaded)
    assert reloaded["jev_token"] == ""
    assert not migrate_layout(reloaded)
    assert Settings.from_dict(reloaded) == expected


def test_grouped_settings_win_over_hidden_legacy_defaults():
    cfg = {
        "_layout_version": 1,
        "enabled": False,
        "jev_token": "",
        "base": {"enabled": True},
        "ai_filter": {
            "jev_token": "private-token",
            "jev_filter_enabled": True,
            "jev_confidence_threshold": 0.85,
        },
    }
    settings = Settings.from_dict(cfg)
    assert settings.enabled and settings.jev_token == "private-token"
    assert settings.jev_confidence_threshold == 0.85
    assert not migrate_layout(cfg)


@pytest.mark.parametrize("section", ["base", "ai_filter", "chat_tools"])
def test_invalid_group_shape(section):
    with pytest.raises(ValueError):
        Settings.from_dict({section: "bad"})
