import asyncio
import json
import sys
from dataclasses import dataclass, replace
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from nikke.config import Settings
from nikke.jev import JevError, evaluate, evaluate_framework
from tests.test_jev import data, response_data


def test_old_config_and_framework_without_token():
    old = Settings.from_dict({"ai_filter": {"jev_filter_enabled": True, "jev_token": "legacy"}})
    assert old.decision_backend == "jev" and old.jev_api_base == ""
    new = Settings.from_dict(
        {"ai_filter": {"jev_filter_enabled": True, "decision_backend": "astrbot"}}
    )
    assert new.jev_token == "" and new.decision_provider_id == ""
    with pytest.raises(ValueError):
        Settings.from_dict(
            {"ai_filter": {"jev_filter_enabled": True, "decision_backend": "gemini"}}
        )


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://server",
        "https://user:secret@server/v1",
        "https://server/?token=secret",
        "https://server:wrong",
    ],
)
def test_endpoint_validation_redacts_secrets(bad):
    with pytest.raises(ValueError) as exc:
        Settings.from_dict({"ai_filter": {"jev_api_base": bad}})
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "backend,path",
    [("jev", "/v1/systemone"), ("gemini", "/v1beta/decisions"), ("jev", "/custom-endpoint")],
)
def test_direct_protocols_use_selected_endpoint_and_auth(backend, path):
    async def run():
        seen = []

        async def endpoint(request):
            body = await request.json()
            seen.append(body)
            if backend == "gemini":
                assert request.headers["x-goog-api-key"] == "gemini-secret"
                assert "Authorization" not in request.headers
                assert body["model"] == "gemini-decision-1"
                assert json.loads(body["input"]) == {"current_text": "公告"}
                assert "state" not in body
            else:
                assert request.headers["Authorization"] == "Bearer jev-secret"
                assert body["state"] == {"current_text": "公告"}
            data = response_data()
            if backend == "gemini":
                data.pop("model")  # Google's documented response does not require model.
            return web.json_response(data)

        app = web.Application()
        app.router.add_post(path, endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        url = base + (path.rsplit("/", 1)[0] if path != "/custom-endpoint" else path)
        cfg = replace(
            Settings.from_dict({}),
            decision_backend=backend,
            jev_token="jev-secret",
            gemini_decision_token="gemini-secret",
            jev_api_base=url,
            gemini_decision_api_base=url,
        )
        try:
            result = await evaluate(cfg, {"current_text": "公告"})
            assert result.choice == "normal" and result.confidence == 0.95
            assert len(seen) == 1
            assert "secret" not in repr(cfg)
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_framework_uses_public_typed_contract(monkeypatch):
    @dataclass
    class Question:
        instructions: str
        criteria: dict

    @dataclass
    class Answer:
        choice: str
        probability: float
        probabilities: dict
        confidence: float

    module = ModuleType("astrbot.api.provider")
    module.DecisionChoiceQuestion, module.DecisionChoiceAnswer = Question, Answer
    monkeypatch.setitem(sys.modules, "astrbot.api.provider", module)
    answer = Answer("normal", 0.98, {"normal": 0.98, "lottery": 0.01, "uncertain": 0.01}, 0.95)
    ask = AsyncMock(return_value={"dynamic_type": answer})
    ctx = SimpleNamespace(decision=SimpleNamespace(ask=ask))
    cfg = replace(
        Settings.from_dict({}), decision_backend="astrbot", decision_provider_id="decision/test"
    )
    result = asyncio.run(evaluate_framework(ctx, cfg, {"current_text": "公告"}))
    assert result.confidence == 0.95  # Not the class probability .98.
    assert ask.call_args.kwargs == {"provider_id": "decision/test"}
    assert isinstance(ask.call_args.args[0]["dynamic_type"], Question)
    ask.return_value = {"dynamic_type": object()}
    with pytest.raises(JevError):
        asyncio.run(evaluate_framework(ctx, cfg, {}))


def test_old_framework_missing_decision_types_is_safe(monkeypatch):
    monkeypatch.setitem(sys.modules, "astrbot.api.provider", ModuleType("astrbot.api.provider"))
    with pytest.raises(JevError, match="升级"):
        asyncio.run(evaluate_framework(SimpleNamespace(), Settings.from_dict({}), {}))


@pytest.mark.parametrize("broken", [False, True])
def test_framework_filter_routes_only_text_and_falls_back(tmp_path, monkeypatch, broken):
    from nikke.classifier import ContentFilter
    from nikke.jev import JevResult

    sub, cfg, entry, db, ctx = data(tmp_path)
    cfg = replace(cfg, decision_backend="astrbot", jev_token="")
    evaluator = AsyncMock(
        side_effect=JevError("unsupported") if broken else None,
        return_value=JevResult("normal", 0.95, "configured"),
    )
    monkeypatch.setattr("nikke.classifier.evaluate_framework", evaluator)
    legacy = AsyncMock(side_effect=AssertionError("must not call legacy endpoint"))
    logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    classifier = ContentFilter(ctx, cfg, db, logger, jev_evaluator=legacy)
    try:
        result = asyncio.run(classifier.assess(sub, entry))
        assert set(evaluator.call_args.args[2]) == {
            "title",
            "current_text",
            "quoted_text",
            "is_repost",
        }
        assert ctx.llm_generate.await_count == int(broken)
        assert not result.skip if not broken else result.skip
        legacy.assert_not_awaited()
    finally:
        db.close()
