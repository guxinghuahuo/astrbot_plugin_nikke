"""Editable policies affect requests, shortcuts and persisted classification caches."""

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_classifier import WINNER_TEXT, fixture_data
from test_config import schema_load

from nikke.classifier import ContentFilter
from nikke.config import Settings
from nikke.jev import JevResult
from nikke.prompts import CATEGORIES_JSON, JEV_CRITERIA_JSON, JEV_INSTRUCTIONS, SYSTEM_PROMPT


def test_schema_editors_have_runtime_defaults_and_nested_frames():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    templates = schema["prompt_templates"]
    assert templates["type"] == "object"
    for section, key, default in (
        ("llm", "llm_system_prompt", SYSTEM_PROMPT),
        ("llm", "llm_categories", CATEGORIES_JSON),
        ("jev", "jev_instructions", JEV_INSTRUCTIONS),
        ("jev", "jev_criteria", JEV_CRITERIA_JSON),
    ):
        assert templates["items"][section]["type"] == "object"
        field = templates["items"][section]["items"][key]
        assert field["default"] == default
        assert field["editor_mode"] and field["type"] == "text"
    assert Settings.from_dict({}).default_llm_policy


@pytest.mark.parametrize(
    "templates",
    [
        "bad",
        {"llm": []},
        {"jev": "bad"},
        {"llm": {"llm_system_prompt": "missing placeholder"}},
        {"llm": {"llm_categories": "not JSON"}},
        {"llm": {"llm_categories": "{}"}},
        {"llm": {"llm_categories": '{"custom":{"description":"test","filter":"false"}}'}},
        {"llm": {"llm_categories": '{"Bad":{"description":"test","filter":false}}'}},
        {"jev": {"jev_criteria": '{"normal":"x"}'}},
        {"jev": {"jev_instructions": ""}},
    ],
)
def test_invalid_custom_templates_are_rejected(templates):
    with pytest.raises(ValueError):
        Settings.from_dict({"prompt_templates": templates})


def test_custom_templates_survive_framework_config_reload():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    saved = {
        "_layout_version": 1,
        "prompt_templates": {
            "llm": {
                "llm_system_prompt": "自定义规则 ${classification_rules}",
                "llm_categories": '{"custom":{"description":"新分类","filter":false}}',
            },
            "jev": {"jev_instructions": "自定义 Jev 判断提示词"},
        },
    }
    expected = Settings.from_dict(saved)
    loaded = schema_load(schema, saved)
    assert Settings.from_dict(loaded) == expected
    assert Settings.from_dict(schema_load(schema, loaded)) == expected


def test_custom_categories_actions_templates_and_cache_invalidation(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        categories = {"custom": {"description": "自定义分类", "filter": True}}
        configured = Settings.from_dict(
            {
                "prompt_templates": {
                    "llm": {
                        "llm_system_prompt": '遵守分类 ${classification_rules}，示例 {"test":1}。',
                        "llm_categories": json.dumps(categories),
                    }
                }
            }
        )
        settings = replace(
            settings,
            llm_system_prompt=configured.llm_system_prompt,
            llm_categories=configured.llm_categories,
        )
        ctx.llm_generate.return_value = SimpleNamespace(
            completion_text='{"category":"custom","reason":"自定义条件命中"}'
        )

        async def assess(active):
            return await ContentFilter(ctx, active, db, logging.getLogger("test")).assess(
                sub, entry
            )

        assert (await assess(settings)).skip
        assert (await assess(settings)).skip
        assert ctx.llm_generate.await_count == 1
        prompt = ctx.llm_generate.call_args.kwargs["system_prompt"]
        assert "${classification_rules}" not in prompt
        assert "自定义分类" in prompt and '{"test":1}' in prompt
        categories["custom"]["filter"] = False
        settings = replace(settings, llm_categories=json.dumps(categories))
        assert not (await assess(settings)).skip
        assert not (await assess(settings)).skip
        assert ctx.llm_generate.await_count == 2
        settings = replace(settings, llm_system_prompt=settings.llm_system_prompt + "新规则")
        assert not (await assess(settings)).skip
        assert ctx.llm_generate.await_count == 3
        db.close()

    asyncio.run(scenario())


def test_custom_policy_is_not_bypassed_by_local_rule_or_jev(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        settings = replace(
            settings, jev_filter_enabled=True, llm_system_prompt=SYSTEM_PROMPT + "\n允许中奖通知。"
        )
        jev = AsyncMock(return_value=JevResult("normal", 1, "test"))
        ctx.llm_generate.return_value = SimpleNamespace(
            completion_text='{"category":"news","reason":"按自定义要求保留"}'
        )
        content_filter = ContentFilter(
            ctx, settings, db, logging.getLogger("test"), jev_evaluator=jev
        )
        assert not (await content_filter.assess(sub, replace(entry, text=WINNER_TEXT))).skip
        ctx.llm_generate.assert_awaited_once()
        jev.assert_not_awaited()
        db.close()

    asyncio.run(scenario())


def test_jev_template_changes_invalidate_cache(tmp_path):
    async def scenario():
        sub, settings, entry, db, ctx = fixture_data(tmp_path)
        settings = replace(settings, jev_filter_enabled=True)
        jev = AsyncMock(return_value=JevResult("normal", 1, "test"))

        async def assess(active):
            return await ContentFilter(
                ctx, active, db, logging.getLogger("test"), jev_evaluator=jev
            ).assess(sub, entry)

        assert not (await assess(settings)).skip
        assert not (await assess(settings)).skip
        assert jev.await_count == 1
        settings = replace(settings, jev_instructions="自定义 Jev 判断提示词")
        await assess(settings)
        assert jev.await_count == 2
        criteria = json.loads(settings.jev_criteria)
        criteria["normal"] = "新的正常分类条件"
        settings = replace(settings, jev_criteria=json.dumps(criteria))
        await assess(settings)
        assert jev.await_count == 3
        ctx.llm_generate.assert_not_awaited()
        db.close()

    asyncio.run(scenario())
