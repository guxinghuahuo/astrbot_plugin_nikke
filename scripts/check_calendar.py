"""Read-only network probe, usable inside the deployed AstrBot container."""

import asyncio
import json
import sys
import time
from pathlib import Path


async def check():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from nikke.calendar import CalendarClient, date_text

    payload, events = await CalendarClient().fetch()
    now = time.time()
    print(
        json.dumps(
            {
                "official_code": payload["code"],
                "events": len(events),
                "current": sum(event.start <= now < event.end for event in events),
                "future": sum(event.start > now for event in events),
                "messages_sent": 0,
            },
            ensure_ascii=False,
        )
    )
    for event in events:
        if event.end > now:
            print(f"{event.title} | {event.notice} | {date_text(event.end)} 北京时间")


if __name__ == "__main__":
    asyncio.run(check())
