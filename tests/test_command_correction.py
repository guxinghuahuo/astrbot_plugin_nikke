from nikke.command_correction import (
    ACTIONS,
    evaluate,
    normalize,
    similarity,
    source_conflict,
    unique_fuzzy,
)
from nikke.config import Subscription

ORIGIN = "qq:GroupMessage:1"


def intl(name="NIKKE国际服", language="zh-TW", targets=(ORIGIN,)):
    return Subscription(name, "official", "http://rss/" + language, targets, "blablalink", language)


def check(text, subs=None, disabled=None, actions=ACTIONS, group="1", admin=False):
    return evaluate(
        text, (intl(),) if subs is None else subs, disabled or {}, actions, ORIGIN, group, admin
    )


def test_normalization_and_edit_distance_boundaries():
    assert normalize(" ＮＩＫＫＥ國際服 ") == "nikke国际服"
    assert similarity("nikke国际腹", "nikke国际服") == 0.875
    assert unique_fuzzy([0.85, 0.70], 3)
    assert not unique_fuzzy([0.8499], 10)
    assert not unique_fuzzy([0.9, 0.7501], 10)
    assert not unique_fuzzy([1], 2)
    assert not unique_fuzzy([], 10)
    assert similarity("x" * 129, "x" * 129) == 0


def test_treasure_commands_preserve_parameters_and_reject_ambiguous_stage_syntax():
    for query in ("迪塞尔 珍藏品1", "拉普拉斯 7 珍藏品3"):
        result = check("nikke 技能 " + query)
        assert result.command == "nikke 技能 " + query and not result.message
    for query in ("迪塞尔 珍藏品", "迪塞尔 珍藏品4", "迪塞尔 珍藏品3 7"):
        result = check("nikke 技能 " + query)
        assert not result.command and "[珍藏品1–3]" in result.message


def test_exact_normalized_and_unique_high_score_names():
    for text in (
        "nikke 最新 NIKKE国际服",
        "NIKKE 最新 nikke国际服",
        "ｎｉｋｋｅ 最新 ＮＩＫＫＥ國際服",
        "nikke 最新 NIKKE国际腹",
    ):
        result = check(text)
        assert result.command == "nikke 最新 NIKKE国际服" and not result.message
    raw = intl("ＮＩＫＫＥ國際服")
    assert check("nikke 最新 " + raw.name, [raw, intl()]).command.endswith(raw.name)


def test_low_similarity_example_only_provides_correct_command():
    result = check("nikke 最新 nikke资讯")
    assert not result.command
    assert "/nikke 最新 NIKKE国际服" in result.message


def test_ambiguity_and_normalized_collisions_do_not_execute():
    for name, subs in [
        ("NIKKE国际腹", [intl(), intl("NIKKE国际版")]),
        ("nikke国际服", [intl(), intl("nikke國際服")]),
    ]:
        result = check("nikke 最新 " + name, subs)
        assert not result.command and "/nikke 最新 " in result.message
    assert check("nikke 最新 NIKKE国际服", [intl(), intl("nikke國際服")]).command


def test_explicit_source_and_language_conflicts_block_auto_execution():
    assert not check("nikke 最新 NIKKE国服").command
    assert not check("nikke 最新 NIKKE日服", [intl("NIKKE韩服", "ko")]).command
    for value in ("b站资讯", "国服", "bilibili"):
        assert source_conflict(value, intl())
    assert source_conflict("NIKKE英文", intl(language="ja"))
    assert source_conflict("NIKKE_ja", intl(language="en"))
    bili = Subscription("NIKKE官方", "1", "http://rss/bili", (ORIGIN,))
    assert source_conflict("国际服公告", bili)


def test_scope_disabled_names_and_private_chats():
    secret = intl("其他群私有名称", targets=("qq:GroupMessage:2",))
    assert "目标群" in check("nikke 最新 " + secret.name, [intl(), secret]).message
    assert "已停用" in check("nikke 最新 NIKKE官方", disabled={"NIKKE官方": True}).message
    assert "已停用" not in check("nikke 最新 hidden", disabled={"hidden": False}).message
    assert secret.name not in check("nikke 最新 x", [intl(), secret]).message
    result = check("nikke 最新 x", [secret], group="")
    assert "目标群" in result.message and secret.name not in result.message
    assert check("nikke 检查 " + secret.name, [secret], admin=True).command


def test_parameter_errors_unknown_action_and_permissions():
    for text in ("nikke 最新", "nikke 最新 NIKKE国际服 多余内容", "nikke 最新 NIKKE 国际服"):
        result = check(text)
        assert not result.command and "/nikke 最新 NIKKE国际服" in result.message
    for action in ("帮助", "活动", "状态"):
        result = check("nikke " + action + " extra", admin=True)
        assert not result.command and "不接受额外参数" in result.message
    result = check("nikke 最心 NIKKE国际服")
    assert not result.command and "/nikke 最新 NIKKE国际服" in result.message
    assert "秘密" not in check("nikke 检查 wrong", [intl("秘密")]).command
    result = check("nikke 最新 bad", actions=("帮助", "活动"))
    assert "停用" in result.message and not result.command
    assert check("nikke 最新 missing", []).command == ""
    assert "检查" not in check("nikke 查询 private").message


def test_public_character_commands_preserve_names_and_validate_levels():
    assert check("nikke 角色 拉毗：小紅帽").command == "nikke 角色 拉毗：小紅帽"
    assert check("nikke 技能 拉毗：小红帽 7").command.endswith("拉毗：小红帽 7")
    assert check("nikke 技能 产品 08 7").command.endswith("产品 08 7")
    assert "用法" in check("nikke 角色").message
    assert "1–10" in check("nikke 技能 拉毗 11").message
    assert "停用" in check("nikke 技能 拉毗", actions=("角色",)).message


def test_suggestions_are_limited_to_three_candidates():
    result = check("nikke 最新", [intl("订阅" + str(i)) for i in range(8)])
    assert sum(line.startswith("/nikke 最新 ") for line in result.message.splitlines()) == 3
