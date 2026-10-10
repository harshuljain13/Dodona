from __future__ import annotations

import json
import os
from pathlib import Path

class RequestTrace:

    def __init__(self) -> None:
        self.events: list[dict] = []

    def reset(self) -> None:
        self.events.clear()

    def record(self, event: dict) -> None:
        self.events.append(event)
        path = os.environ.get("TRACE_PATH", "").strip()
        if not path:
            return
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")

    def overflow_count(self) -> int:
        return sum(1 for ev in self.events if ev.get("via") == "overflow")

TRACES = RequestTrace()
