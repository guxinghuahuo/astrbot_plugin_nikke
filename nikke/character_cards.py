"""Compact, escaped character cards for AstrBot's configured t2i renderer."""

import math
import unicodedata

# Explicit JPEG quality also works with older t2i servers that inherit a
# renderer's default quality=40 even when callers request PNG.
CARD_OPTIONS = {"type": "jpeg", "quality": 90, "full_page": True}
LINE_BUDGET = 72
MAX_CARDS = 12
BASE_NOTE = "（基础技能，未计珍藏品强化）"
STALE_NOTE = "官方资料刷新失败，以下使用本地旧缓存。"

TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=800"><style>
* { box-sizing: border-box; }
html, body { margin: 0; width: 800px; background: #eef2f6; }
body { padding: 28px; color: #17263b; font-family: "Noto Sans CJK SC",
  "Noto Sans SC", "Microsoft YaHei", sans-serif; }
.card { background: #fff; border: 1px solid #dce3eb; border-radius: 18px;
  overflow: hidden; }
.header { padding: 25px 28px 22px; background: #182e48; color: #fff;
  border-top: 6px solid #73dfcd; }
.eyebrow { font-size: 15px; letter-spacing: 2px; color: #a7d9d3; }
h1 { font-size: 36px; line-height: 1.35; margin: 8px 0 0; overflow-wrap: anywhere; }
.subtitle { font-size: 17px; margin-top: 10px; color: #d4e0ee; }
.content { padding: 8px 28px; }
section { padding: 20px 0; border-bottom: 1px solid #e4eaf0; }
section:last-child { border: 0; }
h2 { margin: 0 0 12px; color: #17675f; font-size: 22px; line-height: 1.5; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 22px;
  line-height: 1.65; }
.footer { padding: 18px 28px 22px; background: #f5f8fb; color: #526579;
  font-size: 14px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.note { margin-bottom: 8px; color: #775c2d; }
</style></head><body><article class="card">
<header class="header"><div class="eyebrow">NIKKE · 妮姬资料</div>
<h1>{{ title | e }}</h1><div class="subtitle">{{ subtitle | e }}
{% if total > 1 %} · {{ number }} / {{ total }}{% endif %}</div></header>
<main class="content">{% for section in sections %}<section>
<h2>{{ section.heading | e }}</h2><div class="text">{{ section.text | e }}</div>
</section>{% endfor %}</main><footer class="footer">{% for note in notes %}<div
class="note">{{ note | e }}</div>{% endfor %}{{ footer | e }}</footer>
</article></body></html>"""


def line_cost(line):
    # Match the card's usable width at 22px: a full-width glyph occupies two
    # units. A conservative bound keeps wrapped source lines within the card.
    units = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line)
    return max(1, math.ceil(units / 56))


def build_cards(pages: list[str], *, subtitle="国际服 · 繁中转简体") -> list[dict]:
    """Combine three skills, repeat common provenance once, paginate without truncation."""
    if not pages or sum(map(len, pages)) > 24000:
        raise ValueError("角色资料过长，暂时无法渲染图片。")
    title, footer = "角色资料", ""
    notes, sections, cards = [], [], []
    used = 0

    def finish():
        nonlocal sections, used
        if sections:
            cards.append({"sections": sections})
            sections, used = [], 0
        if len(cards) > MAX_CARDS:
            raise ValueError("角色资料过长，暂时无法渲染图片。")

    for index, page in enumerate(pages):
        body, separator, source = page.rpartition("\n资料校验：")
        if separator:
            footer = "资料校验：" + source
        else:
            body = page
        for note in (STALE_NOTE, BASE_NOTE):
            if note in body:
                body = body.replace(note, "").strip()
                if note not in notes:
                    notes.append(note)
        heading, _, text = body.partition("\n")
        heading = heading.removeprefix("【").removesuffix("】")
        name, marker, label = heading.rpartition(" · ")
        if index == 0:
            title = name if marker else heading
        heading = label if marker else "资料"
        lines, cost = [], 4
        for line in text.splitlines():
            # A single unusually long source paragraph must also be splittable.
            fragments, fragment, width = [], "", 0
            for char in line:
                units = 2 if unicodedata.east_asian_width(char) in "WF" else 1
                if width + units > 56:
                    fragments.append(fragment)
                    fragment, width = "", 0
                fragment += char
                width += units
            fragments.append(fragment)
            for part in fragments:
                count = line_cost(part)
                if used + cost + count > LINE_BUDGET:
                    if lines:
                        sections.append({"heading": heading, "text": "\n".join(lines)})
                        lines, cost = [], 4
                        heading = label + "（续）" if marker else "资料（续）"
                    finish()
                lines.append(part)
                cost += count
        sections.append({"heading": heading, "text": "\n".join(lines)})
        used += cost
    finish()
    for number, card in enumerate(cards, 1):
        card.update(
            title=title,
            subtitle=subtitle,
            footer=footer,
            notes=notes,
            number=number,
            total=len(cards),
        )
    return cards
