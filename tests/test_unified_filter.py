import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nikke.classifier import ContentFilter
from nikke.config import Settings, migrate_layout
from nikke.jev import JevResult
from nikke.service import Service
from tests.test_jev import data


@pytest.mark.parametrize("backend,prefix", [("jev", "jev_"), ("gemini", "gemini_decision_")])
def test_legacy_fields_migrate_once_and_clear_does_not_restore(backend, prefix):
    cfg = {
        "_layout_version": 1,
        "ai_filter": {
            "decision_backend": backend,
            prefix + "token": "private",
            prefix + "api_base": "https://example.org/v1",
            prefix + "model": "my-model",
        },
    }
    assert migrate_layout(cfg)
    section = cfg["ai_filter"]
    assert section["decision_token"] == "private" and section["decision_model"] == "my-model"
    assert not migrate_layout(cfg)
    section["decision_token"] = ""
    assert not migrate_layout(cfg)
    assert Settings.from_dict(cfg).jev_token == ""


@pytest.mark.parametrize("protocol", ["systemone", "gemini"])
def test_custom_shared_fields(protocol):
    cfg = Settings.from_dict(
        {
            "ai_filter": {
                "decision_backend": "custom",
                "decision_protocol": protocol,
                "decision_token": "key",
                "decision_api_base": "https://custom.example/v1",
                "decision_model": "custom-model",
                "jev_filter_enabled": True,
            }
        }
    )
    assert cfg.jev_token == cfg.gemini_decision_token == "key"
    assert cfg.jev_api_base == cfg.gemini_decision_api_base == "https://custom.example/v1"
    assert (cfg.gemini_decision_model if protocol == "gemini" else cfg.jev_model) == "custom-model"


@pytest.mark.parametrize(
    "choice,confidence,llm,skip",
    [
        ("normal", 0.9, False, False),
        ("offline", 0.9, False, True),
        ("offline", 0.7, True, True),
        ("normal", 0.7, True, True),
        ("uncertain", 0.99, True, True),
    ],
)
def test_international_online_policy_and_fallback(tmp_path, choice, confidence, llm, skip):
    sub, cfg, entry, db, ctx = data(tmp_path)
    sub = replace(sub, source="blablalink", language="ja")
    db.sync((sub,))
    ctx.llm_generate.return_value = SimpleNamespace(
        completion_text='{"category":"offline","reason":"仅秋叶原线下见面会"}'
    )
    evaluator = AsyncMock(return_value=JevResult(choice, confidence, "judge"))
    logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    f = ContentFilter(ctx, cfg, db, logger, jev_evaluator=evaluator)
    try:
        result = asyncio.run(f.assess(sub, entry))
        assert result.skip == skip and ctx.llm_generate.await_count == int(llm)
        settings, state = evaluator.call_args.args
        assert set(json.loads(settings.jev_criteria)) == {"normal", "offline", "uncertain"}
        assert "线上 cosplay 比赛" in settings.jev_instructions
        assert set(state) == {"title", "current_text", "quoted_text", "is_repost"}
        if llm:
            assert "秋叶原" in ctx.llm_generate.call_args.kwargs["system_prompt"]
    finally:
        db.close()


def test_offline_filtered_before_translation_and_images(tmp_path):
    sub, cfg, entry, db, ctx = data(tmp_path)
    sub = replace(sub, source="blablalink", language="ja")
    cfg = replace(cfg, subscriptions=(sub,))
    db.sync((sub,))
    db.ingest(sub, [], 100, 300)
    db.ingest(sub, [entry], 400, 300)
    logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    classifier = ContentFilter(
        ctx,
        cfg,
        db,
        logger,
        jev_evaluator=AsyncMock(return_value=JevResult("offline", 0.9, "judge")),
    )
    translator = AsyncMock()
    sender = AsyncMock()
    client = AsyncMock()
    service = Service(
        cfg,
        db,
        client,
        sender,
        logger,
        clock=lambda: 400,
        classifier=classifier.assess,
        translator=translator,
    )
    try:
        asyncio.run(service.deliver_once())
        assert db.status(sub.key)["filtered"] == 2
        translator.assert_not_awaited()
        sender.assert_not_awaited()
        client.image.assert_not_awaited()
    finally:
        db.close()


