import pytest

from nikke.command_correction import ACTIONS, evaluate
from nikke.outpost import OutpostError, level_from_progress, load_data, parse_query, query


def verdict(text, actions=ACTIONS):
    return evaluate(text, [], {}, actions, "qq:GroupMessage:1", "1", False)


def test_snapshot_coverage_and_known_rewards():
    rows, stages = load_data()
    assert set(rows) == set(range(1, 801))
    assert len(dict(stages)) == len(stages) == 1875
    assert dict(stages)["50-15A-1"] == 1847
    assert rows[532]["core_dust_mul"] == "93.42"
    assert rows[533]["core_dust_mul"] == "102.22"
    assert query("基地等级", "533") == [
        "基地等级：533\n信用点：60.97k/小时\n战斗数据：613.27k/小时\n芯尘：102.22/小时"
    ]


def test_initial_offset_and_progress_endpoints():
    assert level_from_progress(19, 0) == 1
    assert level_from_progress(19, 4) == 2
    assert level_from_progress(1875, 1875) == 747


@pytest.mark.parametrize("value", ["", "0", "801", "533级", "５３３", "533 24", "-1", "1.5"])
def test_strict_level_parameters(value):
    with pytest.raises(OutpostError, match="/nikke 基地等级 533"):
        parse_query("基地等级", value)


def test_dust_uses_current_normal_and_hard_progress_in_either_order():
    rows, stages = load_data()
    mapping = dict(stages)
    level = level_from_progress(mapping["33-23"], mapping["18-32"])
    assert level == 316
    expected = [f"基地等级：316\n红球：{rows[316]['core_dust_mul']}/小时"]
    assert query("红球", "33-23 h18-32") == expected
    assert query("红球", "h18-32 33-23") == expected
    assert query("红球", "H18-32 33-23") == expected
    assert query("红球", "h48-32") == query("红球", "50-36 h48-32")
    assert query("红球", "h48-32")[0].startswith("基地等级：729\n")
    assert query("红球", "h50-15A-1")


@pytest.mark.parametrize("value", ["h51-1", "h50-99", "h50-15A", "h50-036", "2-11 h0-1"])
def test_missing_or_unknown_stages_get_usage(value):
    with pytest.raises(OutpostError):
        query("红球", value)


@pytest.mark.parametrize(
    "value",
    [
        "50-36",
        "",
        "h18-32 h19-1",
        "33-23 34-1",
        "33-23 h18-32 extra",
        "h18-32h19-1",
    ],
)
def test_ambiguous_and_duplicate_progress_requires_correct_syntax(value):
    with pytest.raises(OutpostError, match="/nikke 红球 h48-32"):
        parse_query("红球", value)


def test_hard_cannot_exceed_normal():
    with pytest.raises(OutpostError, match="不能超过"):
        query("红球", "33-23 h48-32")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("nikke 前哨 533", "/nikke 基地等级 533"),
        ("nikke 防御等级 533", "/nikke 基地等级 533"),
        ("nikke 基地等級 533", "/nikke 基地等级 533"),
        ("nikke 芯尘表 50-36", "/nikke 红球 h48-32"),
        ("nikke 红球查询 50-36", "/nikke 红球 h48-32"),
        ("nikke 红求 50-36", "/nikke 红球 h48-32"),
        ("nikke 基地等级 533 24", "/nikke 基地等级 533"),
        ("nikke 红球", "/nikke 红球 h48-32"),
    ],
)
def test_nonstandard_commands_only_suggest_never_execute(text, expected):
    result = verdict(text)
    assert not result.command and expected in result.message


def test_exact_commands_and_disabled_handler():
    assert verdict("nikke 基地等级 533").command == "nikke 基地等级 533"
    assert verdict("nikke 红球 h48-32").command == "nikke 红球 h48-32"
    assert verdict("nikke 红球 h18-32 33-23").command == "nikke 红球 h18-32 33-23"
    assert not verdict("nikke 红球 50-36", ("帮助",)).command
    assert not verdict("nikke 芯尘表 50-36", ("帮助",)).command
