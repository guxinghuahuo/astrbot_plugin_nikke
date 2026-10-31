"""Apply display preferences without changing stored source content or identifiers."""

import re
from dataclasses import replace

from .chinese import simplified
from .config import Subscription
from .feed import Entry


def display_text(text: str) -> str:
    # Links are data, including percent encoding and non-ASCII paths/query values.
    return "".join(
        part if index % 2 else simplified(part)
        for index, part in enumerate(re.split(r"(https?://[^\s<>]+)", text))
    )


def display_entry(subscription: Subscription, entry: Entry) -> Entry:
    if (
        subscription.source != "blablalink"
        or subscription.language != "zh-TW"
        or not subscription.simplify
    ):
        return entry
    return replace(
        entry,
        title=display_text(entry.title),
        text=display_text(entry.text),
        author=display_text(entry.author),
    )