def test_source_templates_schema_defaults_and_saved_custom_values():
    from pathlib import Path

    from tests.test_config import schema_load

    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    configured = Settings.from_dict(schema_load(schema, {}))
    assert configured == Settings.from_dict({})
    assert configured.default_international_policy
    for section in schema["prompt_templates"]["items"].values():
        for key, field in section["items"].items():
            assert field["default"] == getattr(configured, key)
            assert field["editor_mode"]
    saved = {
        "prompt_templates": {
            "jev": {"jev_instructions": "旧自定义抽奖条件"},
            "international_decision": {"international_instructions": "新的线下判断条件"},
        }
    }
    loaded = schema_load(schema, saved)
    assert Settings.from_dict(loaded) == Settings.from_dict(saved)
    assert Settings.from_dict(loaded).jev_instructions == "旧自定义抽奖条件"
    assert Settings.from_dict(loaded).international_instructions == "新的线下判断条件"


@pytest.mark.parametrize(
    "templates",
    [
        {"international_llm": []},
        {"international_decision": []},
        {"international_llm": {"international_system_prompt": "缺失占位符"}},
        {"international_llm": {"international_categories": "[]"}},
        {
            "international_llm": {
                "international_categories": '{"offline":{"description":"x","filter":"true"}}'
            }
        },
        {"international_decision": {"international_instructions": ""}},
        {
            "international_decision": {
                "international_criteria": '{"normal":"x","lottery":"y","uncertain":"z"}'
            }
        },
    ],
)
def test_invalid_international_templates(templates):
    with pytest.raises(ValueError):
        Settings.from_dict({"prompt_templates": templates})


def test_international_custom_decision_templates_reach_model_and_invalidate_cache(tmp_path):
    async def scenario():
        sub, cfg, entry, db, ctx = data(tmp_path)
        sub = replace(sub, source="blablalink", language="ja")
        db.sync((sub,))
        evaluator = AsyncMock(return_value=JevResult("normal", 0.99, "judge"))
        logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)

        async def assess(settings):
            return await ContentFilter(ctx, settings, db, logger, jev_evaluator=evaluator).assess(
                sub, entry
            )

        try:
            await assess(cfg)
            await assess(cfg)
            assert evaluator.await_count == 1
            cfg = replace(cfg, international_instructions="自定义线下规则")
            await assess(cfg)
            assert evaluator.await_count == 2
            assert evaluator.call_args.args[0].jev_instructions == "自定义线下规则"
            criteria = json.loads(cfg.international_criteria)
            criteria["offline"] = "只过滤需亲自到场的展览"
            cfg = replace(cfg, international_criteria=json.dumps(criteria))
            await assess(cfg)
            assert evaluator.await_count == 3
            assert json.loads(evaluator.call_args.args[0].jev_criteria) == criteria
            ctx.llm_generate.assert_not_awaited()
        finally:
            db.close()

    asyncio.run(scenario())


def test_international_custom_llm_action_is_not_bypassed_by_judge(tmp_path):
    async def scenario():
        sub, cfg, entry, db, ctx = data(tmp_path)
        sub = replace(sub, source="blablalink", language="ja")
        db.sync((sub,))
        evaluator = AsyncMock(return_value=JevResult("offline", 0.99, "judge"))
        logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
        ctx.llm_generate.return_value = SimpleNamespace(
            completion_text='{"category":"offline","reason":"自定义保留"}'
        )
        categories = json.loads(cfg.international_categories)
        categories["offline"]["filter"] = False
        cfg = replace(cfg, international_categories=json.dumps(categories))
        try:
            f = ContentFilter(ctx, cfg, db, logger, jev_evaluator=evaluator)
            assert not (await f.assess(sub, entry)).skip
            assert not (await f.assess(sub, entry)).skip
            assert ctx.llm_generate.await_count == 1
            evaluator.assert_not_awaited()
            categories["offline"]["filter"] = True
            cfg = replace(
                cfg,
                international_categories=json.dumps(categories),
                international_system_prompt="自定义规则 ${classification_rules}",
            )
            assert (
                await ContentFilter(ctx, cfg, db, logger, jev_evaluator=evaluator).assess(
                    sub, entry
                )
            ).skip
            assert ctx.llm_generate.await_count == 2
            assert "自定义规则" in ctx.llm_generate.call_args.kwargs["system_prompt"]
        finally:
            db.close()

    asyncio.run(scenario())
