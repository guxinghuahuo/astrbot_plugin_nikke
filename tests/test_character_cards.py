import pytest
from jinja2 import Environment

from nikke.character_cards import BASE_NOTE, STALE_NOTE, TEMPLATE, build_cards


def pages(body="■自身\n【攻击力10%▲】【持续10秒】", stale=False):
    footer = "\n资料校验：2026-11-01 12:00（北京时间）\n来源：BlaBlaLink\nhttps://example.test"
    return [
        f"【小红帽 · {name} Lv.10】\n技能名称\n{body}\n{BASE_NOTE}"
        + ("\n" + STALE_NOTE if stale else "")
        + footer
        for name in ("技能1", "技能2", "爆裂技能")
    ]


def test_skills_combine_with_one_source_and_keep_warnings():
    cards = build_cards(pages(stale=True))
    assert len(cards) == 1 and cards[0]["title"] == "小红帽"
    assert [section["heading"] for section in cards[0]["sections"]] == [
        "技能1 Lv.10",
        "技能2 Lv.10",
        "爆裂技能 Lv.10",
    ]
    rendered = Environment(autoescape=False).from_string(TEMPLATE).render(cards[0])
    assert rendered.count("资料校验：") == 1
    assert rendered.count(BASE_NOTE) == rendered.count(STALE_NOTE) == 1
    assert rendered.count("【攻击力10%▲】【持续10秒】") == 3


def test_external_text_is_escaped_without_losing_game_condition_headings():
    text = '<在阶段1使用：辅助部队行动方针>\n<script>alert(1)</script>\n{{7*7}} & "攻击"'
    card = build_cards(pages(text))[0]
    rendered = Environment(autoescape=False).from_string(TEMPLATE).render(card)
    assert "&lt;在阶段1使用：辅助部队行动方针&gt;" in rendered
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "{{7*7}}" in rendered and "&amp;" in rendered


def test_long_skills_paginate_and_preserve_all_content():
    body = "\n".join(f"第{i}段：" + "攻击力提升" * 10 for i in range(55))
    cards = build_cards(pages(body))
    assert 1 < len(cards) <= 12
    combined = "".join(
        section["text"].replace("\n", "") for c in cards for section in c["sections"]
    )
    assert combined == ("技能名称" + body.replace("\n", "")) * 3
    assert [c["number"] for c in cards] == list(range(1, len(cards) + 1))
    assert all(c["total"] == len(cards) and c["footer"].startswith("资料校验：") for c in cards)


@pytest.mark.parametrize("data", [[], ["x" * 24001]])
def test_unbounded_payloads_are_not_rendered(data):
    with pytest.raises(ValueError):
        build_cards(data)
